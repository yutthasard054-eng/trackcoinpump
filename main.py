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

# === SUPABASE CONFIG ===
SUPABASE_URL = "https://pnvvnlcooykoqoebgfom.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBudnZubGNvb3lrb3FvZWJnZm9tIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MTUwNTkyNywiZXhwIjoyMDc3MDgxOTI3fQ.rj4w2ohncSKrBmArNvxuhP-aTv-nKKqyE_An1WQrnwo"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === OPTIMIZED CONFIGURATION ===
MIN_BUY_SOL = 0.5   
MIN_TRADES = 5      
MIN_ROI = 3.0       
CHECK_INTERVAL_SEC = 1800  # 30 minutes
TOKEN_INFO_URL = "https://frontend-api.pump.fun/tokens/" 

# === AI AGENT CONFIGURATION ===
MODEL_FILE = 'elite_wallet_model.pkl'

# Get logger instance
logger = logging.getLogger("PumpAI")

# === 1. LOGGING SETUP (CRITICAL FIX) ===

def setup_logging():
    """Sets up a non-blocking, queue-based logging system."""
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
    
    # Force line-buffering for container compatibility
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    
    logger.info("Non-blocking logging system initialized.")

# === 2. DATA ENRICHMENT FUNCTIONS (ASYNC-SAFE) ===

async def get_market_cap_async(token_mint):
    """Fetches the current market cap for a given token using a thread pool executor."""
    # Use asyncio.to_thread to run the blocking requests.get in a separate thread
    return await asyncio.to_thread(get_market_cap, token_mint)

def get_market_cap(token_mint):
    """Synchronous version of market cap fetch (runs in executor)."""
    try:
        response = requests.get(f"{TOKEN_INFO_URL}{token_mint}", timeout=5)
        response.raise_for_status() 
        data = response.json()
        market_cap = data.get("market_cap_sol") 
        if market_cap:
            return float(market_cap) 
        return 0.0
    except requests.exceptions.RequestException:
        return 0.0
    except Exception:
        return 0.0

# === 3. AI MODEL FUNCTIONS (ASYNC-SAFE) ===

# All blocking AI/DB functions are now run via asyncio.to_thread in the main logic

def load_training_data():
    """Fetches and prepares data for model training (BLOCKING I/O)."""
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        
        data = resp.data if resp.data else []
        if not data:
            return None, None
            
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
    """Trains and saves the Logistic Regression model (BLOCKING I/O)."""
    X, y = load_training_data()
    if X is None or len(X) == 0 or len(y.unique()) < 2:
        logger.warning("AI Trainer: Insufficient data or classes for training.")
        return
        
    logger.info("AI Trainer: Starting model training...")
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    model = LogisticRegression(class_weight='balanced', solver='liblinear') 
    model.fit(X_train, y_train)
    
    score = model.score(X_test, y_test)
    logger.info(f"AI Trainer: Model accuracy on test set: {score:.4f}")
    
    joblib.dump(model, MODEL_FILE)
    logger.info(f"AI Trainer: Model saved to {MODEL_FILE}")

def predict_wallet_score(wallet_features):
    """Predicts the probability of a wallet being elite (BLOCKING I/O)."""
    try:
        model = joblib.load(MODEL_FILE)
        scaler = joblib.load('scaler.pkl')
        
        features_df = pd.DataFrame([wallet_features], columns=['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc'])
        features_scaled = scaler.transform(features_df)
        
        probability = model.predict_proba(features_scaled)[0][1]
        
        return probability
            
    except FileNotFoundError:
        logger.warning(f"Model file {MODEL_FILE} not found. Skipping prediction.")
        return 0.0 
    except Exception as e:
        logger.error(f"AI Predictor error: {e}")
        return 0.0

# === 4. DATABASE FUNCTIONS (ASYNC-SAFE) ===

async def get_all_wallets_async():
    """Async wrapper for fetching all wallet addresses."""
    resp = await asyncio.to_thread(supabase.table("wallets").select("address").execute)
    return [w["address"] for w in resp.data] if resp.data else []

async def save_buy_async(wallet, token_mint, sol_amount):
    """Async wrapper for saving a buy trade."""
    return await asyncio.to_thread(save_buy, wallet, token_mint, sol_amount)

def save_buy(wallet, token_mint, sol_amount):
    """Synchronous buy save (runs in executor)."""
    ts = int(time.time())
    # CRITICAL FIX: get_market_cap is blocking, but it's safe to call here 
    # because this entire function is already running in a separate thread via asyncio.to_thread
    market_cap = get_market_cap(token_mint)

    try:
        supabase.table("trades").upsert({
            "wallet": wallet,
            "token_mint": token_mint,
            "buy_sol": sol_amount,
            "buy_ts": ts,
            "status": "open",
            "entry_market_cap": market_cap 
        }, on_conflict="wallet, token_mint, status").execute()
        
        supabase.table("wallets").upsert({
            "address": wallet,
            "first_seen": ts,
            "last_updated": ts
        }).execute()
        
        logger.info(f"🛒 Tracking Buy: {wallet} | {sol_amount} SOL | MC: {market_cap:.2f} SOL | {token_mint}")
        return True
    except Exception as e:
        error_details = str(e)
        if "23505" in error_details and "unique_open_trade" in error_details:
             logger.info(f"ℹ️ SUPPRESS: Duplicate trade detected for {wallet}/{token_mint}. Already tracking.")
             return True
        else:
             logger.error(f"DB Error (save_buy) for {wallet}/{token_mint}: {e}")
             return False

async def save_sell_async(wallet, token_mint, sol_amount):
    """Async wrapper for saving a sell trade."""
    return await asyncio.to_thread(save_sell, wallet, token_mint, sol_amount)

def save_sell(wallet, token_mint, sol_amount):
    """Synchronous sell save (runs in executor)."""
    ts = int(time.time())
    
    try:
        # Fetch the open trade
        resp = supabase.table("trades").select("buy_sol").eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").limit(1).execute()
        
        if not resp.data:
            return False 
            
        buy_sol = resp.data[0]["buy_sol"]
        roi = sol_amount / buy_sol if buy_sol else 0.0
        
        # Update the trade status to closed
        supabase.table("trades").update({
            "sell_sol": sol_amount,
            "sell_ts": ts,
            "roi": roi,
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        
        logger.info(f"💰 Closing Trade: {wallet} | ROI: {roi:.2f}x | {token_mint}")
        return True
    except Exception as e:
        logger.error(f"DB Error (save_sell) for {wallet}/{token_mint}: {e}")
        return False

# === 5. ASYNC SCORING LOOP (CRITICAL FIX) ===

async def score_wallets_async():
    """Runs periodically to train the model, calculate features, and predict scores."""
    
    # CRITICAL FIX: Start the training in the executor before the loop
    await asyncio.to_thread(train_model) 
    
    while True:
        # CRITICAL FIX: Use await asyncio.sleep to not block the event loop
        await asyncio.sleep(CHECK_INTERVAL_SEC) 
        logger.info("🧠 Scoring wallets and generating features...")
        
        try:
            # CRITICAL FIX: Database calls must be awaited
            wallets = await get_all_wallets_async()
            ready_to_predict = {} 

            for wallet in wallets:
                
                # CRITICAL FIX: Feature calculation involving DB is blocking, run in executor
                
                def calculate_features(wallet):
                    """Synchronous logic to be run in a separate thread."""
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

                # Update wallet record with new calculated features (before prediction)
                update_data = {
                    "tokens_traded": features["tokens_traded"] if features else 0,
                    "wins": wins,
                    "total_roi": total_roi,
                    "avg_hold_time_min": features["avg_hold_time_min"] if features else 0.0,
                    "avg_pump_entry_mc": features["avg_pump_entry_mc"] if features else 0.0,
                    "status": status,
                    "last_updated": int(time.time())
                }
                await asyncio.to_thread(supabase.table("wallets").update(update_data).eq("address", wallet).execute)

                if features:
                    ready_to_predict[wallet] = (features, wins, total_roi)

            # --- AI PREDICTION STEP ---
            if ready_to_predict:
                logger.info("AI Predictor: Starting live scoring...")
                for wallet, (features, wins, total_roi) in ready_to_predict.items():
                    
                    # CRITICAL FIX: Prediction is blocking, run in executor
                    elite_probability = await asyncio.to_thread(predict_wallet_score, features)
                    
                    if elite_probability >= 0.90:  
                        status = "elite"
                    else:
                        status = "demoted"
                        
                    # Final update with AI score
                    await asyncio.to_thread(supabase.table("wallets").update({
                        "status": status,
                        "elite_probability": elite_probability  
                    }).eq("address", wallet).execute)
                    
                    logger.info(f"🧠 AI Score for {wallet}: {elite_probability:.4f} -> {status}")

            # Log elite list (CRITICAL FIX: Database calls must be awaited)
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

# === 6. WEBSOCKET LISTENER ===

async def ws_listener():
    WS_URL = "wss://client-api-v2-wss.pump.fun/trades"
    logger.info("Connecting to pump.fun websocket. Subscribing to all trades...")
    
    # CRITICAL FIX: Use asyncio.create_task to run the scoring loop concurrently
    # This replaces the unreliable threading.Thread(target=score_wallets) 
    asyncio.create_task(score_wallets_async()) 
    
    while True:
        try:
            async with websockets.connect(WS_URL) as websocket:
                
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
                        # CRITICAL FIX: Await the async save_buy function
                        await save_buy_async(wallet, token_mint, sol_amount)
                        
                    elif trade_type == "SELL":
                        # CRITICAL FIX: Await the async save_sell function
                        await save_sell_async(wallet, token_mint, sol_amount)
                        
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("Websocket connection closed normally. Reconnecting...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Websocket error: {e}. Reconnecting in 10 seconds...", exc_info=True)
            await asyncio.sleep(10)

if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(ws_listener())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
