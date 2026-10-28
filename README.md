# Crypto micro-cap screener with a pre-registered forward test

An automated screener for crypto micro-caps ($5-150M market cap) that runs on
GitHub Actions twice a week, keeps a paper portfolio, and tests its own
selection rules against a **random-choice control** in a tournament whose rules
were frozen before the data came in.

No real money is involved. The point of the project is measurement: does any
rule pick coins better than chance once you test it honestly?

Author: Oleksandr Simonenko (Applied AI student, Noroff; background in banking
and crypto markets).

## Result so far (28.10.2026, 12 complete blocks, verdict date 15.11.2026)

| Track | Rule ("sieve") | Closed | Mean PnL | Median PnL | Winners |
|---|---|---|---|---|---|
| A1 | crowded shorts: funding between -0.01 and -0.0005 | 56 | +11.8% | +9.2% | 34/56 |
| A2 | quiet price + high volume/market cap | 60 | +57.4% | +2.1% | 33/60 |
| A3 | low float (circulating/total <= 0.35) | 60 | +17.2% | +1.8% | 31/60 |
| A4 | old sieve: buyback flag | 60 | +5.5% | +3.6% | 32/60 |
| A5 | already rising (+20..60% in 30 days) | 60 | +12.6% | -1.5% | 29/60 |
| **A7** | **random choice (control)** | 57 | +13.8% | **+14.2%** | 33/57 |
| A0 | main scoring portfolio (separate threshold) | 38 | +6.0% | | |

Iman-Davenport test over the 12 blocks: F = 0.657, **p = 0.66**.
Mean ranks: A2 3.92, A7 3.83, A1 3.58, A3 3.50, A5 3.50, A4 2.67.

**Current verdict: no sieve beats random selection.** A2's high mean comes from
one or two outliers (median +2.1%); the random control has the best median.
A negative result, reached with a method that could have shown a positive one.

## How the test is built

- **Pre-registration.** `screener_tournament_rules_v1.md` and
  `tournament_rules_addendum_1.md` fixed the metrics, the winner criterion and
  the statistical test before the first position closed. The rules file is
  hashed (`rules_hash.py`) so any later edit is detectable. Freeze until
  15.11.2026.
- **Control group.** Track A7 buys random coins from the same universe on the
  same days. A rule must beat A7 and the main portfolio A0 by at least
  5 percentage points of mean PnL, with at least 30 closed positions, and lead
  in both halves of the period.
- **Blocked design.** Each run is a block; tracks are ranked inside each block
  and compared with the Friedman / Iman-Davenport test, then Holm-corrected
  pairwise tests against A7 (`stats_core.py`, `verdict_1511.py`).
- **Costs.** Fees, spread and slippage are modelled per venue (`costs.py`,
  `costs.yaml`) before PnL is counted.
- **Provenance.** Every artifact records the code version and inputs that made
  it (`provenance.py`); sanity checks run on every scan (`sanity.py`).

## Pipeline

| Workflow (`ci/workflows/`) | Schedule | What it does |
|---|---|---|
| `screener.yml` | Mon, Thu 06:17 UTC | Universe from CoinGecko -> funding (Binance, Bybit, MEXC) -> revenue (DefiLlama) -> scoring -> paper portfolio -> tournament tracks -> Telegram report |
| `funding.yml` | hourly | Perpetual funding snapshots |
| `metrics.yml`, `auditor.yml` | after scans | Metrics, independent audit of the run |
| `export.yml` | Fri 08:17 UTC | Weekly feed for a downstream DEX system |
| `unlocks.yml`, `autoconfig.yml` | scheduled | Token unlock log, config upkeep |
| `tests.yml` (active here) | on code change | 589 self-tests, forbidden-dependency check |

The production workflows run in a private repository with API secrets.
In this public copy they are reference files only; `tests.yml` is the one
active workflow.

## Engineering notes

- Python 3.12, standard library + `requests` + `PyYAML` only (checked in CI).
- Secrets never enter code or logs: `secrets_guard.py` masks tokens in every
  message, and `tests/test_secrets_leak.py` tests that.
- `python -m unittest discover -s tests -t .` runs the full suite (589 tests).
- Code comments are in Russian (the working language of the project).

## Data in this repository

`tournament.json` (all tracks and closed positions), `positions.json` (main
paper portfolio), `artifacts/` (latest verdict and metrics), `export/` (latest
weekly feed). Personal holdings and private research data are not included.

## Next step

The scan log (`candidates_log.jsonl`, ~1300 coins x 2 runs per week since
03.08.2026) is the labelled dataset for an ML ranking model, to be trained after
the tournament closes and tested against the same random control.
