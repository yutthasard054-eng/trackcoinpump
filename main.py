import asyncio
import websockets
import json
from collections import defaultdict

# In-memory wallet profiler
WALLET_STATS = defaultdict(lambda: {"buys": [], "sells": [], "tokens": set()})
TOKEN_CREATED_AT = {}
ELITE_WALLETS = set()

def score_wallet(wallet):
    stats = WALLET_STATS[wallet]
    buys = stats["buys"]
    sells = {s["token"]: s for s in stats["sells"]}
    
    # Match completed trades
    results = []
    for buy in buys:
        sell = sells.get(buy["token"])
        if sell and buy["sol"] > 0:
            roi = sell["sol"] / buy["sol"]
            results.append(roi)

    if len(results) < 3:
        return {"is_elite": False}

    win_rate = len([r for r in results if r >= 2.0]) / len(results)
    avg_roi = sum(results) / len(results) if results else 0

    is_elite = win_rate >= 0.6 and avg_roi >= 3.0

    return {
        "is_elite": is_elite,
        "wallet": wallet,
        "win_rate": round(win_rate, 3),
        "avg_roi": round(avg_roi, 2),
        "trades": len(results)
    }

async def main():
    uri = "wss://pumpportal.fun/api/data"
    async with websockets.connect(uri) as ws:
        print("✅ Connected to PumpPortal")
        
        # Subscribe to ALL new tokens
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        # Subscribe to ALL trades (empty keys = global feed)
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
        
        async for message in ws:
            try:
                data = json.loads(message)
                
                if data.get("txType") == "create":
                    # New token created
                    mint = data["mint"]
                    WALLET_STATS[data["traderPublicKey"]]["tokens"].add(mint)
                    TOKEN_CREATED_AT[mint] = data.get("timestamp", 0)
                    print(f"🆕 New token: {data['name']} ({mint}) by {data['traderPublicKey'][:8]}...")

                elif "traderPublicKey" in data and "solAmount" in data:
                    # Trade detected
                    wallet = data["traderPublicKey"]
                    mint = data["mint"]
                    sol = data["solAmount"]
                    tx_type = "buy"  # PumpPortal only sends buy/create events

                    # Record buy
                    WALLET_STATS[wallet]["buys"].append({"token": mint, "sol": sol})
                    print(f"🛒 Buy: {wallet[:8]}... | {sol} SOL | {mint}")

                    # Score wallet
                    profile = score_wallet(wallet)
                    if profile["is_elite"] and wallet not in ELITE_WALLETS:
                        ELITE_WALLETS.add(wallet)
                        print(f"✅ ELITE WALLET PROMOTED: {wallet[:8]}... | Win Rate: {profile['win_rate']}, Avg ROI: {profile['avg_roi']}x")

            except Exception as e:
                print(f"⚠️ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
