import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client
import logging
import logging.handlers
import sys
import queue
import atexit
from concurrent.futures import ThreadPoolExecutor
import os
from datetime import datetime

# === MACHINE LEARNING LIBRARIES ===
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import pandas as pd
import joblib 

# === SUPABASE CONFIG (SECURE) ===
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://pnvvnlcooykoqoebgfom.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is required!")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === CONFIG & ML SETUP ===
MIN_BUY_SOL = float(os.getenv("MIN_BUY_SOL", "0.1"))
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))
MIN_ROI = float(os.getenv("MIN_ROI", "3.0"))
ELITE_THRESHOLD = float(os.getenv("ELITE_THRESHOLD", "0.90"))
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "1800"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

TOKEN_INFO_URL = "https://frontend-api.pump.fun/trades/"
TOKEN_DATA_URL = "https://frontend-api.pump.fun/coins/"
MODEL_FILE = 'elite_wallet_model.pkl'
SCALER_FILE = 'scaler.pkl'
logger = logging.getLogger("PumpAI")

executor = ThreadPoolExecutor(max_workers=5) 

# Track stats
stats = {
    "messages_received": 0,
    "buys_tracked": 0,
    "sells_detected": 0,
    "errors": 0,
    "start_time": time.time()
}

def cleanup_executor():
    logger.info("Shutting down executor...")
    executor.shutdown(wait=True)
    
atexit.register(cleanup_executor)

# === 1. LOGGING SETUP ===

def setup_logging():
    log_queue = queue.Queue(-1)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    queue_listener = logging.handlers.QueueListener(log_queue, console_handler)
    queue_handler = logging.handlers.QueueHandler(log_queue)
    root_logger.addHandler(queue_handler)
    queue_listener.start()
    atexit.register(queue_listener.stop)
    logger.info("Non-blocking logging system initialized.")

# === 2. MARKET CAP FETCHING ===

def get_token_market_cap(token_mint):
    try:
        resp = requests.get(f"{TOKEN_DATA_URL}{token_mint}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            market_cap = data.get("usd_market_cap", 0)
            return market_cap
        return 0
    except Exception as e:
        logger.warning(f"Failed to fetch market cap for {token_mint}: {e}")
        return 0

# === 3. MACHINE LEARNING CORE ===

def load_training_data():
    try:
        resp = supabase.table("wallets").select(
            "address, tokens_traded, avg_hold_time_min, avg_pump_entry_mc, total_roi, wins, status"
        ).gte("tokens_traded", MIN_TRADES).execute()
        
        data = resp.data if resp.data else []
        if len(data) < 10:
            logger.warning(f"AI Trainer: Insufficient data ({len(data)} wallets). Need at least 10.")
            return None, None, None
            
        df = pd.DataFrame(data)
        
        def determine_label(row):
            if row['status'] == 'elite':
                return 1
            if row['tokens_traded'] >= MIN_TRADES and row['total_roi'] >= MIN_ROI * MIN_TRADES and row['wins'] >= 2:
                return 1
            return 0
        
        df['is_elite'] = df.apply(determine_label, axis=1)
        
        if len(df['is_elite'].unique()) < 2:
            logger.warning("AI Trainer: Only one class present in training data.")
            return None, None, None
        
        features = ['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc', 'total_roi', 'wins']
        
        for col in features:
            df[col] = df[col].fillna(0)
        
        X = df[features]
        y = df['is_elite']
        
        elite_count = sum(y)
        logger.info(f"Training data: {len(y)} wallets ({elite_count} elite, {len(y)-elite_count} non-elite)")
        
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        joblib.dump(scaler, SCALER_FILE) 
        
        return X_scaled, y, features
    except Exception as e:
        logger.error(f"AI Trainer Error (load_training_data): {e}", exc_info=True)
        return None, None, None

def train_model():
    X, y, features = load_training_data()
    if X is None or len(X) == 0:
        logger.warning("AI Trainer: Cannot train - insufficient data.")
        return False
        
    logger.info("AI Trainer: Starting model training...")
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    
    model = RandomForestClassifier(
        n_estimators=100,
        class_weight='balanced',
        random_state=42,
        max_depth=5
    )
    
    model.fit(X_train, y_train)
    
    train_accuracy = model.score(X_train, y_train)
    test_accuracy = model.score(X_test, y_test)
    
    logger.info(f"Model Performance: Train={train_accuracy:.2%}, Test={test_accuracy:.2%}")
    
    model_metadata = {
        'features': features,
        'trained_at': datetime.now().isoformat(),
        'train_accuracy': train_accuracy,
        'test_accuracy': test_accuracy
    }
    
    joblib.dump(model, MODEL_FILE)
    joblib.dump(model_metadata, 'model_metadata.pkl')
    logger.info(f"AI Trainer: Model saved to {MODEL_FILE}")
    return True

def predict_wallet_score(wallet_features):
    try:
        model = joblib.load(MODEL_FILE)
        scaler = joblib.load(SCALER_FILE)
        metadata = joblib.load('model_metadata.pkl')
        
        features_df = pd.DataFrame([wallet_features], columns=metadata['features'])
        features_scaled = scaler.transform(features_df)
        
        probability = model.predict_proba(features_scaled)[0][1]
        return probability
    except FileNotFoundError:
        logger.debug("Model not found - will train on next scoring cycle")
        return 0.0 
    except Exception as e:
        logger.error(f"AI Predictor Error: {e}", exc_info=True)
        return 0.0

# === 4. DATABASE FUNCTIONS ===

def _save_buy_sync(wallet, token_mint, sol_amount, market_cap):
    ts = int(time.time())
    try:
        supabase.table("trades").upsert({
            "wallet": wallet, 
            "token_mint": token_mint, 
            "buy_sol": sol_amount,
            "buy_ts": ts, 
            "entry_market_cap": market_cap,
            "status": "open"
        }, on_conflict="wallet, token_mint, status").execute()
        
        supabase.table("wallets").upsert({
            "address": wallet, 
            "first_seen": ts, 
            "last_updated": ts
        }, on_conflict="address").execute()
        
        logger.info(f"BUY TRACKED: {wallet[:8]}... | {sol_amount:.2f} SOL | MC: ${market_cap:,.0f}")
        stats["buys_tracked"] += 1
        return True
    except Exception as e:
        if "23505" in str(e):
            return True
        logger.error(f"DB Error (save_buy): {e}", exc_info=True)
        stats["errors"] += 1
        return False

async def save_buy_async(wallet, token_mint, sol_amount, market_cap):
    return await asyncio.get_event_loop().run_in_executor(
        executor, _save_buy_sync, wallet, token_mint, sol_amount, market_cap
    )

def _get_open_trades_sync():
    try:
        resp = supabase.table("trades").select(
            "id, wallet, token_mint, buy_sol"
        ).eq("status", "open").execute()
        return resp.data if resp.data else []
    except Exception as e:
        logger.error(f"DB Error (get_open_trades): {e}", exc_info=True)
        return []

async def get_open_trades_async():
    return await asyncio.get_event_loop().run_in_executor(executor, _get_open_trades_sync)

def _close_trade_in_db_sync(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        supabase.table("trades").update({
            "sell_sol": sell_sol, 
            "sell_ts": int(time.time()), 
            "roi": roi, 
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        
        logger.info(f"SELL CLOSED: {wallet[:8]}... | {token_mint[:8]}... | ROI: {roi:.2f}x")
        stats["sells_detected"] += 1
    except Exception as e:
        logger.error(f"DB Error (close_trade): {e}", exc_info=True)
        stats["errors"] += 1

async def close_trade_in_db_async(wallet, token_mint, sell_sol, buy_sol):
    await asyncio.get_event_loop().run_in_executor(
        executor, _close_trade_in_db_sync, wallet, token_mint, sell_sol, buy_sol
    )

def _get_closed_trades_sync(wallet):
    try:
        resp = supabase.table("trades").select(
            "roi, buy_ts, sell_ts, entry_market_cap"
        ).eq("wallet", wallet).eq("status", "closed").execute()
        return resp.data if resp.data else []
    except Exception as e:
        logger.error(f"DB Error (get_closed_trades): {e}", exc_info=True)
        return []

def _get_open_trade_sync(wallet, token_mint):
    try:
        resp = supabase.table("trades").select(
            "id, buy_sol"
        ).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        
        if resp.data and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        logger.error(f"DB Error (get_open_trade): {e}", exc_info=True)
        return None

# === 5. SCORING LOGIC ===

async def score_wallets_async():
    await asyncio.get_event_loop().run_in_executor(executor, train_model) 
    
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        logger.info("AI Scoring: Starting scoring cycle...")
        
        uptime = time.time() - stats["start_time"]
        logger.info(f"Stats: Messages={stats['messages_received']} | Buys={stats['buys_tracked']} | Sells={stats['sells_detected']} | Errors={stats['errors']} | Uptime={uptime/3600:.1f}h")
        
        trained = await asyncio.get_event_loop().run_in_executor(executor, train_model)
        
        if not trained:
            logger.warning("Model training skipped - insufficient data")
        
        try:
            open_trades = await get_open_trades_async()
            logger.info(f"Checking {len(open_trades)} open trades for sells...")
            
            for trade in open_trades:
                mint = trade["token_mint"]
                wallet = trade["wallet"]
                buy_sol = trade["buy_sol"]
                
                def check_sells():
                    try:
                        resp = requests.get(f"{TOKEN_INFO_URL}{mint}?limit=100", timeout=5)
                        if resp.status_code != 200: 
                            return []
                        trades = resp.json()
                        sells = [t for t in trades if t.get("txType") == "sell" and t.get("user") == wallet]
                        return sells
                    except Exception as e:
                        logger.debug(f"Error checking sells for {mint}: {e}")
                        return []
                        
                sells = await asyncio.get_event_loop().run_in_executor(executor, check_sells)
                
                if sells:
                    await close_trade_in_db_async(wallet, mint, sells[-1]["sol_amount"], buy_sol)
            
            wallets_resp = await asyncio.get_event_loop().run_in_executor(
                executor, supabase.table("wallets").select("address").execute
            )
            wallets = [w["address"] for w in wallets_resp.data] if wallets_resp.data else []
            
            logger.info(f"Scoring {len(wallets)} wallets...")
            
            for wallet in wallets:
                closed_trades = await asyncio.get_event_loop().run_in_executor(
                    executor, _get_closed_trades_sync, wallet
                )
                tokens_traded = len(closed_trades)
                
                if tokens_traded >= MIN_TRADES:
                    hold_times_sec = [
                        (t["sell_ts"] - t["buy_ts"]) 
                        for t in closed_trades 
                        if t.get("sell_ts") and t.get("buy_ts")
                    ]
                    avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                    
                    entry_mcs = [
                        t["entry_market_cap"] 
                        for t in closed_trades 
                        if t.get("entry_market_cap") is not None
                    ]
                    avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                    
                    closed_rois = [t["roi"] for t in closed_trades]
                    total_roi = sum(closed_rois)
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    
                    wallet_features = {
                        "tokens_traded": tokens_traded,
                        "avg_hold_time_min": avg_hold_time,
                        "avg_pump_entry_mc": avg_entry_mc,
                        "total_roi": total_roi,
                        "wins": wins
                    }
                    
                    elite_probability = await asyncio.get_event_loop().run_in_executor(
                        executor, predict_wallet_score, wallet_features
                    )
                    
                    status = "elite" if elite_probability >= ELITE_THRESHOLD else "evaluating"
                    
                    update_data = {
                        "tokens_traded": tokens_traded, 
                        "wins": wins, 
                        "total_roi": total_roi,
                        "avg_hold_time_min": avg_hold_time, 
                        "avg_pump_entry_mc": avg_entry_mc,
                        "status": status, 
                        "elite_probability": elite_probability, 
                        "last_updated": int(time.time())
                    }
                    
                    await asyncio.get_event_loop().run_in_executor(
                        executor, 
                        supabase.table("wallets").update(update_data).eq("address", wallet).execute
                    )
                    
                    if status == "elite":
                        logger.info(f"ELITE FOUND: {wallet[:8]}... | AI: {elite_probability:.2%}")

                else:
                    await asyncio.get_event_loop().run_in_executor(
                        executor, 
                        supabase.table("wallets").update({
                            "status": "candidate", 
                            "last_updated": int(time.time())
                        }).eq("address", wallet).execute
                    )

            elite_resp = await asyncio.get_event_loop().run_in_executor(
                executor, 
                supabase.table("wallets").select("address, elite_probability, total_roi, tokens_traded")
                .eq("status", "elite").execute
            )
            elite = elite_resp.data if elite_resp.data else []
            
            if elite:
                logger.info(f"ELITE WALLETS: {len(elite)} found")
                for w in elite[:5]:
                    logger.info(
                        f"  ELITE: {w['address'][:12]}... | "
                        f"AI: {w.get('elite_probability', 0.0):.2%} | "
                        f"ROI: {w.get('total_roi', 0):.1f}x | "
                        f"Trades: {w.get('tokens_traded', 0)}"
                    )
            else:
                logger.info("No elite wallets yet. Keep collecting data...")
                
        except Exception as e:
            logger.error(f"Critical AI Scoring Error: {e}", exc_info=True)
            stats["errors"] += 1

# === 6. WEBSOCKET LISTENER ===

open_trades_cache = {}

async def ws_listener():
    uri = "wss://pumpportal.fun/api/data"
    
    if not hasattr(ws_listener, 'scorer_started'):
        asyncio.create_task(score_wallets_async()) 
        ws_listener.scorer_started = True

    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("Connected to PumpPortal WebSocket")
                
                await ws.send(json.dumps({"method": "subscribeNewToken"})) 
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []})) 
                logger.info("Subscribed to token trades stream")
                
                message_count = 0
                last_sample_time = time.time()
                
                async for message in ws:
                    try:
                        stats["messages_received"] += 1
                        message_count += 1
                        
                        current_time = time.time()
                        if current_time - last_sample_time > 10:
                            logger.info(f"Received {message_count} messages in last 10s")
                            message_count = 0
                            last_sample_time = current_time
                        
                        data = json.loads(message)
                        event_method = data.get("method")
                        trade_data = data.get("data", {})
                        
                        if stats["messages_received"] <= 3:
                            logger.info(f"SAMPLE MESSAGE #{stats['messages_received']}: {json.dumps(data)[:300]}")

                        if event_method == "tokenTrade" and trade_data:
                            tx_type = trade_data.get("txType", "").lower()
                            sol_amount_str = trade_data.get("sol_amount")
                            
                            if DEBUG_MODE and stats["messages_received"] % 50 == 0:
                                logger.debug(f"Trade data: txType={tx_type}, sol_amount={sol_amount_str}")
                            
                            if sol_amount_str is None: 
                                continue

                            try:
                                sol_amount = float(sol_amount_str)
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid SOL amount: {sol_amount_str}")
                                continue 
                                
                            token_mint = trade_data.get("mint")
                            wallet = trade_data.get("user")
                            
                            if not token_mint or not wallet:
                                if DEBUG_MODE:
                                    logger.debug(f"Missing mint or wallet: mint={token_mint}, wallet={wallet}")
                                continue
                            
                            if tx_type == "buy" and sol_amount >= MIN_BUY_SOL:
                                logger.info(f"BUY DETECTED: {wallet[:8]}... | {sol_amount:.2f} SOL | Token: {token_mint[:8]}...")
                                
                                market_cap = await asyncio.get_event_loop().run_in_executor(
                                    executor, get_token_market_cap, token_mint
                                )
                                
                                success = await save_buy_async(wallet, token_mint, sol_amount, market_cap)
                                
                                if success:
                                    cache_key = f"{wallet}:{token_mint}"
                                    open_trades_cache[cache_key] = sol_amount
                                    
                            elif tx_type == "sell":
                                logger.info(f"SELL DETECTED: {wallet[:8]}... | {sol_amount:.2f} SOL | Token: {token_mint[:8]}...")
                                cache_key = f"{wallet}:{token_mint}"
                                
                                if cache_key in open_trades_cache:
                                    buy_sol = open_trades_cache[cache_key]
                                    await close_trade_in_db_async(wallet, token_mint, sol_amount, buy_sol)
                                    del open_trades_cache[cache_key]
                                else:
                                    open_trade = await asyncio.get_event_loop().run_in_executor(
                                        executor, _get_open_trade_sync, wallet, token_mint
                                    )
                                    
                                    if open_trade:
                                        await close_trade_in_db_async(
                                            wallet, token_mint, sol_amount, open_trade["buy_sol"]
                                        )
                                        
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to decode message: {message[:100]}...")
                        stats["errors"] += 1
                    except Exception as e:
                        logger.error(f"Message Processing Error: {e}", exc_info=True)
                        stats["errors"] += 1
                        
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Websocket closed. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket Connection Error: {e}. Reconnecting in 10 seconds...", exc_info=True)
            stats["errors"] += 1
            await asyncio.sleep(10)

# === 7. MAIN ENTRY POINT ===

if __name__ == "__main__":
    setup_logging()
    logger.info("="*60)
    logger.info("SUPER AI AGENT STARTING (DEBUG MODE)")
    logger.info("="*60)
    logger.info(f"Config: MIN_BUY={MIN_BUY_SOL} SOL | MIN_TRADES={MIN_TRADES} | MIN_ROI={MIN_ROI}x")
    logger.info(f"Elite Threshold: {ELITE_THRESHOLD:.0%}")
    logger.info(f"Debug Mode: {DEBUG_MODE}")
    logger.info("="*60)
    
    try:
        asyncio.run(ws_listener())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        logger.info("Shutdown complete.")
        logger.info(f"Final Stats: Messages={stats['messages_received']} | Buys={stats['buys_tracked']} | Sells={stats['sells_detected']} | Errors={stats['errors']}")
