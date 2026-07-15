#!/usr/bin/env python3
"""Reproduce the SUPRA/USDT five-minute-boundary analysis.

The script downloads Bybit's dated public spot-trade archives, validates each
file, writes the processed CSVs used by the article, and optionally regenerates
the charts.  The calculations use only the Python standard library; charts
additionally require matplotlib.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import gzip
import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
import statistics
import time
from typing import Dict, List, Sequence, Tuple
import urllib.error
import urllib.request


START_DATE = dt.date(2026, 6, 15)
END_DATE = dt.date(2026, 7, 14)
DISCOVERY_START = dt.date(2026, 7, 12)
SYMBOLS = ("SUPRAUSDT", "CITYUSDT", "PYBOBOUSDT")
TARGET = "SUPRAUSDT"
BASE_URL = "https://public.bybit.com/spot/{symbol}/{symbol}_{date}.csv.gz"

# Headline definitions. T is a five-minute UTC boundary in Unix milliseconds.
WIDE_START_MS = -300
WIDE_END_MS = 800
PRE_START_MS = -300
PRE_END_MS = -200
POST_START_MS = 0
POST_END_MS = 100
BALANCE_THRESHOLDS = (0.50, 0.70, 0.80, 0.90, 0.95, 0.99)
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Trade:
    trade_id: int
    timestamp: int
    price: float
    volume: float
    side: str
    rpi: int
    date: str

    @property
    def notional(self) -> float:
        return self.price * self.volume


@dataclass
class Leg:
    rows: List[Trade]

    @property
    def sides(self) -> set[str]:
        return {row.side for row in self.rows}

    @property
    def volume(self) -> float:
        return math.fsum(row.volume for row in self.rows)

    @property
    def notional(self) -> float:
        return math.fsum(row.notional for row in self.rows)

    @property
    def vwap(self) -> float:
        return self.notional / self.volume if self.volume else math.nan

    @property
    def side_label(self) -> str:
        return "+".join(sorted(self.sides)) if self.rows else ""


@dataclass
class BoundaryEvent:
    boundary_ms: int
    pre: Leg
    post: Leg
    last_before_boundary: bool
    first_after_boundary: bool

    @property
    def both_legs(self) -> bool:
        return bool(self.pre.rows and self.post.rows)

    @property
    def opposite_homogeneous_sides(self) -> bool:
        return (
            self.both_legs
            and len(self.pre.sides) == 1
            and len(self.post.sides) == 1
            and self.pre.sides != self.post.sides
        )

    @property
    def balance_ratio(self) -> float:
        if not self.both_legs or not self.pre.notional or not self.post.notional:
            return math.nan
        return min(self.pre.notional, self.post.notional) / max(
            self.pre.notional, self.post.notional
        )


def date_strings(start: dt.date = START_DATE, end: dt.date = END_DATE) -> List[str]:
    dates = []
    day = start
    while day <= end:
        dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    return dates


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, path: Path, retries: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "DNI-SUPRA-research/1.0"})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(payload)
            temporary.replace(path)
            return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to download {url}: {last_error}")


def load_symbol(
    symbol: str, cache_dir: Path, offline: bool
) -> Tuple[List[Trade], List[dict]]:
    trades: List[Trade] = []
    manifest_rows: List[dict] = []
    seen_ids: set[Tuple[str, int]] = set()

    for date in date_strings():
        url = BASE_URL.format(symbol=symbol, date=date)
        path = cache_dir / symbol / f"{symbol}_{date}.csv.gz"
        if not path.exists():
            if offline:
                raise FileNotFoundError(f"offline cache miss: {path}")
            download_file(url, path)

        file_rows = 0
        first_id = None
        last_id = None
        previous_timestamp = None
        timestamps_nondecreasing = True
        ids_unique = True
        timestamp_dates_match = True

        with gzip.open(path, "rt", newline="") as handle:
            reader = csv.DictReader(handle)
            expected = {"id", "timestamp", "price", "volume", "side", "rpi"}
            if set(reader.fieldnames or ()) != expected:
                raise ValueError(f"unexpected columns in {path}: {reader.fieldnames}")
            for source_row in reader:
                trade = Trade(
                    trade_id=int(source_row["id"]),
                    timestamp=int(source_row["timestamp"]),
                    price=float(source_row["price"]),
                    volume=float(source_row["volume"]),
                    side=source_row["side"].lower(),
                    rpi=int(source_row["rpi"]),
                    date=date,
                )
                if trade.side not in {"buy", "sell"}:
                    raise ValueError(f"unexpected side {trade.side!r} in {path}")
                key = (date, trade.trade_id)
                if key in seen_ids:
                    ids_unique = False
                seen_ids.add(key)
                if previous_timestamp is not None and trade.timestamp < previous_timestamp:
                    timestamps_nondecreasing = False
                previous_timestamp = trade.timestamp
                utc_date = dt.datetime.fromtimestamp(
                    trade.timestamp / 1000, tz=dt.timezone.utc
                ).date().isoformat()
                timestamp_dates_match &= utc_date == date
                file_rows += 1
                first_id = trade.trade_id if first_id is None else first_id
                last_id = trade.trade_id
                trades.append(trade)

        manifest_rows.append(
            {
                "symbol": symbol,
                "date": date,
                "url": url,
                "sha256": sha256_file(path),
                "compressed_bytes": path.stat().st_size,
                "rows": file_rows,
                "first_id": first_id,
                "last_id": last_id,
                "ids_unique": str(ids_unique).lower(),
                "timestamps_nondecreasing": str(timestamps_nondecreasing).lower(),
                "timestamp_dates_match_utc": str(timestamp_dates_match).lower(),
            }
        )

    trades.sort(key=lambda row: (row.timestamp, row.trade_id))
    return trades, manifest_rows


def slice_rows(
    rows: Sequence[Trade], timestamps: Sequence[int], start: int, end: int
) -> List[Trade]:
    return list(
        rows[bisect.bisect_left(timestamps, start) : bisect.bisect_left(timestamps, end)]
    )


def boundary_range(rows: Sequence[Trade], cadence_ms: int = 300_000) -> range:
    """Return anchors observed by the dated archives, not by trade activity.

    The first anchor is excluded because its pre-boundary band precedes the
    archive window.  A quiet start or end of a symbol's tape must not remove
    otherwise observable zero-hit anchors from the denominator.
    """
    dates = [dt.date.fromisoformat(row.date) for row in rows]
    start_ms = (min(dates) - dt.date(1970, 1, 1)).days * DAY_MS
    end_ms = ((max(dates) + dt.timedelta(days=1)) - dt.date(1970, 1, 1)).days * DAY_MS
    first = (start_ms // cadence_ms + 1) * cadence_ms
    return range(first, end_ms, cadence_ms)


def build_boundary_events(
    rows: Sequence[Trade],
    phase_seconds: int = 0,
    cadence_ms: int = 300_000,
    pre_start_ms: int = PRE_START_MS,
    pre_end_ms: int = PRE_END_MS,
    post_start_ms: int = POST_START_MS,
    post_end_ms: int = POST_END_MS,
) -> Tuple[List[BoundaryEvent], int]:
    timestamps = [row.timestamp for row in rows]
    events: List[BoundaryEvent] = []
    possible = 0
    for base_boundary in boundary_range(rows, cadence_ms):
        boundary = base_boundary + phase_seconds * 1000
        possible += 1
        pre_rows = slice_rows(
            rows, timestamps, boundary + pre_start_ms, boundary + pre_end_ms
        )
        post_rows = slice_rows(
            rows, timestamps, boundary + post_start_ms, boundary + post_end_ms
        )
        insertion = bisect.bisect_left(timestamps, boundary)
        last_timestamp = rows[insertion - 1].timestamp if insertion else None
        first_timestamp = rows[insertion].timestamp if insertion < len(rows) else None
        events.append(
            BoundaryEvent(
                boundary_ms=boundary,
                pre=Leg(pre_rows),
                post=Leg(post_rows),
                last_before_boundary=bool(
                    pre_rows and max(row.timestamp for row in pre_rows) == last_timestamp
                ),
                first_after_boundary=bool(
                    post_rows and min(row.timestamp for row in post_rows) == first_timestamp
                ),
            )
        )
    return events, possible


def count_wide_hits(
    rows: Sequence[Trade], phase_seconds: int = 0, cadence_ms: int = 300_000
) -> Tuple[int, int]:
    timestamps = [row.timestamp for row in rows]
    hits = 0
    possible = 0
    for base_boundary in boundary_range(rows, cadence_ms):
        boundary = base_boundary + phase_seconds * 1000
        possible += 1
        left = bisect.bisect_left(timestamps, boundary + WIDE_START_MS)
        right = bisect.bisect_left(timestamps, boundary + WIDE_END_MS)
        hits += right > left
    return hits, possible


def event_counts(events: Sequence[BoundaryEvent]) -> dict:
    both = [event for event in events if event.both_legs]
    opposite = [event for event in both if event.opposite_homogeneous_sides]
    counts = {
        "core_populated_boundaries": sum(
            bool(event.pre.rows or event.post.rows) for event in events
        ),
        "core_both_legs_boundaries": len(both),
        "opposite_taker_side_boundaries": len(opposite),
    }
    for threshold in BALANCE_THRESHOLDS:
        label = int(round(threshold * 100))
        counts[f"balanced_{label}_boundaries"] = sum(
            event.balance_ratio >= threshold for event in opposite
        )
    return counts


def second_rows(symbol: str, rows: Sequence[Trade]) -> List[dict]:
    total_notional = math.fsum(row.notional for row in rows)
    output = []
    for second in range(60):
        selected = [row for row in rows if (row.timestamp // 1000) % 60 == second]
        notional = math.fsum(row.notional for row in selected)
        output.append(
            {
                "symbol": symbol,
                "second": second,
                "trades": len(selected),
                "trade_share": len(selected) / len(rows),
                "buy_trades": sum(row.side == "buy" for row in selected),
                "sell_trades": sum(row.side == "sell" for row in selected),
                "notional": notional,
                "notional_share": notional / total_notional,
            }
        )
    return output


def is_core_trade(timestamp: int) -> bool:
    offset = timestamp % 300_000
    return 299_700 <= offset < 299_800 or 0 <= offset < 100


def daily_rows(symbol: str, rows: Sequence[Trade]) -> List[dict]:
    output = []
    all_events, _ = build_boundary_events(rows)
    for date in date_strings():
        selected = [row for row in rows if row.date == date]
        core = [row for row in selected if is_core_trade(row.timestamp)]
        second_pair = [
            row for row in selected if (row.timestamp // 1000) % 60 in {59, 0}
        ]
        day_events = [
            event
            for event in all_events
            if dt.datetime.fromtimestamp(
                event.boundary_ms / 1000, tz=dt.timezone.utc
            ).date().isoformat()
            == date
        ]
        possible_day_boundaries = len(day_events)
        counts = event_counts(day_events) if day_events else event_counts([])
        notional = math.fsum(row.notional for row in selected)
        core_notional = math.fsum(row.notional for row in core)
        output.append(
            {
                "symbol": symbol,
                "date": date,
                "trades": len(selected),
                "notional": notional,
                "seconds_59_00_trades": len(second_pair),
                "seconds_59_00_share": len(second_pair) / len(selected),
                "core_trades": len(core),
                "core_trade_share": len(core) / len(selected),
                "core_notional": core_notional,
                "core_notional_share": (
                    core_notional / notional if notional else math.nan
                ),
                "possible_five_minute_boundaries": possible_day_boundaries,
                **counts,
            }
        )
    return output


def source_window_summary(symbol: str, rows: Sequence[Trade], label: str) -> dict:
    events, possible = build_boundary_events(rows)
    counts = event_counts(events)
    wide_hits, wide_possible = count_wide_hits(rows)
    if possible != wide_possible:
        raise AssertionError((symbol, possible, wide_possible))
    seconds_pair = sum((row.timestamp // 1000) % 60 in {59, 0} for row in rows)
    core = [row for row in rows if is_core_trade(row.timestamp)]
    pre_offsets = [
        row.timestamp % 300_000 - 300_000
        for row in rows
        if 299_700 <= row.timestamp % 300_000 < 299_800
    ]
    post_offsets = [
        row.timestamp % 300_000
        for row in rows
        if 0 <= row.timestamp % 300_000 < 100
    ]
    size_counts: Dict[float, int] = {}
    for row in rows:
        size_counts[row.volume] = size_counts.get(row.volume, 0) + 1
    top_size_count = max(size_counts.values(), default=0)
    opposite = [event for event in events if event.opposite_homogeneous_sides]
    balanced = [event for event in opposite if event.balance_ratio >= 0.80]
    balanced_rows = [row for event in balanced for row in event.pre.rows + event.post.rows]
    balanced_buy = math.fsum(
        row.notional for row in balanced_rows if row.side == "buy"
    )
    balanced_sell = math.fsum(
        row.notional for row in balanced_rows if row.side == "sell"
    )
    balanced_buy_volume = math.fsum(
        row.volume for row in balanced_rows if row.side == "buy"
    )
    balanced_sell_volume = math.fsum(
        row.volume for row in balanced_rows if row.side == "sell"
    )
    conservative_matched_notional = math.fsum(
        2 * min(event.pre.notional, event.post.notional) for event in balanced
    )
    balanced_same_price = sum(
        abs(event.post.vwap / event.pre.vwap - 1) * 10_000 <= 0.1
        for event in balanced
    )
    return {
        "window": label,
        "symbol": symbol,
        "start_date": rows[0].date,
        "end_date": rows[-1].date,
        "trades": len(rows),
        "notional": math.fsum(row.notional for row in rows),
        "rpi_trades": sum(row.rpi != 0 for row in rows),
        "seconds_59_00_trades": seconds_pair,
        "seconds_59_00_share": seconds_pair / len(rows),
        "core_trades": len(core),
        "core_trade_share": len(core) / len(rows),
        "core_notional": math.fsum(row.notional for row in core),
        "core_notional_share": math.fsum(row.notional for row in core)
        / math.fsum(row.notional for row in rows),
        "possible_five_minute_boundaries": possible,
        "wide_boundary_hits": wide_hits,
        "wide_boundary_hit_share": wide_hits / possible,
        **counts,
        "balanced_80_rows": len(balanced_rows),
        "balanced_80_notional": math.fsum(row.notional for row in balanced_rows),
        "balanced_80_conservative_matched_notional": conservative_matched_notional,
        "balanced_80_buy_notional": balanced_buy,
        "balanced_80_sell_notional": balanced_sell,
        "balanced_80_buy_sell_difference_pct": (
            abs(balanced_buy - balanced_sell) / ((balanced_buy + balanced_sell) / 2)
            * 100
            if balanced
            else math.nan
        ),
        "balanced_80_buy_volume": balanced_buy_volume,
        "balanced_80_sell_volume": balanced_sell_volume,
        "balanced_80_buy_sell_volume_difference_pct": (
            abs(balanced_buy_volume - balanced_sell_volume)
            / ((balanced_buy_volume + balanced_sell_volume) / 2)
            * 100
            if balanced
            else math.nan
        ),
        "balanced_80_same_price_within_0_1bp_share": (
            balanced_same_price / len(balanced) if balanced else math.nan
        ),
        "balanced_80_last_before_share": (
            sum(event.last_before_boundary for event in balanced) / len(balanced)
            if balanced
            else math.nan
        ),
        "balanced_80_first_after_share": (
            sum(event.first_after_boundary for event in balanced) / len(balanced)
            if balanced
            else math.nan
        ),
        "balance_ratio_p10": percentile(
            [event.balance_ratio for event in opposite], 0.10
        ),
        "balance_ratio_median": percentile(
            [event.balance_ratio for event in opposite], 0.50
        ),
        "balance_ratio_p90": percentile(
            [event.balance_ratio for event in opposite], 0.90
        ),
        "pre_offset_ms_p10": percentile(pre_offsets, 0.10),
        "pre_offset_ms_median": percentile(pre_offsets, 0.50),
        "pre_offset_ms_p90": percentile(pre_offsets, 0.90),
        "post_offset_ms_p10": percentile(post_offsets, 0.10),
        "post_offset_ms_median": percentile(post_offsets, 0.50),
        "post_offset_ms_p90": percentile(post_offsets, 0.90),
        "top_exact_size_trade_share": top_size_count / len(rows),
    }


def event_output_rows(events: Sequence[BoundaryEvent]) -> List[dict]:
    output = []
    for event in events:
        if not event.both_legs:
            continue
        timestamp = dt.datetime.fromtimestamp(
            event.boundary_ms / 1000, tz=dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        output.append(
            {
                "boundary_utc": timestamp,
                "pre_rows": len(event.pre.rows),
                "post_rows": len(event.post.rows),
                "pre_side": event.pre.side_label,
                "post_side": event.post.side_label,
                "pre_first_offset_ms": min(
                    row.timestamp - event.boundary_ms for row in event.pre.rows
                ),
                "pre_last_offset_ms": max(
                    row.timestamp - event.boundary_ms for row in event.pre.rows
                ),
                "post_first_offset_ms": min(
                    row.timestamp - event.boundary_ms for row in event.post.rows
                ),
                "post_last_offset_ms": max(
                    row.timestamp - event.boundary_ms for row in event.post.rows
                ),
                "pre_volume": event.pre.volume,
                "post_volume": event.post.volume,
                "pre_notional": event.pre.notional,
                "post_notional": event.post.notional,
                "pre_vwap": event.pre.vwap,
                "post_vwap": event.post.vwap,
                "balance_ratio": event.balance_ratio,
                "opposite_homogeneous_taker_sides": str(
                    event.opposite_homogeneous_sides
                ).lower(),
                "balanced_80": str(
                    event.opposite_homogeneous_sides and event.balance_ratio >= 0.80
                ).lower(),
                "balanced_90": str(
                    event.opposite_homogeneous_sides and event.balance_ratio >= 0.90
                ).lower(),
                "balanced_95": str(
                    event.opposite_homogeneous_sides and event.balance_ratio >= 0.95
                ).lower(),
                "last_trade_before_boundary": str(event.last_before_boundary).lower(),
                "first_trade_after_boundary": str(event.first_after_boundary).lower(),
            }
        )
    return output


def phase_profile(rows: Sequence[Trade]) -> List[dict]:
    output = []
    for phase in range(300):
        events, possible = build_boundary_events(rows, phase_seconds=phase)
        hits, wide_possible = count_wide_hits(rows, phase_seconds=phase)
        if possible != wide_possible:
            raise AssertionError((phase, possible, wide_possible))
        output.append(
            {
                "phase_seconds_after_five_minute_boundary": phase,
                "possible_anchors": possible,
                "wide_window_hits": hits,
                "wide_window_hit_share": hits / possible,
                **event_counts(events),
                "wide_window_overlaps_target_boundary": str(phase in {0, 299}).lower(),
            }
        )
    return output


def offset_histogram(symbol: str, rows: Sequence[Trade]) -> List[dict]:
    total = len(rows)
    counts: Dict[int, int] = {}
    # Relative to the nearest prior five-minute boundary, retaining only the
    # 1.3-second display window around the next boundary.
    for row in rows:
        offset = row.timestamp % 300_000
        signed = offset - 300_000 if offset >= 299_500 else offset
        if -500 <= signed < 800:
            bin_start = int(math.floor(signed / 10) * 10)
            counts[bin_start] = counts.get(bin_start, 0) + 1
    return [
        {
            "symbol": symbol,
            "offset_bin_start_ms": start,
            "offset_bin_end_ms": start + 10,
            "trades": counts.get(start, 0),
            "share_of_all_trades": counts.get(start, 0) / total,
        }
        for start in range(-500, 800, 10)
    ]


def sensitivity_rows(rows: Sequence[Trade]) -> List[dict]:
    output = []
    # Window widths are centred on the observed -260ms and +40ms peaks.
    for width in (20, 50, 100, 150, 200):
        half = width // 2
        events, possible = build_boundary_events(
            rows,
            pre_start_ms=-260 - half,
            pre_end_ms=-260 + (width - half),
            post_start_ms=40 - half,
            post_end_ms=40 + (width - half),
        )
        output.append(
            {
                "test": "peak_window_width",
                "parameter": f"{width}ms_each_leg",
                "possible_anchors": possible,
                **event_counts(events),
            }
        )
    events, possible = build_boundary_events(rows)
    opposite = [event for event in events if event.opposite_homogeneous_sides]
    for threshold in BALANCE_THRESHOLDS:
        output.append(
            {
                "test": "balance_threshold",
                "parameter": f"min_max_ratio_gte_{threshold:.2f}",
                "possible_anchors": possible,
                "core_populated_boundaries": "",
                "core_both_legs_boundaries": len(
                    [event for event in events if event.both_legs]
                ),
                "opposite_taker_side_boundaries": len(opposite),
                "matching_boundaries": sum(
                    event.balance_ratio >= threshold for event in opposite
                ),
            }
        )
    for cadence_minutes in (1, 5, 10, 15, 30, 60):
        cadence_ms = cadence_minutes * 60_000
        cadence_events, possible = build_boundary_events(rows, cadence_ms=cadence_ms)
        hits, wide_possible = count_wide_hits(rows, cadence_ms=cadence_ms)
        if possible != wide_possible:
            raise AssertionError((cadence_minutes, possible, wide_possible))
        output.append(
            {
                "test": "utc_boundary_cadence",
                "parameter": f"{cadence_minutes}min",
                "possible_anchors": possible,
                "wide_window_hits": hits,
                **event_counts(cadence_events),
            }
        )
    return output


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            {
                key: format(value, ".10g") if isinstance(value, float) else value
                for key, value in row.items()
            }
            for row in rows
        )


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def generate_charts(output_dir: Path) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mtick
    except ImportError as error:
        raise RuntimeError(
            "matplotlib is required for charts; rerun with --skip-charts or install it"
        ) from error

    colors = {"SUPRAUSDT": "#3157A4", "CITYUSDT": "#D47B29", "PYBOBOUSDT": "#2C8C74"}
    labels = {"SUPRAUSDT": "SUPRA/USDT", "CITYUSDT": "CITY/USDT", "PYBOBOUSDT": "PYBOBO/USDT"}
    plt.rcParams.update(
        {
            "font.size": 11,
            "axes.titlesize": 17,
            "axes.labelsize": 12,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )

    seconds = read_csv(output_dir / "second_of_minute.csv")
    fig, ax = plt.subplots(figsize=(12, 6.6), dpi=140)
    for symbol in SYMBOLS:
        selected = [row for row in seconds if row["symbol"] == symbol]
        x = [int(row["second"]) for row in selected]
        y = [float(row["trade_share"]) for row in selected]
        if symbol == TARGET:
            ax.bar(x, y, color=colors[symbol], width=0.78, alpha=0.84, label=labels[symbol])
        else:
            ax.plot(x, y, color=colors[symbol], linewidth=1.8, label=labels[symbol])
    ax.axhline(1 / 60, color="#555555", linestyle="--", linewidth=1.2, label="Uniform 1/60")
    ax.set_title("SUPRA trades concentrate on the two sides of the minute boundary")
    ax.set_xlabel("Second of the minute (UTC)")
    ax.set_ylabel("Share of executed trades")
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    ax.set_xlim(-0.8, 59.8)
    ax.set_xticks(range(0, 60, 5))
    ax.grid(axis="y", alpha=0.18)
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir.parent.parent / "supra-second-of-minute.png", bbox_inches="tight")
    plt.close(fig)

    offsets = read_csv(output_dir / "boundary_offset_10ms.csv")
    fig, ax = plt.subplots(figsize=(12, 6.6), dpi=140)
    for symbol in SYMBOLS:
        selected = [row for row in offsets if row["symbol"] == symbol]
        x = [int(row["offset_bin_start_ms"]) + 5 for row in selected]
        y = [float(row["share_of_all_trades"]) for row in selected]
        ax.plot(
            x,
            y,
            color=colors[symbol],
            linewidth=2.2 if symbol == TARGET else 1.5,
            label=labels[symbol],
        )
    ax.axvline(0, color="#555555", linestyle="--", linewidth=1.1)
    ax.axvspan(-300, -200, color="#3157A4", alpha=0.08)
    ax.axvspan(0, 100, color="#3157A4", alpha=0.08)
    ax.set_title("Two millisecond-scale print peaks straddle each five-minute boundary")
    ax.set_xlabel("Milliseconds from a five-minute UTC boundary")
    ax.set_ylabel("Share of all trades per 10ms bin")
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    ax.set_xlim(-500, 800)
    ax.grid(axis="y", alpha=0.18)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir.parent.parent / "supra-boundary-offset.png", bbox_inches="tight")
    plt.close(fig)

    daily = read_csv(output_dir / "daily_timing.csv")
    phases = read_csv(output_dir / "phase_placebo.csv")
    import matplotlib.dates as mdates

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(12, 9), dpi=140)
    for symbol in SYMBOLS:
        selected = [row for row in daily if row["symbol"] == symbol]
        x = [dt.date.fromisoformat(row["date"]) for row in selected]
        y = [float(row["core_trade_share"]) for row in selected]
        top.plot(
            x,
            y,
            marker="o" if symbol == TARGET else None,
            markersize=3,
            linewidth=2 if symbol == TARGET else 1.4,
            color=colors[symbol],
            label=labels[symbol],
        )
    top.set_title("The boundary concentration persists on all 30 days")
    top.set_ylabel("Trades in the two 100ms bands")
    top.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    top.xaxis.set_major_locator(mdates.DayLocator(interval=4))
    top.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    top.grid(axis="y", alpha=0.18)
    top.legend(frameon=False, ncol=3)

    phase_x = [int(row["phase_seconds_after_five_minute_boundary"]) for row in phases]
    phase_y = [int(row["core_both_legs_boundaries"]) for row in phases]
    bottom.bar(phase_x, phase_y, color="#3157A4", width=1.0)
    bottom.set_yscale("symlog", linthresh=1)
    bottom.set_title("Paired-event counts collapse at every off-boundary phase")
    bottom.set_xlabel("Placebo phase (seconds after each five-minute boundary)")
    bottom.set_ylabel("Boundaries with both 100ms legs (symlog)")
    bottom.set_xlim(-1, 300)
    bottom.grid(axis="y", alpha=0.18)
    fig.tight_layout(h_pad=2.0)
    fig.savefig(output_dir.parent.parent / "supra-boundary-robustness.png", bbox_inches="tight")
    plt.close(fig)


def verify_outputs(output_dir: Path) -> None:
    summary = read_csv(output_dir / "summary.csv")
    full = {
        row["symbol"]: row
        for row in summary
        if row["window"] == "full_2026-06-15_to_2026-07-14"
    }
    expected = {
        "SUPRAUSDT": (86851, 20611, 8313, 7932, 3273),
        "CITYUSDT": (79479, 2698, 67, 2, 0),
        "PYBOBOUSDT": (66742, 2190, 225, 0, 0),
    }
    for symbol, values in expected.items():
        row = full[symbol]
        observed = (
            int(row["trades"]),
            int(row["seconds_59_00_trades"]),
            int(row["wide_boundary_hits"]),
            int(row["core_both_legs_boundaries"]),
            int(row["balanced_80_boundaries"]),
        )
        if observed != values:
            raise AssertionError(f"{symbol}: expected {values}, observed {observed}")

    validation = {
        row["symbol"]: row
        for row in summary
        if row["window"] == "pre_discovery_dates_2026-06-15_to_2026-07-11"
    }
    if int(validation["SUPRAUSDT"]["wide_boundary_hits"]) != 7453:
        raise AssertionError("pre-discovery-date SUPRA boundary-hit count changed")
    if int(validation["SUPRAUSDT"]["balanced_80_boundaries"]) != 2858:
        raise AssertionError("pre-discovery-date SUPRA balanced-event count changed")

    phases = read_csv(output_dir / "phase_placebo.csv")
    phase_zero = next(
        row for row in phases if row["phase_seconds_after_five_minute_boundary"] == "0"
    )
    if int(phase_zero["core_both_legs_boundaries"]) != 7932:
        raise AssertionError("phase-zero paired-event count changed")
    nonoverlapping = [
        row
        for row in phases
        if 1 <= int(row["phase_seconds_after_five_minute_boundary"]) <= 298
    ]
    if max(int(row["core_both_legs_boundaries"]) for row in nonoverlapping) != 3:
        raise AssertionError("placebo paired-event maximum changed")
    if max(int(row["balanced_80_boundaries"]) for row in nonoverlapping) != 1:
        raise AssertionError("placebo balanced-event maximum changed")

    manifest = read_csv(output_dir / "source_manifest.csv")
    if len(manifest) != 90:
        raise AssertionError(f"expected 90 source files, found {len(manifest)}")
    for row in manifest:
        for key in (
            "ids_unique",
            "timestamps_nondecreasing",
            "timestamp_dates_match_utc",
        ):
            if row[key] != "true":
                raise AssertionError(f"source-integrity failure: {row['symbol']} {row['date']} {key}")


def verify_pinned_sources(expected: Sequence[dict], observed: Sequence[dict]) -> None:
    """Fail if a dated public archive differs from the committed manifest."""
    expected_by_key = {(row["symbol"], row["date"]): row for row in expected}
    observed_by_key = {(row["symbol"], row["date"]): row for row in observed}
    if expected_by_key.keys() != observed_by_key.keys():
        raise AssertionError("source-manifest symbol/date set changed")
    for key, expected_row in expected_by_key.items():
        observed_row = observed_by_key[key]
        for field in ("sha256", "compressed_bytes", "rows"):
            if str(expected_row[field]) != str(observed_row[field]):
                raise AssertionError(
                    f"pinned source changed for {key[0]} {key[1]}: "
                    f"{field} {expected_row[field]} -> {observed_row[field]}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("BYBIT_CACHE", ".raw-cache")),
        help="raw gzip cache (default: .raw-cache or BYBIT_CACHE)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory for processed CSVs (default: script directory)",
    )
    parser.add_argument("--offline", action="store_true", help="never download cache misses")
    parser.add_argument("--skip-charts", action="store_true")
    parser.add_argument("--verify", action="store_true", help="assert all headline values")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pinned_manifest_path = Path(__file__).resolve().parent / "source_manifest.csv"
    pinned_manifest = (
        read_csv(pinned_manifest_path)
        if args.verify and pinned_manifest_path.exists()
        else None
    )
    all_rows: Dict[str, List[Trade]] = {}
    manifest: List[dict] = []
    for symbol in SYMBOLS:
        rows, source_rows = load_symbol(symbol, args.cache_dir, args.offline)
        all_rows[symbol] = rows
        manifest.extend(source_rows)

    if pinned_manifest is not None:
        verify_pinned_sources(pinned_manifest, manifest)

    summaries: List[dict] = []
    for symbol, rows in all_rows.items():
        summaries.append(
            source_window_summary(
                symbol, rows, "full_2026-06-15_to_2026-07-14"
            )
        )
        validation = [
            row for row in rows if dt.date.fromisoformat(row.date) < DISCOVERY_START
        ]
        summaries.append(
            source_window_summary(
                symbol, validation, "pre_discovery_dates_2026-06-15_to_2026-07-11"
            )
        )

    write_csv(args.output_dir / "source_manifest.csv", manifest)
    write_csv(args.output_dir / "summary.csv", summaries)
    write_csv(
        args.output_dir / "second_of_minute.csv",
        [row for symbol, rows in all_rows.items() for row in second_rows(symbol, rows)],
    )
    write_csv(
        args.output_dir / "daily_timing.csv",
        [row for symbol, rows in all_rows.items() for row in daily_rows(symbol, rows)],
    )
    target_events, _ = build_boundary_events(all_rows[TARGET])
    write_csv(args.output_dir / "boundary_events.csv", event_output_rows(target_events))
    write_csv(args.output_dir / "phase_placebo.csv", phase_profile(all_rows[TARGET]))
    write_csv(
        args.output_dir / "boundary_offset_10ms.csv",
        [
            row
            for symbol, rows in all_rows.items()
            for row in offset_histogram(symbol, rows)
        ],
    )
    write_csv(args.output_dir / "sensitivity.csv", sensitivity_rows(all_rows[TARGET]))

    if not args.skip_charts:
        generate_charts(args.output_dir)
    if args.verify:
        verify_outputs(args.output_dir)


if __name__ == "__main__":
    main()
