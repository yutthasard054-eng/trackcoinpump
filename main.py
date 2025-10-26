import asyncio
import websockets
import json
import requests
import time
from collections import defaultdict
from threading import Thread

# === CONFIG ===
MIN_BUY_SOL = 0.1
MIN_TRADES = 3
MIN_WIN_RATE = 0.6
MIN_ROI = 2.0
CHECK_INTERVAL_SEC = 900  # 15 minutes

# === STATE ===
WALLET_BUYS = defaultdict(list)  # wallet -> [{"token": mint, "sol": sol}]
ELITE_WALLETS = set()

def save_elite():
    with open("/tmp/elite_wallets.json", "w") as f:
        json.dump(list(ELITE_WALLETS), f)
    print(f"💾 Saved {len(ELITE_WALLETS)} elite wallets")

def get_trades(mint):
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except:
        return []

def score_wallets():
    global ELITE_WALLETS
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        print("🔍 Scoring wallets for profitability...")
        new_elite = set()
        
        for wallet, buys in WALLET_BUYS.items():
            if len(buys) < MIN_TRADES:
                continue
                
            wins = 0
            total = 0
            for buy in buys:
                mint = buy["token"]
                trades = get_trades(mint)
                sells = [t for t in trades if t["type"] == "sell" and t["user"] == wallet]
                if sells:
                    roi = sells[-1]["sol_amount"] / max(buy["sol"], 1e-9)
                    if roi >= MIN_ROI:
                        wins += 1
                    total += 1
            
            if total >= MIN_TRADES and (wins / total) >= MIN_WIN_RATE:
                new_elite.add(wallet)
                if wallet not in ELITE_WALLETS:
                    print(f"✅ ELITE WALLET PROMOTED: {wallet[:8]}... | Wins: {wins}/{total}")
        
        ELITE_WALLETS = new_elite
        save_elite()

async def main():
    # Start scoring thread
    Thread(target=score_wallets, daemon=True).start()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✅ Connected to PumpPortal")
                
                # Watch ALL new tokens
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                # Watch ALL trades (empty keys = global feed)
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if data.get("txType") == "create":
                            wallet = data["traderPublicKey"]
                            sol = data["solAmount"]
                            mint = data["mint"]
                            
                            # Track significant early buys
                            if sol >= MIN_BUY_SOL:
                                WALLET_BUYS[wallet].append({"token": mint, "sol": sol})
                                print(f"🛒 Tracking: {wallet[:8]}... | {sol} SOL | {mint[:8]}...")
                                
                    except Exception as e:
                        print(f"⚠️ Message error: {e}")
                        
        except Exception as e:
            print(f"💥 Connection error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
