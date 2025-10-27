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

# === AI / ML LIBRARIES ===
try:
    from sklearn.model_selection import train_test_split
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    import pandas as pd
    import joblib
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    # No need to raise an error, just disable features
    # logging.warning("ML libraries (scikit-learn, pandas, joblib) not found. AI features disabled.")

# === SOLANA / HELIUS LIBRARY ===
try:
    from solana.rpc.api import Client
except ImportError:
    Client = None
    # logging.warning("Solana library not found. Helius RPC features disabled.")

# === ENVIRONMENT VARIABLES & CONFIG ===
# Load environment variables if .env file exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass 

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://your-supabase-url.supabase.co") # Use your actual URL
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

if not SUPABASE_KEY:
    # Use logging.critical instead of raise to allow the container to potentially stay up for debugging
    logging.critical("SUPABASE_KEY environment variable is required! Exiting...")
    sys.exit(1)
if not SUPABASE_URL or "your-supabase-url" in SUPABASE_URL:
     logging.critical("SUPABASE_URL environment variable is required! Exiting...")
     sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_BUY_SOL = float(os.getenv("MIN_BUY_SOL", "0.5")) # Using optimized value
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))     # Using optimized value
MIN_ROI = float(os.getenv("MIN_ROI", "3.0"))       # Using optimized value
ELITE_THRESHOLD = float(os.getenv("ELITE_THRESHOLD", "0.90")) # Using optimized value
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "1800")) # Using optimized value (30 mins)

# Helius RPC endpoint (fallback to public Solana RPC)
HELIUS_RPC_URL = f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"

# === LOGGING SETUP ===
# Simple logging config, ensure PYTHONUNBUFFERED=1 in environment for reliability
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("PumpAI")
# Use a ThreadPoolExecutor for blocking calls within asyncio
executor = ThreadPoolExecutor(max_workers=10) 

# === DATABASE FUNCTIONS (Synchronous + Async Wrappers) ===

def _run_sync(func, *args, **kwargs):
    """Helper to run synchronous functions in the executor."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(executor, lambda: func(*args, **kwargs))

# Synchronous version of close_trade_in_db used inside the sync check_for_sells thread
def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        supabase.table("trades").update({
            "sell_sol": sell_sol, "sell_ts": int(time.time()), "roi": roi, "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        logger.info(f"💰 SELL: {wallet[:6]}... | {token_mint[:6]}... | ROI: {roi:.2f}x (Sync)")
        return True
    except Exception as e:
        logger.error(f"DB Error (close_trade sync): {e}")
        return False
        
def get_open_trades():
    """Synchronous version of get_open_trades used inside the sync check_for_sells thread."""
    try:
        resp = supabase.table("trades").select("id, wallet, token_mint, buy_sol, buy_ts").eq("status", "open").execute()
        return resp.data if resp.data else []
    except Exception as e:
        logger.error(f"DB Error (get_open_trades sync): {e}")
        return []

def update_wallet_status(wallet, tokens_traded, wins, total_roi, status, elite_probability=0.0):
    """Synchronous version of update_wallet_status used inside the sync score_wallets_sync thread."""
    try:
        data = {
            "tokens_traded": tokens_traded, "wins": wins, "total_roi": total_roi,
            "status": status, "last_updated": int(time.time())
        }
        if ML_AVAILABLE: 
            data["elite_probability"] = elite_probability
        supabase.table("wallets").update(data).eq("address", wallet).execute()
    except Exception as e:
        logger.error(f"DB Error (update_wallet_status sync): {e}")


async def save_buy_async(wallet, token_mint, sol_amount, market_cap=0):
    ts = int(time.time())
    try:
        await _run_sync(supabase.table("trades").upsert({
            "wallet": wallet, "token_mint": token_mint, "buy_sol": sol_amount,
            "buy_ts": ts, "entry_market_cap": market_cap, "status": "open"
        }, on_conflict="wallet, token_mint, status").execute)
        await _run_sync(supabase.table("wallets").upsert({
            "address": wallet, "first_seen": ts, "last_updated": ts
        }).execute)
        logger.info(f"🛒 BUY: {wallet[:6]}... | {sol_amount:.4f} SOL | {token_mint[:6]}...")
        return True
    except Exception as e:
        if "23505" in str(e): return True # Suppress unique constraint violation
        logger.error(f"DB Error (save_buy): {e}")
        return False


# === HELIUS RPC FUNCTIONS (Synchronous) ===

_helius_client = None
def get_helius_client():
    global _helius_client
    if _helius_client:
        return _helius_client
    if not HELIUS_API_KEY:
        logger.warning("Helius API key not configured - using public RPC")
        if Client:
            _helius_client = Client(HELIUS_RPC_URL) # Fallback public
            return _helius_client
        else:
            logger.error("Solana library not installed, cannot create RPC client.")
            return None
    try:
        if Client:
            _helius_client = Client(HELIUS_RPC_URL)
            logger.info("Helius client initialized.")
            return _helius_client
        else:
             logger.error("Solana library not installed, cannot create Helius client.")
             return None
    except Exception as e:
        logger.error(f"Failed to initialize Helius client: {e}")
        return None

def parse_pump_transaction(signature):
    """Parse Pump.fun transaction using Helius (BLOCKING). Needs refinement."""
    # Placeholder implementation based on the user's code. This is currently non-functional
    return None

def get_token_transactions(mint, limit=20):
    """Get recent transaction signatures involving a mint address (BLOCKING)."""
    try:
        client = get_helius_client()
        if not client: return []
        
        signatures_response = client.get_signatures_for_address(mint, limit=limit)
        signatures = signatures_response.value if signatures_response else []
        
        return [sig.signature for sig in signatures]
    except Exception as e:
        logger.debug(f"Error getting Helius tx for {mint[:6]}...: {e}")
        return []

# === PUMP.FUN API FUNCTIONS (Synchronous) ===

def get_pump_trades(token_mint, limit=100):
    """Get recent trades from Pump.fun API (BLOCKING)."""
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/trades/{token_mint}?limit={limit}", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        logger.debug(f"Error getting Pump trades for {token_mint[:6]}...: {e}")
        return []

# === SELL DETECTION (Synchronous - Called within scoring thread) ===
def check_for_sells(open_trades):
    """Check for sells using Pump.fun API and Helius RPC fallback (BLOCKING)."""
    if not open_trades: return 0
    
    logger.info(f"🔍 Checking {len(open_trades)} open trades for sells...")
    sells_found = 0
    
    for trade in open_trades:
        mint = trade["token_mint"]
        wallet = trade["wallet"]
        buy_sol = trade["buy_sol"]
        buy_ts = trade.get("buy_ts", 0)
        trade_closed = False
        
        # Method 1: Check via Pump.fun API (Primary)
        try:
            pump_trades = get_pump_trades(mint, 50) # Blocking call
            for t in pump_trades:
                user = t.get("traderPublicKey") or t.get("user") or t.get("wallet")
                tx_type = t.get("type", "").lower()
                timestamp = t.get("timestamp", 0)
                
                # Check for sell by the same wallet *after* the buy timestamp
                if (tx_type == "sell" and user == wallet and timestamp > buy_ts):
                    sell_sol = t.get("sol_amount", 0)
                    # Use the synchronous version directly since we are in a sync thread
                    if close_trade_in_db(wallet, mint, sell_sol, buy_sol): 
                        sells_found += 1
                        trade_closed = True
                        break # Move to next open trade
        except Exception as e:
            logger.debug(f"Pump.fun API error for {mint[:6]}...: {e}")
            
        # Method 2: Check via Helius RPC (Fallback if Pump API missed it and Helius configured)
        # Helius parsing is currently non-functional, so this is mostly a placeholder
        if not trade_closed and HELIUS_API_KEY and Client:
            try:
                signatures = get_token_transactions(mint, 20) # Blocking call
                for signature in signatures:
                    parsed_trade = parse_pump_transaction(signature) # Blocking call
                    
                    if (parsed_trade and 
                        parsed_trade.get('type') == 'sell' and 
                        parsed_trade.get('user') == wallet and
                        parsed_trade.get('token_mint') == mint):
                        
                        # Use the synchronous version directly
                        if close_trade_in_db(wallet, mint, parsed_trade.get('sol_amount', 0), buy_sol): 
                            sells_found += 1
                            trade_closed = True
                            break # Move to next open trade
                if trade_closed: continue # Move to next outer loop trade

            except Exception as e:
                logger.debug(f"Helius RPC error for {mint[:6]}...: {e}")
                
    if sells_found > 0:
        logger.info(f"✅ Found and closed {sells_found} sell(s)")
    else:
        logger.info("❌ No new sells found in this polling cycle")
        
    return sells_found

# === DEBUG FUNCTIONS (Synchronous) ===
def debug_status():
    """Check current system status (BLOCKING DB calls)."""
    try:
        model_exists = os.path.exists('elite_wallet_model.pkl') if ML_AVAILABLE else False
        scaler_exists = os.path.exists('scaler.pkl') if ML_AVAILABLE else False
        
        logger.info(f"🤖 AI Model Status:")
        logger.info(f"  ML Available: {ML_AVAILABLE}")
        logger.info(f"  Model file exists: {model_exists}")
        logger.info(f"  Scaler file exists: {scaler_exists}")
        logger.info(f"  Helius API: {'✅ Configured' if HELIUS_API_KEY and Client else '❌ Not configured/Installed'}")
        
        wallets_resp = supabase.table("wallets").select("status, elite_probability").execute()
        wallets = wallets_resp.data if wallets_resp.data else []
        
        if wallets:
            status_counts = {}
            for w in wallets:
                status = w.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            logger.info(f"📊 Wallet Distribution ({len(wallets)} total):")
            for status, count in status_counts.items(): logger.info(f"  {status}: {count}")
            
            trades_resp = supabase.table("trades").select("status").execute()
            trades = trades_resp.data if trades_resp.data else []
            trade_counts = {}
            for t in trades:
                status = t.get("status", "unknown")
                trade_counts[status] = trade_counts.get(status, 0) + 1
            logger.info(f"📈 Trade Distribution ({len(trades)} total):")
            for status, count in trade_counts.items(): logger.info(f"  {status}: {count}")
            
            elite_wallets = [w for w in wallets if w.get("status") == "elite"]
            if elite_wallets:
                avg_prob = sum(w.get("elite_probability", 0) for w in elite_wallets) / len(elite_wallets)
                logger.info(f"🌟 Elite Wallets: {len(elite_wallets)} with avg probability: {avg_prob:.2%}")
    except Exception as e:
        logger.error(f"Debug error: {e}")

def periodic_debug():
    """Run debug status every 10 minutes (BLOCKING)."""
    while True:
        time.sleep(600) 
        debug_status()

# === MACHINE LEARNING FUNCTIONS (Synchronous - Called within scoring thread) ===
def load_training_data():
    if not ML_AVAILABLE: return None, None
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        data = resp.data if resp.data else []
        if not data or len(data) < 10: 
            logger.warning(f"AI Trainer: Insufficient data ({len(data)} wallets. Need at least 10.")
            return None, None
        df = pd.DataFrame(data)
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']].fillna(0) # Fill NaN with 0
        df['is_elite'] = df['status'].apply(lambda s: 1 if s == 'elite' else 0)
        y = df['is_elite']
        if len(y.unique()) < 2:
            logger.warning("AI Trainer: Only one class present in training data.")
            return None, None
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        joblib.dump(scaler, 'scaler.pkl')
        return X_scaled, y
    except Exception as e:
        logger.error(f"AI Trainer Error (load_data): {e}")
        return None, None

def train_model():
    if not ML_AVAILABLE: return False
    # Use sync DB call
    X, y = load_training_data() 
    if X is None or len(X) == 0:
        logger.warning("AI Trainer: Cannot train - insufficient data.")
        return False
    logger.info("🧠 Training AI model...")
    try:
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        model = RandomForestClassifier(n_estimators=100, class_weight='balanced', random_state=42, max_depth=5, n_jobs=-1)
        model.fit(X_train, y_train)
        train_accuracy = model.score(X_train, y_train)
        test_accuracy = model.score(X_test, y_test)
        logger.info(f"Model Performance: Train={train_accuracy:.2%}, Test={test_accuracy:.2%}")
        joblib.dump(model, 'elite_wallet_model.pkl') 
        logger.info("✅ AI model trained and saved")
        return True
    except Exception as e:
        logger.error(f"AI Model Training Failed: {e}", exc_info=True)
        return False


def predict_wallet_score(wallet_features):
    if not ML_AVAILABLE: return 0.0
    try:
        model = joblib.load('elite_wallet_model.pkl') 
        scaler = joblib.load('scaler.pkl') 
        features_df = pd.DataFrame([wallet_features], columns=['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']).fillna(0)
        features_scaled = scaler.transform(features_df)
        probability = model.predict_proba(features_scaled)[0][1] 
        return probability
    except FileNotFoundError:
        return 0.0 
    except Exception as e:
        logger.error(f"AI Predictor Error: {e}")
        return 0.0

# === SCORING LOGIC (Synchronous - Runs in its own thread) ===
def score_wallets_sync():
    """Main scoring loop - runs blocking operations."""
    while True:
        time.sleep(CHECK_INTERVAL_SEC) # Blocking sleep
        logger.info("\n" + "="*60)
        logger.info("🧠 Starting AI scoring cycle...")
        logger.info("="*60)
        
        try:
            # Check for sells (blocking network/DB calls)
            open_trades = get_open_trades() # Blocking DB call (sync version)
            check_for_sells(open_trades) # Blocking API/DB calls inside
            
            # Re-fetch all wallets for scoring
            wallets_resp = supabase.table("wallets").select("address").execute() # Blocking DB call
            
            # FIX: Syntax error fix
            wallets_data = wallets_resp.data if wallets_resp.data else []
            wallets = [w["address"] for w in wallets_data]
            
            # Train model if possible (blocking file/CPU/DB)
            trained = False
            if ML_AVAILABLE:
                trained = train_model() # Blocking call
                if not trained:
                    logger.warning("Model training skipped/failed - insufficient data or error.")
            
            logger.info(f"Scoring {len(wallets)} wallets...")
            elite_count = 0
            
            for wallet in wallets:
                # Blocking DB call
                closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute() 
                closed_trades = closed_resp.data if closed_resp.data else []
                tokens_traded = len(closed_trades)
                
                status = "candidate"
                wins = 0
                total_roi = 0.0
                elite_probability = 0.0 # Default probability
                
                if tokens_traded >= MIN_TRADES:
                    closed_rois = [t.get("roi", 0) or 0 for t in closed_trades] # Handle None ROI
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    total_roi = sum(closed_rois)
                    
                    if ML_AVAILABLE and trained: 
                        # Calculate features needed for ML
                        hold_times_sec = [(t["sell_ts"] - t["buy_ts"]) for t in closed_trades if t.get("sell_ts") and t.get("buy_ts")]
                        avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                        entry_mcs = [t.get("entry_market_cap", 0) or 0 for t in closed_trades] 
                        avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                        
                        wallet_features = {
                            "tokens_traded": tokens_traded,
                            "avg_hold_time_min": avg_hold_time,
                            "avg_pump_entry_mc": avg_entry_mc
                        }
                        # Blocking ML prediction
                        elite_probability = predict_wallet_score(wallet_features) 
                        status = "elite" if elite_probability >= ELITE_THRESHOLD else "evaluating" 
                    else:
                        # Fallback to simple rules if ML not available or not trained
                        avg_roi = total_roi / tokens_traded if tokens_traded else 0
                        win_rate = wins / tokens_traded if tokens_traded else 0
                        status = "elite" if win_rate >= 0.6 and avg_roi >= MIN_ROI else "evaluating"
                        
                # Blocking DB update (sync version)
                update_wallet_status(wallet, tokens_traded, wins, total_roi, status, elite_probability)
                
                if status == "elite":
                    elite_count += 1
                    
            # Log summary
            elite_resp = supabase.table("wallets").select("address").eq("status", "elite").execute() # Blocking DB call
            elite = elite_resp.data if elite_resp.data else []
            if elite:
                logger.info(f"\n🌟 Elite Summary: {len(elite)} wallets marked as elite.")
            else:
                logger.info("\n⏳ No elite wallets found in this cycle.")
                
        except Exception as e:
            logger.error(f"Critical Scoring Error: {e}", exc_info=True)

# === WEBSOCKET LISTENER ===
async def main():
    # Start background threads using threading.Thread (since they run blocking code)
    threading.Thread(target=score_wallets_sync, daemon=True, name="ScoringThread").start()
    threading.Thread(target=periodic_debug, daemon=True, name="DebugThread").start()
    
    # Initial status check (run blocking function in executor)
    await _run_sync(debug_status) 
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("✅ Connected to PumpPortal WebSocket")
                
                # Subscribe to relevant events
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                logger.info("✅ Subscribed to token creation and trade events")
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        
                        method = data.get("method")
                        trade_data = data.get("data", {})
                        
                        # Handle initial buys from 'newToken' or 'create' type events
                        if method == "newToken" or trade_data.get("txType") == "create":
                            wallet = trade_data.get("traderPublicKey") or trade_data.get("creator")
                            sol_str = trade_data.get("solAmount")
                            mint = trade_data.get("mint")
                            mc_sol = trade_data.get("marketCapSol", 0) 

                            if wallet and sol_str and mint:
                                try:
                                    sol = float(sol_str)
                                    if sol >= MIN_BUY_SOL:
                                        # Run blocking DB call in executor
                                        await save_buy_async(wallet, mint, sol, mc_sol) 
                                except (ValueError, TypeError):
                                    logger.warning(f"Could not parse solAmount: {sol_str}")

                        # Handle general trades 
                        elif method == "tokenTrade" and trade_data:
                            wallet = trade_data.get("wallet")
                            sol_str = trade_data.get("sol_amount")
                            mint = trade_data.get("mint")
                            tx_type = trade_data.get("tx_type", "").upper()

                            if wallet and sol_str and mint:
                                try:
                                    sol = float(sol_str)
                                    if tx_type == "BUY" and sol >= MIN_BUY_SOL:
                                         # Check if trade exists before saving again (run DB call in executor)
                                         exists_resp = await _run_sync(supabase.table("trades").select("id").eq("wallet", wallet).eq("token_mint", mint).eq("status", "open").execute)
                                         if not exists_resp.data:
                                             await save_buy_async(wallet, mint, sol) # Run blocking DB call in executor
                                         
                                except (ValueError, TypeError):
                                     logger.warning(f"Could not parse sol_amount for trade: {sol_str}")
                                     
                    except json.JSONDecodeError:
                        logger.warning("Failed to decode JSON message.")
                    except Exception as e:
                        logger.error(f"Message Processing Error: {e}", exc_info=False)
                        
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket closed. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket Connection Error: {e}. Reconnecting in 10 seconds...", exc_info=False)
            await asyncio.sleep(10)

if __name__ == "__main__":
    # Removed the undefined call to setup_logging()
    logger.info("\n" + "="*60)
    logger.info("🚀 PUMP.AI SUPER AGENT STARTING")
    logger.info("="*60)
    logger.info(f"⚙️ Config: MIN_BUY={MIN_BUY_SOL} SOL | MIN_TRADES={MIN_TRADES} | MIN_ROI={MIN_ROI}x")
    logger.info(f"🎯 Elite Threshold: {ELITE_THRESHOLD:.0%}")
    logger.info(f"🤖 ML Available: {ML_AVAILABLE}")
    logger.info(f"🕒 Scoring Interval: {CHECK_INTERVAL_SEC} seconds")
    logger.info(f"🔍 Sell Detection: Pump.fun API + Helius RPC (Fallback: {HELIUS_API_KEY is not None and Client is not None})")
    logger.info("="*60)
    
    # Add PYTHONUNBUFFERED=1 to your Railway environment variables
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n⏹️ Agent stopped by user.")
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}", exc_info=True)
