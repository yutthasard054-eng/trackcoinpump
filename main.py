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
WALLET_BUYS = defaultdict(list)
ELITE_WALLETS = set()

def save_and_log_elite():
    # Save to file (for future trade executor)
    with open("/tmp/elite_wallets.json", "w") as f:
        json.dump(list(ELITE_WALLETS), f)
    
    # LOG TO CONSOLE (you'll see this in Railway Logs)
    if ELITE_WALLETS:
        print("🌟 ELITE WALLETS LIST:")
        for w in ELITE_WALLETS:
            print(f"  - {w}")
        print(f"✅ Total elite wallets: {len(ELITE_WALLETS)}")
    else:
        print("⏳ No elite wallets yet — still collecting data...")

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
        print("🔍 Scoring wallets for real profits...")
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
        
        # Only log if changed
        if new_elite != ELITE_WALLETS:
            ELITE_WALLETS = new_elite
            save_and_log_elite()
        else:
            print("📊 No change in elite wallet list.")

async def main():
    Thread(target=score_wallets, daemon=True).start()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✅ Connected to PumpPortal")
                
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if data.get("txType") == "create":
                            wallet = data["traderPublicKey"]
                            sol = data["solAmount"]
                            mint = data["mint"]
                            if sol >= MIN_BUY_SOL:
                                WALLET_BUYS[wallet].append({"token": mint, "sol": sol})
                                print(f"🛒 Tracking: {wallet} | {sol} SOL | {mint}")
                    except Exception as e:
                        print(f"⚠️ Message error: {e}")
        except Exception as e:
            print(f"💥 Connection error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
