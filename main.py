import asyncio
import websockets
import json

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
                print(f"📥 Raw data: {data}")  # DEBUG: Log EVERY message
                
                if data.get("type") == "newToken":
                    print(f"🆕 New token: {data['data']['mint']}")
                    
                elif data.get("type") == "tokenTrade":
                    trade = data["data"]
                    print(f"💱 Trade: {trade['account']} | {trade['type']} | {trade['solAmount']} SOL | Token: {trade['token']}")
                    
            except Exception as e:
                print(f"⚠️ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
