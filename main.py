import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client

# === SUPABASE CONFIG ===
SUPABASE_URL = "https://pnvvnlcooykoqoebgfom.supabase.co"
# The provided service_role key has been inserted below
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBudnZubGNvb3lrb3FvZWJnZm9tIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MTUwNTkyNywiZXhwIjoyMDc3MDgxOTI3fQ.rj4w2ohncSKrBmArNvxuhP-aTv-nKKqyE_An1WQrnwo"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_BUY_SOL = 0.1
MIN_TRADES = 3
MIN_WIN_RATE = 0.6
MIN_ROI = 2.0
CHECK_INTERVAL_SEC = 900

def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    supabase.table("trades").insert({
        "wallet": wallet,
        "token_mint": token_mint,
        "buy_sol": sol_amount,
        "buy_ts": ts,
        "status": "open"
    }).execute()
    supabase.table("wallets").upsert({
        "address": wallet,
        "first_seen": ts,
        "last_updated": ts
    }).execute()

def get_all_wallets():
    try:
        response = supabase.table("wallets").select("address").execute()
        return [w["address"] for w in response.data] if response.data else []
    except Exception as e:
        print(f"DB error (get wallets): {e}")
        return []

def update_wallet_status(wallet, tokens_traded, wins, total_roi, status):
    try:
        supabase.table("wallets").update({
            "tokens_traded": tokens_traded,
            "wins": wins,
            "total_roi": total_roi,
            "status": status,
            "last_updated": int(time.time())
        }).eq("address", wallet).execute()
    except Exception as e:
        print(f"DB error (update status): {e}")

def get_trades_from_api(mint):
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except:
        return []

def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        supabase.table("trades").update({
            "sell_sol": sell_sol,
            "sell_ts": int(time.time()),
            "roi": roi,
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).execute()
    except Exception as e:
        print(f"DB error (close trade): {e}")

def score_wallets():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        print("🧠 Scoring wallets for profitability...")
        try:
            wallets = get_all_wallets()
            for wallet in wallets:
                # Get open trades
                open_trades = supabase.table("trades").select("token_mint, buy_sol").eq("wallet", wallet).eq("status", "open").execute()
                if not open_trades.data:
                    continue

                # FIX for SyntaxError: Added .data and the colon (:) 
                for trade in open_trades.data: 
                    mint = trade["token_mint"]
                    buy_sol = trade["buy_sol"]
                    api_trades = get_trades_from_api(mint)
                    sells = [t for t in api_trades if t["type"] == "sell" and t["user"] == wallet]
                    if sells:
                        close_trade_in_db(wallet, mint, sells[-1]["sol_amount"], buy_sol)

                # Recalculate stats
                closed_resp = supabase.table("trades").select("roi").eq("wallet", wallet).eq("status", "closed").execute()
                closed_rois = [t["roi"] for t in closed_resp.data] if closed_resp.data else []

                if len(closed_rois) < MIN_TRADES:
                    status = "candidate"
                else:
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    win_rate = wins / len(closed_rois) if closed_rois else 0
                    avg_roi = sum(closed_rois) / len(closed_rois) if closed_rois else 0
                    status = "elite" if win_rate >= MIN_WIN_RATE and avg_roi >= MIN_ROI else "demoted"

                update_wallet_status(wallet, len(closed_rois), wins, avg_roi, status)
                if status == "elite":
                    print(f"✅ ELITE: {wallet}")

            # Log elite list
            elite_resp = supabase.table("wallets").select("address").eq("status", "elite").execute()
            elite = [w["address"] for w in elite_resp.data] if elite_resp.data else []
            if elite:
                print("🌟 ELITE WALLETS:")
                for w in elite:
                    print(f"  - {w}")
            else:
                print("⏳ No elite wallets yet.")

        except Exception as e:
            print(f"⚠️ Scoring error: {e}")

async def main():
    threading.Thread(target=score_wallets, daemon=True).start()

    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
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
                            wallet = data["traderPublicKey"]
                            sol = data["solAmount"]
                            mint = data["mint"]
                            if sol >= MIN_BUY_SOL:
                                save_buy(wallet, mint, sol)
                                print(f"🛒 Tracking: {wallet} | {sol} SOL | {mint}")
                    except Exception as e:
                        print(f"⚠️ Message error: {e}")
        except Exception as e:
            print(f"💥 Connection error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
