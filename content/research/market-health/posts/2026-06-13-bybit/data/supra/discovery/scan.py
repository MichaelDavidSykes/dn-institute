#!/usr/bin/env python3
"""Reproduce the 2026-07-15 exploratory scan that surfaced SUPRA/USDT."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import gzip
import hashlib
import io
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
OUTPUT_FIELDS = [
    "score_rank", "selection_rank", "symbol", "ticker_turnover_24h",
    "trades", "download_bytes", "notional_3d", "price_min", "price_max",
    "price_ratio", "top1_size_share", "top5_size_share",
    "balanced_recurring_top5_share", "max_second", "max_second_share",
    "second_rank2", "top_two_second_share", "top_gap_ms", "top_gap_share",
    "benford_l1", "anomaly_score", "followup_candidate", "followup_reason",
]
NUMERIC_FIELDS = set(OUTPUT_FIELDS) - {
    "symbol", "followup_candidate", "followup_reason"
}
SUPRA_REASON = (
    "Manual exploratory follow-up: seconds 00/59 held 35.6% of trades, "
    "the leading positive gap was 300 ms, sample size was 5,309, and no "
    "exact clip dominated."
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "selection_rank", "symbol", "ticker_turnover_24h", "date", "url",
        "compressed_bytes", "sha256",
    }
    if not rows or set(rows[0]) != required:
        raise ValueError(f"unexpected manifest columns in {path}")
    keys = {(row["symbol"], row["date"]) for row in rows}
    symbols = {row["symbol"] for row in rows}
    dates = {row["date"] for row in rows}
    if len(rows) != 240 or len(keys) != 240 or len(symbols) != 80 or len(dates) != 3:
        raise ValueError("manifest must pin 80 symbols x 3 unique dates")
    return rows


def cached_path(cache_dir: Path, row: dict[str, str]) -> Path:
    return cache_dir / f'{row["symbol"]}_{row["date"]}.csv.gz'


def valid_bytes(data: bytes, row: dict[str, str]) -> bool:
    return (
        len(data) == int(row["compressed_bytes"])
        and sha256(data) == row["sha256"]
    )


def obtain_archive(
    row: dict[str, str], cache_dir: Path, offline: bool, retries: int = 8
) -> tuple[dict[str, str], bytes]:
    path = cached_path(cache_dir, row)
    if path.exists():
        data = path.read_bytes()
        if valid_bytes(data, row):
            return row, data
        if offline:
            raise ValueError(f"cached file fails manifest integrity: {path}")
        path.unlink()
    if offline:
        raise FileNotFoundError(f"offline cache miss: {path}")

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                row["url"], headers={"User-Agent": "dn-institute-discovery-repro/1.0"}
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
            if not valid_bytes(data, row):
                raise ValueError(
                    f'archive differs from pinned bytes/hash: {row["url"]}'
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            return row, data
        except Exception as error:  # Network errors are retried; integrity still fails.
            last_error = error
            if attempt + 1 < retries:
                time.sleep(1 + 2 * attempt)
    assert last_error is not None
    raise last_error


def parse_trades(data: bytes, date: str) -> list[tuple[int, float, float, str, str, str]]:
    text = gzip.decompress(data).decode("utf-8")
    trades = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            trades.append(
                (
                    int(row["timestamp"]),
                    float(row["price"]),
                    float(row["volume"]),
                    row["side"].lower(),
                    date,
                    row["volume"],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return trades


def analyze_symbol(
    symbol: str,
    selection_rank: int,
    turnover: float,
    archives: list[tuple[dict[str, str], bytes]],
) -> dict[str, object]:
    rows: list[tuple[int, float, float, str, str, str]] = []
    download_bytes = 0
    for manifest_row, data in sorted(archives, key=lambda item: item[0]["date"]):
        rows.extend(parse_trades(data, manifest_row["date"]))
        download_bytes += len(data)
    rows.sort()
    n = len(rows)
    if n < 50:
        raise ValueError(f"{symbol}: expected at least 50 parsed trades, got {n}")

    exact = collections.Counter(row[5] for row in rows)
    top_sizes = exact.most_common(10)
    second_counts = collections.Counter((row[0] // 1000) % 60 for row in rows)
    top_seconds = second_counts.most_common(5)

    sides_by_second: dict[int, list[int]] = collections.defaultdict(lambda: [0, 0])
    sides_by_size: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for row in rows:
        side_index = 0 if row[3] == "buy" else 1
        sides_by_second[(row[0] // 1000) % 60][side_index] += 1
        sides_by_size[row[5]][side_index] += 1

    balanced = []
    for size, count in exact.items():
        buys, sells = sides_by_size[size]
        if count >= max(15, int(n * 0.002)) and min(buys, sells) / count >= 0.35:
            balanced.append((count, size, buys, sells))
    balanced.sort(reverse=True)
    balanced = balanced[:10]

    prices = [row[1] for row in rows]
    notional = sum(row[1] * row[2] for row in rows)
    first_digits = collections.Counter()
    for row in rows:
        value = abs(row[2])
        if value <= 0:
            continue
        while value < 1:
            value *= 10
        while value >= 10:
            value /= 10
        digit = int(value)
        if 1 <= digit <= 9:
            first_digits[digit] += 1
    digit_total = sum(first_digits.values())
    observed = [first_digits[digit] / digit_total for digit in range(1, 10)]
    benford = [math.log10(1 + 1 / digit) for digit in range(1, 10)]
    benford_l1 = sum(abs(actual - expected) for actual, expected in zip(observed, benford))

    gaps = [
        rows[index][0] - rows[index - 1][0]
        for index in range(1, n)
        if 0 < rows[index][0] - rows[index - 1][0] <= 600_000
    ]
    gap_bins = collections.Counter(round(gap / 10) * 10 for gap in gaps)
    top_gaps = gap_bins.most_common(5)

    top1_share = top_sizes[0][1] / n
    top5_share = sum(count for _, count in top_sizes[:5]) / n
    max_second_share = top_seconds[0][1] / n
    top_two_second_share = sum(count for _, count in top_seconds[:2]) / n
    balanced_share = round(
        sum(round(count / n, 5) for count, _, _, _ in balanced[:5]), 5
    )
    price_ratio = round(max(prices) / min(prices), 4)
    score = round(
        6 * top1_share
        + 3 * top5_share
        + 7 * max_second_share
        + 4 * top_two_second_share
        + 0.5 * round(benford_l1, 5)
        + min(2, max(0, price_ratio - 1)) / 3
        + 5 * balanced_share,
        5,
    )

    return {
        "selection_rank": selection_rank,
        "symbol": symbol,
        "ticker_turnover_24h": turnover,
        "trades": n,
        "download_bytes": download_bytes,
        "notional_3d": round(notional, 2),
        "price_min": min(prices),
        "price_max": max(prices),
        "price_ratio": price_ratio,
        "top1_size_share": top1_share,
        "top5_size_share": top5_share,
        "balanced_recurring_top5_share": balanced_share,
        "max_second": top_seconds[0][0],
        "max_second_share": max_second_share,
        "second_rank2": top_seconds[1][0],
        "top_two_second_share": top_two_second_share,
        "top_gap_ms": top_gaps[0][0],
        "top_gap_share": round(top_gaps[0][1] / len(gaps), 5),
        "benford_l1": round(benford_l1, 5),
        "anomaly_score": score,
        "followup_candidate": "yes" if symbol == "SUPRAUSDT" else "no",
        "followup_reason": SUPRA_REASON if symbol == "SUPRAUSDT" else "",
    }


def write_results(path: Path, results: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(results)


def verify_selection(
    manifest: list[dict[str, str]], selection_path: Path
) -> None:
    with selection_path.open(newline="") as handle:
        expected = list(csv.DictReader(handle))
    manifest_pairs = {
        (int(row["selection_rank"]), row["symbol"], float(row["ticker_turnover_24h"]))
        for row in manifest
    }
    selected_pairs = {
        (int(row["selection_rank"]), row["symbol"], float(row["ticker_turnover_24h"]))
        for row in expected
    }
    if len(expected) != 80 or manifest_pairs != selected_pairs:
        raise AssertionError("pair_selection.csv and input_manifest.csv disagree")


def verify_results(results: list[dict[str, object]], expected_path: Path) -> None:
    with expected_path.open(newline="") as handle:
        expected = list(csv.DictReader(handle))
    if len(results) != 80 or len(expected) != 80:
        raise AssertionError("expected exactly 80 result rows")
    for actual, wanted in zip(results, expected):
        for field in OUTPUT_FIELDS:
            if field in NUMERIC_FIELDS:
                if not math.isclose(
                    float(actual[field]), float(wanted[field]), rel_tol=1e-12, abs_tol=1e-12
                ):
                    raise AssertionError(
                        f'{actual["symbol"]} {field}: {actual[field]} != {wanted[field]}'
                    )
            elif str(actual[field]) != wanted[field]:
                raise AssertionError(
                    f'{actual["symbol"]} {field}: {actual[field]!r} != {wanted[field]!r}'
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "input_manifest.csv")
    parser.add_argument("--selection", type=Path, default=HERE / "pair_selection.csv")
    parser.add_argument("--expected", type=Path, default=HERE / "scan_results.csv")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "dni-bybit-discovery",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    verify_selection(manifest, args.selection)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        obtained = list(
            executor.map(
                lambda row: obtain_archive(row, args.cache_dir, args.offline),
                manifest,
            )
        )
    grouped: dict[str, list[tuple[dict[str, str], bytes]]] = collections.defaultdict(list)
    metadata: dict[str, tuple[int, float]] = {}
    for row, data in obtained:
        grouped[row["symbol"]].append((row, data))
        metadata[row["symbol"]] = (
            int(row["selection_rank"]), float(row["ticker_turnover_24h"])
        )
    results = [
        analyze_symbol(symbol, metadata[symbol][0], metadata[symbol][1], archives)
        for symbol, archives in grouped.items()
    ]
    results.sort(key=lambda row: (-float(row["anomaly_score"]), int(row["selection_rank"])))
    for rank, row in enumerate(results, 1):
        row["score_rank"] = rank
    write_results(args.output, results)
    if args.verify:
        verify_results(results, args.expected)
    supra = next(row for row in results if row["symbol"] == "SUPRAUSDT")
    suffix = "; committed output verified" if args.verify else ""
    print(
        f'Analyzed {len(results)} pairs from {len(manifest)} pinned archives{suffix}. '
        f'SUPRA/USDT: score rank {supra["score_rank"]}, {supra["trades"]} trades, '
        f'seconds 00/59 share {100 * float(supra["top_two_second_share"]):.2f}%.'
    )
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
