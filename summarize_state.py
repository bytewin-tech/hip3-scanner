import json

with open('./output/paper_trader_state_live_1000.json') as f:
    state = json.load(f)

print('=== TOP LEVEL ===')
print(f"cash_usd: {state.get('cash_usd')}")
equity = state.get('equity_usd', state.get('cash_usd'))
print(f"equity_usd: {equity}")
print(f"open_positions count: {len(state.get('open_positions', []))}")
print(f"closed_positions count: {len(state.get('closed_positions', []))}")
print(f"safety_paused: {state.get('safety_paused')}")
print(f"api_stale_count: {state.get('api_stale_count')}")
print(f"consecutive_volatility_spikes: {state.get('consecutive_volatility_spikes')}")
print(f"peak_equity_usd: {state.get('peak_equity_usd')}")
print(f"start_cash: {state.get('start_cash_usd')}")

# Calculate realized from closed positions
realized = sum(p.get('realized_pnl_usd', 0) for p in state.get('closed_positions', []))
unrealized = sum(p.get('unrealized_pnl_usd', 0) for p in state.get('open_positions', []))
print(f"calculated realized: {realized}")
print(f"calculated unrealized: {unrealized}")

print()
print('=== OPEN POSITIONS ===')
for pos in state.get('open_positions', []):
    unreal = pos.get('unrealized_pnl_usd', 0)
    entry_bps = pos.get('entry_spread_bps', 0)
    curr_bps = pos.get('last_exec_spread_bps', 0)
    tier = pos.get('last_tier', 'unknown')
    scans = pos.get('holding_scans', 0)
    signal = pos.get('signal_present', False)
    notional = pos.get('notional_usd', 250)
    pnl_dollar = unreal
    print(f"{pos.get('underlying_key')} | entry_bps={entry_bps:.4f} | curr_bps={curr_bps:.4f} | pnl=${pnl_dollar:+.4f} | scans={scans} | tier={tier} | signal={signal}")
