import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client
import logging
import sys
import os
from concurrent.futures import ThreadPoolExecutor

# === MACHINE LEARNING (Optional) ===
try:
    from sklearn.model_selection import train_test_split
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    import pandas as pd
    import joblib
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

# === CONFIG ===
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://pnvvnlcooykoqoebgfom.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is required!")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_BUY_SOL = float(os.getenv("MIN_BUY_SOL", "0.1"))
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))
MIN_ROI = float(os.getenv("MIN_ROI", "3.0"))
ELITE_THRESHOLD = float(os.getenv("ELITE_THRESHOLD", "0.90"))
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "1800"))

logger = logging.getLogger("PumpAI")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
executor = ThreadPoolExecutor(max_workers=5)

# === DATABASE FUNCTIONS ===
def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    try:
        supabase.table("trades").upsert({
            "wallet": wallet, "token_mint": token_mint, "buy_sol": sol_amount,
            "buy_ts": ts, "status": "open"
        }, on_conflict="wallet, token_mint, status").execute()
        supabase.table("wallets").upsert({
            "address": wallet, "first_seen": ts, "last_updated": ts
        }).execute()
        logger.info(f"🛒 Tracking Buy: {wallet} | {sol_amount} SOL | {token_mint}")
        return True
    except Exception as e:
        if "23505" in str(e): return True
        logger.error(f"DB Error (save_buy): {e}")
        return False

def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        supabase.table("trades").update({
            "sell_sol": sell_sol, "sell_ts": int(time.time()), "roi": roi, "status": "closed"
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

def update_wallet_status(wallet, tokens_traded, wins, total_roi, status, elite_probability=0.0):
    try:
        data = {
            "tokens_traded": tokens_traded, "wins": wins, "total_roi": total_roi,
            "status": status, "last_updated": int(time.time())
        }
        if ML_AVAILABLE:
            data["elite_probability"] = elite_probability
        supabase.table("wallets").update(data).eq("address", wallet).execute()
    except Exception as e:
        logger.error(f"DB Error (update_wallet_status): {e}")

# === MACHINE LEARNING FUNCTIONS ===
def load_training_data():
    if not ML_AVAILABLE: return None, None
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        data = resp.data if resp.data else []
        if not data or len(data) < 10: return None, None
        df = pd.DataFrame(data)
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']]
        df['is_elite'] = df['status'].apply(lambda s: 1 if s == 'elite' else 0)
        y = df['is_elite']
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        joblib.dump(scaler, 'scaler.pkl')
        return X_scaled, y
    except Exception as e:
        logger.error(f"AI Trainer Error: {e}")
        return None, None

def train_model():
    if not ML_AVAILABLE: return
    X, y = load_training_data()
    if X is None or len(X) == 0 or (len(y) > 0 and len(set(y)) < 2):
        return
    logger.info("🧠 Training AI model...")
    X_train, _, y_train, _ = train_test_split(X, y, test_size=0.2, random_state=42)
    model = RandomForestClassifier(n_estimators=100, class_weight='balanced', random_state=42)
    model.fit(X_train, y_train)
    joblib.dump(model, 'elite_wallet_model.pkl')
    logger.info("✅ AI model trained and saved")

def predict_wallet_score(wallet_features):
    if not ML_AVAILABLE: return 0.0
    try:
        model = joblib.load('elite_wallet_model.pkl')
        scaler = joblib.load('scaler.pkl')
        features_df = pd.DataFrame([wallet_features], columns=['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc'])
        features_scaled = scaler.transform(features_df)
        probability = model.predict_proba(features_scaled)[0][1]
        return probability
    except FileNotFoundError:
        return 0.0
    except Exception as e:
        logger.error(f"AI Predictor Error: {e}")
        return 0.0

# === SCORING LOGIC ===
def score_wallets():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        logger.info("🔍 Checking for sells via Pump.fun API...")
        try:
            # Check for sells
            open_trades = get_open_trades()
            for trade in open_trades:
                mint = trade["token_mint"]
                wallet = trade["wallet"]
                buy_sol = trade["buy_sol"]
                try:
                    resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100", timeout=5)
                    if resp.status_code != 200: continue
                    trades = resp.json()
                    sells = [t for t in trades if t["type"] == "sell" and t["user"] == wallet]
                    if sells:
                        close_trade_in_db(wallet, mint, sells[-1]["sol_amount"], buy_sol)
                except Exception as e:
                    logger.error(f"API Error for {mint}: {e}")
            
            # Update wallet stats
            wallets_resp = supabase.table("wallets").select("address").execute()
            wallets = [w["address"] for w in wallets_resp.data] if wallets_resp.data else []
            
            if ML_AVAILABLE:
                train_model()
            
            for wallet in wallets:
                closed_resp = supabase.table("trades").select("roi").eq("wallet", wallet).eq("status", "closed").execute()
                closed_rois = [t["roi"] for t in closed_resp.data] if closed_resp.data else []
                wins = 0
                avg_roi = 0
                elite_probability = 0.0
                
                if len(closed_rois) >= MIN_TRADES:
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    avg_roi = sum(closed_rois) / len(closed_rois)
                    win_rate = wins / len(closed_rois)
                    
                    if ML_AVAILABLE:
                        hold_times_sec = []
                        entry_mcs = []
                        closed_trades = supabase.table("trades").select("buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                        for t in closed_trades.data or []:
                            if t.get("sell_ts") and t.get("buy_ts"):
                                hold_times_sec.append(t["sell_ts"] - t["buy_ts"])
                            if t.get("entry_market_cap") is not None:
                                entry_mcs.append(t["entry_market_cap"])
                        avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                        avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                        wallet_features = {
                            "tokens_traded": len(closed_rois),
                            "avg_hold_time_min": avg_hold_time,
                            "avg_pump_entry_mc": avg_entry_mc
                        }
                        elite_probability = predict_wallet_score(wallet_features)
                        status = "elite" if elite_probability >= ELITE_THRESHOLD else "demoted"
                    else:
                        status = "elite" if win_rate >= 0.6 and avg_roi >= MIN_ROI else "demoted"
                else:
                    status = "candidate"
                
                update_wallet_status(wallet, len(closed_rois), wins, avg_roi, status, elite_probability)
                if status == "elite":
                    logger.info(f"✅ ELITE: {wallet} | AI: {elite_probability:.4f}" if ML_AVAILABLE else f"✅ ELITE: {wallet}")
            
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
                        # Only 'create' events are sent (buys)
                        if data.get("txType") == "create":
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
