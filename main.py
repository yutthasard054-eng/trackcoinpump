import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client
import logging
import sys

# === SUPABASE CONFIG ===
SUPABASE_URL = "https://pnvvnlcooykoqoebgfom.supabase.co"  # Fixed: no trailing spaces
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBudnZubGNvb3lrb3FvZWJnZm9tIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MTUwNTkyNywiZXhwIjoyMDc3MDgxOTI3fQ.rj4w2ohncSKrBmArNvxuhP-aTv-nKKqyE_An1WQrnwo"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === CONFIG ===
MIN_BUY_SOL = 0.5
MIN_TRADES = 5
MIN_ROI = 3.0
CHECK_INTERVAL_SEC = 1800  # 30 minutes

# === LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("PumpAI")

# === DATABASE FUNCTIONS ===

def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    try:
        supabase.table("trades").upsert({
            "wallet": wallet,
            "token_mint": token_mint,
            "buy_sol": sol_amount,
            "buy_ts": ts,
            "status": "open"
        }, on_conflict="wallet, token_mint, status").execute()
        supabase.table("wallets").upsert({
            "address": wallet,
            "first_seen": ts,
            "last_updated": ts
        }).execute()
        logger.info(f"🛒 Tracking Buy: {wallet} | {sol_amount} SOL | {token_mint}")
        return True
    except Exception as e:
        if "23505" in str(e):
            return True
        logger.error(f"DB Error (save_buy): {e}")
        return False

def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        supabase.table("trades").update({
            "sell_sol": sell_sol,
            "sell_ts": int(time.time()),
            "roi": roi,
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        logger.info(f"💰 Closed: {wallet} | {token_mint} | ROI: {roi:.2f}x")
    except Exception as e:
        logger.error(f"DB Error (close_trade): {e}")

def get_open_trades():
    try:
        resp = supabase.table("trades").select("id, wallet, token_mint, buy_sol").eq("status", "open").execute()
        return resp.data if resp.data else []
    except Exception as e:
        logger.error(f"DB Error (get_open_trades): {e}")
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
        logger.error(f"DB Error (update_wallet_status): {e}")

# === SCORING LOGIC (WITH SELL POLLING) ===

def score_wallets():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        logger.info("🔍 Checking for sells via Pump.fun API...")
        try:
            # Check all open trades for sells
            open_trades = get_open_trades()
            for trade in open_trades:
                mint = trade["token_mint"]
                wallet = trade["wallet"]
                buy_sol = trade["buy_sol"]
                
                try:
                    resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100", timeout=5)
                    if resp.status_code != 200:
                        continue
                    trades = resp.json()
                    sells = [t for t in trades if t["type"] == "sell" and t["user"] == wallet]
                    if sells:
                        close_trade_in_db(wallet, mint, sells[-1]["sol_amount"], buy_sol)
                except Exception as e:
                    logger.error(f"API Error for {mint}: {e}")
            
            # Update wallet stats
            wallets_resp = supabase.table("wallets").select("address").execute()
            wallets = [w["address"] for w in wallets_resp.data] if wallets_resp.data else []
            
            for wallet in wallets:
                closed_resp = supabase.table("trades").select("roi").eq("wallet", wallet).eq("status", "closed").execute()
                closed_rois = [t["roi"] for t in closed_resp.data] if closed_resp.data else []
                
                wins = 0
                avg_roi = 0
                if len(closed_rois) >= MIN_TRADES:
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    avg_roi = sum(closed_rois) / len(closed_rois)
                    status = "elite" if avg_roi >= MIN_ROI and wins / len(closed_rois) >= 0.6 else "demoted"
                else:
                    status = "candidate"
                
                update_wallet_status(wallet, len(closed_rois), wins, avg_roi, status)
                if status == "elite":
                    logger.info(f"✅ ELITE: {wallet} | Wins: {wins} | Avg ROI: {avg_roi:.2f}x")
            
            # Log elite wallets
            elite_resp = supabase.table("wallets").select("address").eq("status", "elite").execute()
            elite = [w["address"] for w in elite_resp.data] if elite_resp.data else []
            if elite:
                logger.info("🌟 ELITE WALLETS:")
                for w in elite:
                    logger.info(f"  - {w}")
            else:
                logger.info("⏳ No elite wallets yet.")
                
        except Exception as e:
            logger.error(f"Scoring Error: {e}")

# === WEBSOCKET LISTENER (CORRECT PARSING) ===

async def main():
    threading.Thread(target=score_wallets, daemon=True).start()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("✅ Connected to PumpPortal")
                
                # Subscribe to ALL new tokens and trades
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        # Only 'create' events (buys) are sent
                        if "txType" in data and data["txType"] == "create":
                            wallet = data.get("traderPublicKey")
                            sol = data.get("solAmount")
                            mint = data.get("mint")
                            if wallet and sol and mint and sol >= MIN_BUY_SOL:
                                save_buy(wallet, mint, sol)
                    except Exception as e:
                        logger.error(f"Message Error: {e}")
        except Exception as e:
            logger.error(f"WebSocket Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
