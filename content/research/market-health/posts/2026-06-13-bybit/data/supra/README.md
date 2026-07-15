# SUPRA/USDT five-minute-boundary analysis

This directory contains the processed data and reproducible analysis behind the
SUPRA/USDT section of the Bybit market-health article. Raw archives are not
committed. `source_manifest.csv` pins all 90 dated inputs by URL and SHA-256.
The separate [`discovery/`](./discovery/) package pins the exact 80-pair search
cohort, 240 dated inputs and complete exploratory output that led to the SUPRA
follow-up, together with the original live-ticker snapshot limitation.

## Reproduce

Python 3.10 or later is sufficient for the calculations and tests. Chart
generation additionally needs Matplotlib 3.10 or later.

```bash
python3 -m unittest test_analyze.py
python3 -m pip install 'matplotlib>=3.10,<4'
python3 analyze.py --cache-dir /tmp/dni-bybit-raw --verify
```

Use `--offline` to prohibit downloads or `--skip-charts` to run without
Matplotlib. The script downloads public files from
`https://public.bybit.com/spot/<symbol>/`, validates the schema, unique daily
trade IDs, timestamp ordering and UTC filename date, then regenerates every CSV
and chart. Floating-point aggregation uses `math.fsum`, and output is normalised
to 10 significant digits so CSVs remain byte-stable across supported Python
versions.

## Definitions

- **Boundary hit:** at least one print in `[T-300ms, T+800ms)`, where `T` is a
  five-minute UTC boundary. This 1.1-second window is kept separate from the
  narrower paired-event definition.
- **Paired event:** at least one print in each of the data-informed 100ms bands
  `[T-300ms, T-200ms)` and `[T, T+100ms)`.
- **Opposite-side event:** a paired event whose pre and post legs each contain
  only one Bybit-reported taker side, and those sides differ.
- **Balanced event:** an opposite-side event whose smaller aggregate quote
  notional divided by its larger aggregate quote notional meets the stated
  threshold. The article's headline threshold is 80%; `sensitivity.csv` also
  reports 50%, 70%, 90%, 95% and 99%.

The initial discovery scan covered 80 low-turnover Bybit USDT pairs over
2026-07-12 through 2026-07-14. The earlier 27 days (2026-06-15 through
2026-07-11) are therefore reported separately as a pre-discovery-date temporal
robustness window. It is not a preregistered confirmatory sample because all 30
days were inspected while formalising the narrow bands. CITY/USDT and
PYBOBO/USDT are post-selected activity comparators, not labelled ground truth or
an unbiased control sample. The full SUPRA within-pair phase placebo tests all
300 one-second phases of a five-minute cycle; phase 299's wide hit window
overlaps the next boundary's pre-leg and is explicitly marked in the CSV. Phase
zero has 8,639 fully observed anchors because the opening pre-window precedes
the sample; phases 1-299 each have 8,640 because their shifted opening windows
fall inside the sample.

With the repository-default output directory, charts are written beside the
article. A custom `--output-dir` keeps both generated CSVs and charts inside
that directory, making temporary verification runs self-contained.

## Outputs

- `summary.csv`: headline results for the full and validation windows.
- `source_manifest.csv`: source URLs, hashes, sizes, row counts and integrity checks.
- `second_of_minute.csv`: per-second trade and notional distributions.
- `daily_timing.csv`: persistence and control comparison by UTC date.
- `boundary_events.csv`: all boundaries with both narrow legs, one row per boundary.
- `boundary_offset_10ms.csv`: data for the millisecond-offset chart.
- `phase_placebo.csv`: full 0-299 second within-pair placebo profile.
- `sensitivity.csv`: window-width, balance-threshold and cadence checks.

Bybit's official public-trade documentation defines `time` as trade time in
Unix milliseconds and `side` as the taker side, and links the historical trade
archives: <https://bybit-exchange.github.io/docs/v5/market/recent-trade>.
