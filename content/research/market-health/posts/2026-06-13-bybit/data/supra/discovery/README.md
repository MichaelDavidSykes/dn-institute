# SUPRA/USDT discovery scan

This directory makes the exploratory screen that surfaced SUPRA/USDT
re-runnable. It is a discovery record, not a manipulation classifier or a
preregistered test. The later 30-day boundary analysis is in the parent
directory.

## Search context and cohort

At approximately 2026-07-15 13:58 UTC, the original script requested Bybit's
live spot-ticker endpoint:

`https://api.bybit.com/v5/market/tickers?category=spot`

It retained symbols ending in `USDT` whose reported `turnover24h` was between
5,000 and 400,000 USDT inclusive, excluded `WCTUSDT`, `LMWRUSDT`, `CBKUSDT`,
`MCRTUSDT`, and `SOLUSDT` because those article cases/control were already
known, sorted ascending by turnover, and took the first 80. Presence in the
spot-ticker response was treated as active; instrument status was not queried
separately. `pair_selection.csv` pins the resulting 80 symbols, their recorded
24-hour turnover, low-turnover selection rank, and later heuristic score rank.

The ticker response itself was not retained. Consequently, the exact 80-pair
cohort is reproducible, but the mutable ticker universe at that instant—and in
particular the 81st eligible pair—cannot be reconstructed independently. The
recorded turnovers should not be interpreted as historical daily volume; they
are a live 24-hour snapshot used only to choose a thin-market search cohort.

For each selected pair, the scan read Bybit's public executed-trade dumps for
2026-07-12 through 2026-07-14. `input_manifest.csv` pins all 240 archive URLs,
compressed byte counts, and SHA-256 hashes. The hashes were captured
retrospectively later on 2026-07-15 rather than by the first exploratory run.
All 80 three-file compressed-byte totals match the totals recorded by that run,
and the pinned files reproduce every committed metric exactly; nevertheless,
the absence of original per-file hashes is a provenance limitation.

## Reproduce

Python 3.9+ and the standard library are sufficient. Raw archives are cached
outside the repository.

```bash
python3 scan.py \
  --cache-dir /tmp/dni-bybit-discovery \
  --output /tmp/dni-bybit-discovery-results.csv \
  --verify
```

After one online run, repeat without network access:

```bash
python3 scan.py \
  --cache-dir /tmp/dni-bybit-discovery \
  --offline \
  --output /tmp/dni-bybit-discovery-results.csv \
  --verify
```

`--verify` checks the 240 compressed archives against the manifest, checks the
pinned cohort, recomputes the scan, and compares all 80 rows with
`scan_results.csv`.

## Exploratory metrics

For each pair, the script parses timestamp, price, base size, and reported taker
side, then records:

- the shares of trades carried by the most common exact base size and the five
  most common sizes;
- the share on the busiest second of the minute and on the two busiest seconds;
- an L1 distance between the base-size first-digit distribution and Benford's
  distribution;
- the most common positive interarrival gap after rounding to 10 milliseconds
  (gaps above ten minutes are omitted);
- the three-day price range and quote notional; and
- recurrent exact sizes with at least `max(15, floor(0.2% of trades))` prints
  and at least 35% of their prints on each reported taker side.

The ad hoc prioritisation score was:

```text
6*top1_size_share + 3*top5_size_share
+ 7*max_second_share + 4*top_two_second_share
+ 0.5*benford_L1
+ min(2, max(0, price_ratio - 1))/3
+ 5*balanced_recurring_top5_share
```

This score has no calibrated null distribution, significance threshold, or
claim of market-health validity. It merely sorted heterogeneous timing and
repeat-size leads for manual inspection.

## Why SUPRA was followed up

`scan_results.csv` contains every pair, including higher-scoring repeat-size
and sparse-tape cases. SUPRA ranked 25th by the composite, not first. It was
manually selected because its two busiest seconds were adjacent across the
minute boundary (:00 and :59) and held 35.6% of its 5,309 trades, its leading
positive interarrival-gap bin was 300 ms, and its largest exact clip was only
2.1% of trades. That combination suggested a timing process distinct from the
recurring-clip cases already covered by the article and provided enough trades
for a deeper check. The broad scan did not itself test five-minute boundaries;
that relationship and the narrow bands were found during the subsequent,
explicitly data-informed analysis.

The `followup_candidate` column records this manual decision. It is not a claim
that SUPRA was uniquely anomalous, that the other 79 pairs were organic, or
that public trade data identify account ownership or intent.
