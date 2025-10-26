import asyncio
import websockets
import json
from collections import defaultdict

# Track early buys
WALLET_BUYS = defaultdict(list)
ELITE_WALLETS = set()

async def main():
    uri = "wss://pumpportal.fun/api/data"
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
                    
                    # Only track significant early buys (≥0.1 SOL)
                    if sol >= 0.1:
                        WALLET_BUYS[wallet].append({"token": mint, "sol": sol})
                        print(f"🛒 Tracking: {wallet[:8]}... | {sol} SOL | {mint}")
                        
            except Exception as e:
                print(f"⚠️ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
