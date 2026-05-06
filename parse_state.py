import json

with open('./output/paper_trader_state_live_1000.json', 'r') as f:
    data = json.load(f)

print('=== SUMMARY ===')
print(f"Cash: ${data['cash_usd']:.2f}")
print(f"Realized PnL: ${data['realized_pnl_usd']:.2f}")
print(f"Unrealized PnL: ${data['unrealized_pnl_usd']:.5f}")
equity = 1000 + data['realized_pnl_usd'] + data['unrealized_pnl_usd']
print(f"Equity: ${equity:.2f}")

print()
print('=== OPEN POSITIONS ===')
open_pos = [p for p in data['positions'] if p['status'] == 'open']
for i, p in enumerate(sorted(open_pos, key=lambda x: -x['holding_scans']), 1):
    pnl = p.get('realized_pnl_usd', 0) + p.get('unrealized_pnl_usd', 0)
    pnl_str = f'+{pnl:.2f}' if pnl >= 0 else f'{pnl:.2f}'
    signal = '✓' if p.get('signal_present', False) else '✗'
    tier = p.get('last_tier', 'unknown').replace('_', ' ')[:10]
    print(f"{i} | {p['underlying_key']:8} | {p['entry_exec_spread_bps']:8.2f} | {p['last_exec_spread_bps']:8.2f} | {pnl_str:>8} | {p['holding_scans']:5} | {tier:10} | {signal}")
