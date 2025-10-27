import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client

# === SUPABASE CONFIG ===
SUPABASE_URL = "https://pnvvnlcooykoqoebgfom.supabase.co"
# SERVICE_ROLE key is used for stability
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBudnZubGNvb3lrb3FvZWJnZm9tIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MTUwNTkyNywiZXhwIjoyMDc3MDgxOTI3fQ.rj4w2ohncSKrBmArNvxuhP-aTv-nKKqyE_An1WQrnwo"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === CONFIGURATION ===
MIN_BUY_SOL = 0.1
MIN_TRADES = 3
MIN_WIN_RATE = 0.6
MIN_ROI = 2.0
CHECK_INTERVAL_SEC = 900 # 15 minutes (Only for scoring, no more API polling!)

# === NEW FEATURE: MARKET CAP FETCH FUNCTION ===
TOKEN_INFO_URL = "https://frontend-api.pump.fun/tokens/" 

def get_market_cap(token_mint):
    """Fetches the current market cap for a given token."""
    try:
        # Hitting a stable, public token info endpoint.
        response = requests.get(f"{TOKEN_INFO_URL}{token_mint}", timeout=5)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        
        # The exact key can vary. Use 'market_cap_sol' if available, otherwise 0.0
        # If the API key is different, you'll need to check the exact response key.
        market_cap = data.get("market_cap_sol") 
        
        if market_cap:
            return float(market_cap) 
        return 0.0
    except requests.exceptions.RequestException as e:
        print(f"API Error (get_market_cap) for {token_mint}: {e}")
        return 0.0
    except Exception as e:
        print(f"Parsing Error (get_market_cap) for {token_mint}: {e}")
        return 0.0

# === DATABASE FUNCTIONS (REST OF FILE FOLLOWS) ===

def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    
    # NEW: Fetch Market Cap at the moment of the buy
    market_cap = get_market_cap(token_mint)

    try:
        # Using upsert to prevent duplicate entry errors for the same open trade
        supabase.table("trades").upsert({
            "wallet": wallet,
            "token_mint": token_mint,
            "buy_sol": sol_amount,
            "buy_ts": ts,
            "status": "open",
            # NEW: Store the feature
            "entry_market_cap": market_cap 
        }, on_conflict="wallet, token_mint, status").execute()
        
        # Upsert the wallet record
        supabase.table("wallets").upsert({
            "address": wallet,
            "first_seen": ts,
            "last_updated": ts
        }).execute()
        
        print(f"🛒 Tracking Buy: {wallet} | {sol_amount} SOL | MC: {market_cap:.2f} SOL | {token_mint}")
        return True
    except Exception as e:
        # Added token_mint and wallet to error log for better debugging
        print(f"DB Error (save_buy) for {wallet}/{token_mint}: {e}")
        return False
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

def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        # Find the specific 'open' trade to close
        supabase.table("trades").update({
            "sell_sol": sell_sol,
            "sell_ts": int(time.time()),
            "roi": roi,
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        
        # NOTE: A subsequent run of score_wallets will pick this up and update wallet stats
    except Exception as e:
        print(f"DB error (close trade): {e}")

def get_open_buy_sol(wallet, token_mint):
    """Retrieves the original buy_sol amount for a currently open trade."""
    try:
        response = supabase.table("trades").select("buy_sol").eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").limit(1).execute()
        if response.data:
            return response.data[0]["buy_sol"]
        return None
    except Exception as e:
        print(f"DB error (get_open_buy_sol): {e}")
        return None

# === SCORING LOGIC (NO MORE API POLLING) ===

def score_wallets():
    """Runs periodically to calculate and update wallet profitability scores and features."""
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        print("🧠 Scoring wallets and generating features...")
        try:
            wallets = get_all_wallets()
            for wallet in wallets:
                
                # Recalculate stats based ONLY on closed trades
                # NEW: Select all data needed for feature engineering
                closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                closed_trades = closed_resp.data if closed_resp.data else []

                # Initialize variables to prevent 'referenced before assignment' error
                wins = 0
                total_roi = 0
                avg_hold_time = 0
                avg_entry_mc = 0

                if len(closed_trades) < MIN_TRADES:
                    status = "candidate"
                else:
                    closed_rois = [t["roi"] for t in closed_trades]
                    
                    # 1. Feature: Avg Hold Time Calculation
                    hold_times_sec = [(t["sell_ts"] - t["buy_ts"]) for t in closed_trades if t["sell_ts"] and t["buy_ts"]]
                    if hold_times_sec:
                        avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 # Convert to minutes

                    # 2. Feature: Avg Entry Market Cap Calculation
                    entry_mcs = [t["entry_market_cap"] for t in closed_trades if t["entry_market_cap"] is not None]
                    if entry_mcs:
                        avg_entry_mc = sum(entry_mcs) / len(entry_mcs)
                    
                    # 3. Standard Score Calculations
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    total_roi = sum(closed_rois)
                    avg_roi = total_roi / len(closed_trades)
                    win_rate = wins / len(closed_trades)
                    
                    # Determine final status (placeholder, will be replaced by AI model)
                    if win_rate >= MIN_WIN_RATE and avg_roi >= MIN_ROI:
                        status = "elite"
                    else:
                        status = "demoted"
                
                # Update wallet record with new stats and features
                try:
                    supabase.table("wallets").update({
                        "tokens_traded": len(closed_trades),
                        "wins": wins,
                        "total_roi": total_roi,
                        "avg_hold_time_min": avg_hold_time, # NEW FEATURE
                        "avg_pump_entry_mc": avg_entry_mc, # NEW FEATURE
                        "status": status,
                        "last_updated": int(time.time())
                    }).eq("address", wallet).execute()
                except Exception as db_update_e:
                    print(f"⚠️ DB Update Error for {wallet}: {db_update_e}")


            # Log elite list
            elite_resp = supabase.table("wallets").select("address, total_roi").eq("status", "elite").execute()
            elite = elite_resp.data if elite_resp.data else []
            if elite:
                print("🌟 ELITE WALLETS FOUND:")
                for w in elite:
                    print(f"  - {w['address']} (ROI: {w['total_roi']:.2f}x)")
            else:
                print("⏳ No elite wallets yet.")

        except Exception as e:
            print(f"⚠️ Scoring error: {e}")

# ------------------------------
# === MAIN WEBSOCKET LISTENER (HANDLES BUYS AND SELLS) ===

async def main():
    # Start the wallet scoring thread
    threading.Thread(target=score_wallets, daemon=True).start()

    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✅ Connected to PumpPortal WebSocket. Subscribing to all trades...")

                # Subscribe to ALL new tokens and trades
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))

                async for message in ws:
                    try:
                        data = json.loads(message)
                        tx_type = data.get("txType")
                        
                        # Data structure for create and trade events is slightly different
                        wallet = data.get("traderPublicKey", data.get("user")) 
                        sol = data.get("solAmount") 
                        mint = data.get("mint")
                        
                        if tx_type == "create":
                            # === HANDLE BUY (CREATE) ===
                            if sol is not None and sol >= MIN_BUY_SOL:
                                success = save_buy(wallet, mint, sol)
                                if success:
                                    print(f"🛒 Tracking Buy: {wallet} | {sol} SOL | {mint}")
                                
                        elif tx_type == "sell":
                            # === HANDLE SELL (TRADE) ===
                            if sol is not None and wallet and mint:
                                # Need to retrieve the original buy_sol amount from the DB to calculate ROI
                                buy_sol = get_open_buy_sol(wallet, mint)
                                
                                if buy_sol is not None:
                                    close_trade_in_db(wallet, mint, sol, buy_sol)
                                    print(f"💰 Trade Closed: {wallet} sold {mint} for {sol:.4f} SOL (ROI check next cycle)")
                                else:
                                    # Wallet sold, but we weren't tracking the initial buy
                                    print(f"ℹ️ Sell Event: {wallet} sold {mint}, but no open buy trade was found.")

                    except Exception as e:
                        print(f"⚠️ Message processing error: {e}")
        except Exception as e:
            # Catch connection errors and attempt to reconnect
            print(f"💥 WebSocket Connection error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Application stopped manually.")
