import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client
import logging
import logging.handlers
import queue
import atexit
import sys
# === AI LIBRARIES (MUST BE INSTALLED VIA requirements.txt) ===
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import pandas as pd
import joblib 

# === SUPABASE CONFIG (DUMMY/PLACEHOLDER VALUES) ===
SUPABASE_URL = "https://pnvvnlcooykoqoebgfom.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBudnZubGNvb3lrb3FvZWJnZm9tIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MTUwNTkyNywiZXhwIjoyMDc3MDgxOTI3fQ.rj4w2ohncSKrBmArNvxuhP-aTv-nKKqyE_An1WQrnwo"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === OPTIMIZED CONFIGURATION ===
MIN_BUY_SOL = 0.5   
MIN_TRADES = 5      
MIN_ROI = 3.0       
CHECK_INTERVAL_SEC = 1800  # 30 minutes
TOKEN_INFO_URL = "https://frontend-api.pump.fun/tokens/" 
MODEL_FILE = 'elite_wallet_model.pkl'
logger = logging.getLogger("PumpAI")

# === 1. LOGGING SETUP ===

def setup_logging():
    log_queue = queue.Queue(-1)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    queue_listener = logging.handlers.QueueListener(
        log_queue, 
        console_handler
    )
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger.addHandler(queue_handler)
    queue_listener.start()
    atexit.register(queue_listener.stop)
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    logger.info("Non-blocking logging system initialized.")

# === 2. DATA ENRICHMENT FUNCTIONS (ASYNC-SAFE) ===

def get_market_cap(token_mint):
    try:
        response = requests.get(f"{TOKEN_INFO_URL}{token_mint}", timeout=5)
        response.raise_for_status() 
        data = response.json()
        market_cap = data.get("market_cap_sol") 
        return float(market_cap) if market_cap else 0.0
    except requests.exceptions.RequestException:
        return 0.0
    except Exception:
        return 0.0

# === 3. AI MODEL FUNCTIONS (ASYNC-SAFE) ===

def load_training_data():
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        data = resp.data if resp.data else []
        if not data: return None, None
        df = pd.DataFrame(data)
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']]
        df['is_elite'] = df['status'].apply(lambda s: 1 if s == 'elite' else 0)
        y = df['is_elite']
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        joblib.dump(scaler, 'scaler.pkl') 
        return X_scaled, y
    except Exception as e:
        logger.error(f"AI Trainer Error (load_training_data): {e}")
        return None, None

def train_model():
    X, y = load_training_data()
    if X is None or len(X) == 0 or len(y.unique()) < 2:
        logger.warning("AI Trainer: Insufficient data or classes for training.")
        return
    logger.info("AI Trainer: Starting model training...")
    X_train, _, y_train, _ = train_test_split(X, y, test_size=0.2, random_state=42)
    model = LogisticRegression(class_weight='balanced', solver='liblinear') 
    model.fit(X_train, y_train)
    joblib.dump(model, MODEL_FILE)
    logger.info(f"AI Trainer: Model saved to {MODEL_FILE}")

def predict_wallet_score(wallet_features):
    try:
        model = joblib.load(MODEL_FILE)
        scaler = joblib.load('scaler.pkl')
        features_df = pd.DataFrame([wallet_features], columns=['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc'])
        features_scaled = scaler.transform(features_df)
        probability = model.predict_proba(features_scaled)[0][1]
        return probability
    except FileNotFoundError:
        return 0.0 
    except Exception:
        return 0.0

# === 4. DATABASE FUNCTIONS (ASYNC-SAFE) ===

async def get_all_wallets_async():
    resp = await asyncio.to_thread(supabase.table("wallets").select("address").execute)
    return [w["address"] for w in resp.data] if resp.data else []

async def save_buy_async(wallet, token_mint, sol_amount):
    return await asyncio.to_thread(save_buy, wallet, token_mint, sol_amount)

def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    market_cap = get_market_cap(token_mint)
    try:
        supabase.table("trades").upsert({
            "wallet": wallet, "token_mint": token_mint, "buy_sol": sol_amount,
            "buy_ts": ts, "status": "open", "entry_market_cap": market_cap 
        }, on_conflict="wallet, token_mint, status").execute()
        supabase.table("wallets").upsert({
            "address": wallet, "first_seen": ts, "last_updated": ts
        }).execute()
        logger.info(f"🛒 Tracking Buy: {wallet} | {sol_amount} SOL | MC: {market_cap:.2f} SOL | {token_mint}")
        return True
    except Exception as e:
        if "23505" in str(e) and "unique_open_trade": return True
        logger.error(f"DB Error (save_buy) for {wallet}/{token_mint}: {e}")
        return False

async def save_sell_async(wallet, token_mint, sol_amount):
    return await asyncio.to_thread(save_sell, wallet, token_mint, sol_amount)

def save_sell(wallet, token_mint, sol_amount):
    ts = int(time.time())
    try:
        resp = supabase.table("trades").select("buy_sol").eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").limit(1).execute()
        if not resp.data: return False 
        buy_sol = resp.data[0]["buy_sol"]
        roi = sol_amount / buy_sol if buy_sol else 0.0
        supabase.table("trades").update({
            "sell_sol": sol_amount, "sell_ts": ts, "roi": roi, "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        logger.info(f"💰 Closing Trade: {wallet} | ROI: {roi:.2f}x | {token_mint}")
        return True
    except Exception as e:
        logger.error(f"DB Error (save_sell) for {wallet}/{token_mint}: {e}")
        return False

# === 5. ASYNC SCORING LOOP ===

async def score_wallets_async():
    await asyncio.to_thread(train_model) 
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC) 
        logger.info("🧠 Scoring wallets and generating features...")
        try:
            wallets = await get_all_wallets_async()
            ready_to_predict = {} 
            for wallet in wallets:
                def calculate_features(wallet):
                    closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                    closed_trades = closed_resp.data if closed_resp.data else []
                    tokens_traded = len(closed_trades)
                    status = "candidate"
                    wins = 0
                    total_roi = 0.0
                    avg_hold_time = 0.0
                    avg_entry_mc = 0.0
                    if tokens_traded >= MIN_TRADES:
                        closed_rois = [t["roi"] for t in closed_trades]
                        hold_times_sec = [(t["sell_ts"] - t["buy_ts"]) for t in closed_trades if t["sell_ts"] and t["buy_ts"]]
                        avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                        entry_mcs = [t["entry_market_cap"] for t in closed_trades if t["entry_market_cap"] is not None]
                        avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                        wins = len([r for r in closed_rois if r >= MIN_ROI])
                        total_roi = sum(closed_rois)
                        wallet_features = {
                            "tokens_traded": tokens_traded,
                            "avg_hold_time_min": avg_hold_time,
                            "avg_pump_entry_mc": avg_entry_mc
                        }
                        return wallet_features, wins, total_roi, "evaluating"
                    return None, wins, total_roi, status
                
                features, wins, total_roi, status = await asyncio.to_thread(calculate_features, wallet)

                update_data = {
                    "tokens_traded": features["tokens_traded"] if features else 0, "wins": wins, "total_roi": total_roi,
                    "avg_hold_time_min": features["avg_hold_time_min"] if features else 0.0, 
                    "avg_pump_entry_mc": features["avg_pump_entry_mc"] if features else 0.0,
                    "status": status, "last_updated": int(time.time())
                }
                await asyncio.to_thread(supabase.table("wallets").update(update_data).eq("address", wallet).execute)

                if features:
                    ready_to_predict[wallet] = (features, wins, total_roi)

            if ready_to_predict:
                logger.info("AI Predictor: Starting live scoring...")
                for wallet, (features, _, _) in ready_to_predict.items():
                    elite_probability = await asyncio.to_thread(predict_wallet_score, features)
                    status = "elite" if elite_probability >= 0.90 else "demoted"
                        
                    await asyncio.to_thread(supabase.table("wallets").update({
                        "status": status, "elite_probability": elite_probability  
                    }).eq("address", wallet).execute)
                    
                    logger.info(f"🧠 AI Score for {wallet}: {elite_probability:.4f} -> {status}")

            elite_resp = await asyncio.to_thread(supabase.table("wallets").select("address, total_roi, elite_probability").eq("status", "elite").execute)
            elite = elite_resp.data if elite_resp.data else []
            if elite:
                logger.info("🌟 ELITE WALLETS FOUND:")
                for w in elite:
                    logger.info(f"  - {w['address']} (ROI: {w['total_roi']:.2f}x, AI: {w['elite_probability']:.4f})")
            else:
                logger.info("⏳ No elite wallets yet.")

        except Exception as e:
            logger.critical(f"⚠️ Critical Scoring Error: {e}", exc_info=True)

# === 6. WEBSOCKET LISTENER (Final DNS Bypass with Explicit IP/Port) ===

async def ws_listener():
    # Final Fix Attempt: Specify the IP and Port directly, but use the correct hostname for SSL.
    
    # 1. Hostname for SSL Certificate (must be resolvable by the endpoint's server)
    HOSTNAME = "client-api-v2.pump.fun" 
    
    # 2. IP Address and Port to connect to (Bypasses local DNS resolution)
    # ⚠️ IMPORTANT: YOU MUST REPLACE '104.18.3.179' with the current IP of client-api-v2.pump.fun
    IP_ADDRESS = "104.18.3.179"  
    PORT = 443 
    URI = f"wss://{HOSTNAME}/trades" # The URI structure used for the path
    
    if not hasattr(ws_listener, 'scorer_started'):
        asyncio.create_task(score_wallets_async()) 
        ws_listener.scorer_started = True

    logger.info(f"Connecting to pump.fun websocket (DNS bypass) using: {HOSTNAME} at {IP_ADDRESS}:{PORT}. Subscribing to all trades...")
    
    while True:
        try:
            # Explicitly connecting using the IP and Port, but providing the HOSTNAME for SSL validation.
            async with websockets.connect(
                URI, 
                host=HOSTNAME, 
                port=PORT,
                server_hostname=HOSTNAME, # Redundant, but ensures hostname is used for SSL
                # Pass the explicit IP address to the underlying socket connection
                # This requires websockets to use the IP/Port for socket creation
                # The 'uri' is parsed for the path '/trades'
                # The 'host' parameter in connect tells the underlying socket to bypass DNS
                # This is the most direct method to circumvent a broken DNS resolver
                uri_host=IP_ADDRESS # Explicitly override the hostname for the socket connection
            ) as websocket:
                logger.info("✅ Successfully connected to the WebSocket.")
                
                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    
                    if data.get("event") != "trade":
                        continue
                        
                    trade_type = data["data"].get("trade_type")
                    sol_amount = float(data["data"].get("sol_amount", 0.0))
                    token_mint = data["data"].get("mint")
                    wallet = data["data"].get("wallet")
                    
                    if trade_type == "BUY" and sol_amount >= MIN_BUY_SOL:
                        await save_buy_async(wallet, token_mint, sol_amount)
                        
                    elif trade_type == "SELL":
                        await save_sell_async(wallet, token_mint, sol_amount)
                        
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("Websocket connection closed normally. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except websockets.exceptions.InvalidURI:
            logger.critical(f"Invalid WebSocket URI: {URI}. Check the format!")
            await asyncio.sleep(60) 
        except Exception as e:
            # Catch all exceptions, including the underlying socket.gaierror
            logger.error(f"Websocket error: Connection failed. Reconnecting in 10 seconds...", exc_info=False)
            await asyncio.sleep(10)

if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(ws_listener())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
