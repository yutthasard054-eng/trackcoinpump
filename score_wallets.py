import requests
from collections import defaultdict

# Reuse your WALLET_BUYS from main.py (or store to file)
WALLET_BUYS = defaultdict(list)  # Populate from your main.py

def get_token_trades(mint):
    try:
        return requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100").json()
    except:
        return []

def score_wallet(wallet):
    results = []
    for buy in WALLET_BUYS[wallet]:
        mint = buy["token"]
        trades = get_token_trades(mint)
        sells = [t for t in trades if t["type"] == "sell" and t["user"] == wallet]
        if sells:
            roi = sells[-1]["sol_amount"] / buy["sol"]
            results.append(roi)
    
    if len(results) < 3:
        return False
    
    win_rate = len([r for r in results if r >= 2.0]) / len(results)
    avg_roi = sum(results) / len(results)
    return win_rate >= 0.6 and avg_roi >= 3.0

# Check all tracked wallets
for wallet in WALLET_BUYS:
    if score_wallet(wallet):
        print(f"✅ ELITE WALLET: {wallet}")
