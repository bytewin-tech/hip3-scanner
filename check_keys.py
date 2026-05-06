import json

f = open('./output/paper_trader_state_live_1000.json')
d = json.load(f)
print('Keys:', list(d.keys())[:30])
print('Type of open_positions:', type(d.get('open_positions')))
print('Type of opportunities:', type(d.get('opportunities')))
print('Equity USD:', d.get('equity_usd'))
