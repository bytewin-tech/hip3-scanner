# HIP-3 Scanner MVP

Read-only Python scanner for Hyperliquid HIP-3 cross-DEX opportunities across:
`xyz`, `flx`, `vntl`, `hyna`, `km`, `abcd`, `cash`, `para`.

## Features
- Fetches live venue data from Hyperliquid `perpDexs` and `metaAndAssetCtxs`
- Normalizes venue-qualified markets into duplicate-underlying groups
- Scores cross-venue price and funding dislocations using `Decimal`
- Applies sanity filters for delisted, low-liquidity, missing-impact, and oracle-divergence cases
- Verifies shortlisted candidates with `l2Book`
- Prints ranked opportunities to the console and appends JSONL alerts to `output/`
- Optional $1,000 paper trader / PnL simulator with JSON state persistence
- Includes pytest coverage for normalization, scoring, and paper-trader state transitions

## Install
```bash
cd /Users/chiaclaw/Projects/hip3-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Run
Single scan:
```bash
hip3-scan once
```

Looping scan:
```bash
hip3-scan loop --interval 10 --top 20
```

Using a config file:
```bash
hip3-scan once --config config.example.json
```

With paper trader enabled:
```bash
hip3-scan once --paper
hip3-scan once --paper --paper-state ./output/paper_trader_state.json
```

## Paper trader rules
The simulator is intentionally simple and explicit.

### Starting portfolio
- Starting equity: **$1,000**
- Default state file: `./output/paper_trader_state.json`
- Default notional reserved per paired trade: **$250**
- Max simultaneous open positions: **3**
- Cash is reduced when a simulated trade is opened and released on close

### Entry rules
Open a simulated paired trade only when all of the following are true:
- opportunity tier is `heads_up` or `strong_candidate`
- raw score is at least **3.0**
- confidence is at least **0.55**
- executable spread is at least **2.0 bps**
- no existing open position for the same underlying

The scanner uses the current ranked opportunity direction directly:
- **buy venue** = long cheap venue
- **sell venue** = short rich venue

### Mark-to-market approximation
Each open position is marked using the newest executable spread for the same ranked opportunity.

Approximation used:
```text
unrealized_pnl_usd = (entry_exec_spread_bps - current_exec_spread_bps) / 10000 * notional_usd
```

Interpretation:
- spread **narrows/converges** -> positive PnL
- spread **widens** -> negative PnL

Funding spread is tracked in state for context, but the MVP PnL calculation only monetizes spread convergence/widening so the result stays simple and explainable.

### Exit rules
Close a simulated trade when any of these happens:
- executable spread converges to **0.5 bps or less**
- raw score falls below **1.0**
- opportunity drops out of the ranked scan output (`signal_missing`)
- tier deteriorates below `heads_up` / `strong_candidate`
- holding period reaches **6 scans**
- unrealized loss reaches **-2% of trade notional**

## Output
- Console table with top-ranked opportunities
- Paper portfolio summary when `--paper` is enabled:
  - starting equity
  - cash
  - equity
  - open positions count
  - realized PnL
  - unrealized PnL
  - total PnL
- JSONL file under `output/opportunities_YYYYMMDD.jsonl`
- Paper trader JSON state under `output/paper_trader_state.json` by default

## Config knobs
Paper-trader defaults can be overridden in `config.example.json` or your own JSON config:
- `paper_trader_enabled`
- `paper_initial_equity_usd`
- `paper_state_path`
- `paper_per_trade_notional_usd`
- `paper_max_open_positions`
- `paper_min_entry_score`
- `paper_min_entry_confidence`
- `paper_min_entry_exec_spread_bps`
- `paper_allowed_entry_tiers`
- `paper_close_exec_spread_bps`
- `paper_close_score_below`
- `paper_max_holding_scans`
- `paper_stop_loss_pct`

You can also enable paper mode with env / CLI:
```bash
HIP3_PAPER_TRADER_ENABLED=true hip3-scan once
hip3-scan once --paper
```

## Notes
- MVP remains read-only: no live order placement or exchange-side state
- `vntl` and `para` receive stricter confidence penalties due to specialized product profiles
- L2 checks are only performed for candidates above the initial executable-spread threshold
