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

# === CONFIG ===
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://pnvvnlcooykoqoebgfom.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is required!")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_BUY_SOL = float(os.getenv("MIN_BUY_SOL", "0.1"))
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))
MIN_ROI = float(os.getenv("MIN_ROI", "3.0"))
ELITE_THRESHOLD = float(os.getenv("ELITE_THRESHOLD", "0.90"))
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))  # 3 minutes

# Helius RPC endpoint
HELIUS_RPC_URL = f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"

logger = logging.getLogger("PumpAI")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
executor = ThreadPoolExecutor(max_workers=5)

# === DATABASE FUNCTIONS ===
def save_buy(wallet, token_mint, sol_amount, market_cap=0, signature=None):
    ts = int(time.time())
    try:
        data = {
            "wallet": wallet, 
            "token_mint": token_mint, 
            "buy_sol": sol_amount,
            "buy_ts": ts, 
            "entry_market_cap": market_cap,
            "status": "open"
        }
        if signature:
            data["signature"] = signature
        supabase.table("trades").upsert(data, on_conflict="wallet, token_mint, status").execute()
        supabase.table("wallets").upsert({
            "address": wallet, "first_seen": ts, "last_updated": ts
        }).execute()
        logger.info(f"🛒 BUY: {wallet[:12]}... | {sol_amount:.4f} SOL | {token_mint[:12]}...")
        return True
    except Exception as e:
        if "23505" in str(e): return True
        logger.error(f"DB Error (save_buy): {e}")
        return False

def close_trade_in_db(wallet, token_mint, sell_sol, buy_sol, signature=None):
    roi = sell_sol / buy_sol if buy_sol > 0 else 0
    try:
        data = {
            "sell_sol": sell_sol, 
            "sell_ts": int(time.time()), 
            "roi": roi, 
            "status": "closed"
        }
        if signature:
            data["sell_signature"] = signature
        supabase.table("trades").update(data).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        logger.info(f"💰 SELL: {wallet[:12]}... | {token_mint[:12]}... | ROI: {roi:.2f}x")
        return True
    except Exception as e:
        logger.error(f"DB Error (close_trade): {e}")
        return False

def get_open_trades():
    try:
        resp = supabase.table("trades").select("id, wallet, token_mint, buy_sol, buy_ts, signature").eq("status", "open").execute()
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
        data["elite_probability"] = elite_probability
        supabase.table("wallets").update(data).eq("address", wallet).execute()
    except Exception as e:
        logger.error(f"DB Error (update_wallet_status): {e}")

# === HELIUS RPC FUNCTIONS ===
def get_helius_client():
    """Initialize Helius RPC client"""
    if not HELIUS_API_KEY:
        logger.warning("Helius API key not configured - using public RPC")
        return None
    try:
        client = Client(HELIUS_RPC_URL)
        return client
    except Exception as e:
        logger.error(f"Failed to initialize Helius client: {e}")
        return None

def parse_pump_transaction(signature):
    """Parse Pump.fun transaction to detect buys/sells"""
    try:
        client = get_helius_client()
        if not client:
            return None
            
        tx = client.get_transaction(signature)
        
        if not tx or not tx.get('meta'):
            return None
            
        # Check for successful transaction
        if tx['meta']['err']:
            return None
            
        # Look for Raydium instructions (Pump.fun uses Raydium)
        for instruction in tx['transaction']['message']['instructions']:
            if instruction['program'] == '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwFyPQJgNEJqPFSoRN9U8RfGvrtECpDQDLtBjFj6QFJZZpEkzz7L':
                # This is a Raydium swap instruction
                inner_instructions = instruction['parsed']['info']['innerInstructions']
                
                for inner in inner_instructions:
                    if inner['type'] == 'swap':
                        swap_info = inner['info']['swap']
                        
                        # Extract swap details
                        input_a = swap_info.get('inputA', '')
                        input_b = swap_info.get('inputB', '')
                        output_a = swap_info.get('outputA', '')
                        output_b = swap_info.get('outputB', '')
                        
                        # Determine swap direction
                        if 'So111111111' in input_a and 'So111111111' not in output_b:
                            # SOL in, Token out = BUY
                            token_mint = output_b.split(' ')[0] if ' ' in output_b else output_b
                            sol_amount = float(input_a.replace('So111111111', ''))
                            return {
                                'type': 'buy',
                                'token_mint': token_mint,
                                'sol_amount': sol_amount,
                                'user': tx['transaction']['message']['accountKeys'][0],
                                'signature': signature
                            }
                        elif 'So111111111' in output_a and 'So111111111' not in input_b:
                            # Token in, SOL out = SELL
                            token_mint = input_a.split(' ')[0] if ' ' in input_a else input_a
                            sol_amount = float(output_a.replace('So111111111', ''))
                            return {
                                'type': 'sell',
                                'token_mint': token_mint,
                                'sol_amount': sol_amount,
                                'user': tx['transaction']['message']['accountKeys'][0],
                                'signature': signature
                            }
    except Exception as e:
        logger.debug(f"Error parsing Pump transaction {signature[:12]}: {e}")
        return None

def get_token_account(mint):
    """Get token account for a mint address"""
    try:
        client = get_helius_client()
        if not client:
            return None
            
        token_accounts = client.get_token_accounts_by_owner(mint)
        return token_accounts[0].pubkey if token_accounts else None
    except Exception as e:
        logger.debug(f"Error getting token account for {mint[:12]}: {e}")
        return None

def get_token_transactions(mint, limit=20):
    """Get recent transactions for a token"""
    try:
        client = get_helius_client()
        if not client:
            return []
            
        token_account = get_token_account(mint)
        if not token_account:
            return []
            
        signatures = client.get_signatures_for_address(
            token_account,
            limit=limit,
            before=None,
            until=None
        )
        
        return [sig["signature"] for sig in signatures]
    except Exception as e:
        logger.debug(f"Error getting token transactions for {mint[:12]}: {e}")
        return []

# === PUMP.FUN API FUNCTIONS ===
def get_token_market_cap(token_mint):
    """Get current market cap from Pump.fun API"""
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/coins/{token_mint}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("usd_market_cap", 0)
        return 0
    except Exception as e:
        logger.debug(f"Error getting market cap for {token_mint[:12]}: {e}")
        return 0

def get_pump_trades(token_mint, limit=100):
    """Get recent trades from Pump.fun API"""
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/trades/{token_mint}?limit={limit}", timeout=5)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        logger.debug(f"Error getting trades for {token_mint[:12]}: {e}")
        return []

# === SELL DETECTION ===
def check_for_sells():
    """Check for sells using multiple methods"""
    open_trades = get_open_trades()
    if not open_trades:
        return 0
    
    logger.info(f"🔍 Checking {len(open_trades)} open trades for sells...")
    sells_found = 0
    
    for trade in open_trades:
        mint = trade["token_mint"]
        wallet = trade["wallet"]
        buy_sol = trade["buy_sol"]
        buy_ts = trade.get("buy_ts", 0)
        
        # Method 1: Check via Pump.fun API
        try:
            trades = get_pump_trades(mint, 50)
            for t in trades:
                # Try multiple field names for user/wallet
                user = t.get("user") or t.get("traderPublicKey") or t.get("wallet")
                tx_type = t.get("type", "").lower()
                timestamp = t.get("timestamp", 0)
                
                if (tx_type == "sell" and 
                    user == wallet and 
                    timestamp > buy_ts):
                    
                    sell_sol = t.get("sol_amount", 0)
                    if close_trade_in_db(wallet, mint, sell_sol, buy_sol):
                        sells_found += 1
                        break
        except Exception as e:
            logger.debug(f"Pump.fun API error for {mint[:12]}: {e}")
        
        # Method 2: Check via Helius RPC (if first method didn't find sells)
        if sells_found == 0 and HELIUS_API_KEY:
            try:
                signatures = get_token_transactions(mint, 20)
                for signature in signatures:
                    trade_data = parse_pump_transaction(signature)
                    
                    if (trade_data and 
                        trade_data['type'] == 'sell' and 
                        trade_data['user'] == wallet and
                        trade_data['token_mint'] == mint):
                        
                        if close_trade_in_db(wallet, mint, trade_data['sol_amount'], buy_sol, trade_data['signature']):
                            sells_found += 1
                            break
                            
            except Exception as e:
                logger.debug(f"RPC error for {mint[:12]}: {e}")
    
    if sells_found > 0:
        logger.info(f"✅ Found and closed {sells_found} sell(s)")
    else:
        logger.info("❌ No new sells found in this cycle")
    
    return sells_found

# === MACHINE LEARNING ===
try:
    from sklearn.model_selection import train_test_split
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    import pandas as pd
    import joblib
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

def load_training_data():
    if not ML_AVAILABLE: return None, None
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        data = resp.data if resp.data else []
        if not data or len(data) < 10: 
            logger.warning(f"AI Trainer: Insufficient data ({len(data)} wallets). Need at least 10.")
            return None, None
        df = pd.DataFrame(data)
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']]
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
        logger.error(f"AI Trainer Error: {e}")
        return None, None

def train_model():
    if not ML_AVAILABLE: return False
    X, y = load_training_data()
    if X is None or len(X) == 0:
        logger.warning("AI Trainer: Cannot train - insufficient data.")
        return False
    logger.info("🧠 Training AI model...")
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    model = RandomForestClassifier(n_estimators=100, class_weight='balanced', random_state=42, max_depth=5)
    model.fit(X_train, y_train)
    train_accuracy = model.score(X_train, y_train)
    test_accuracy = model.score(X_test, y_test)
    logger.info(f"Model Performance: Train={train_accuracy:.2%}, Test={test_accuracy:.2%}")
    joblib.dump(model, 'elite_wallet_model.pkl')
    logger.info("✅ AI model trained and saved")
    return True

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

# === DEBUG FUNCTIONS ===
def debug_status():
    """Check current system status"""
    try:
        # Check model files
        model_exists = os.path.exists('elite_wallet_model.pkl')
        scaler_exists = os.path.exists('scaler.pkl')
        
        logger.info(f"🤖 AI Model Status:")
        logger.info(f"  Model file exists: {model_exists}")
        logger.info(f"  Scaler file exists: {scaler_exists}")
        logger.info(f"  Helius API: {'✅ Configured' if HELIUS_API_KEY else '❌ Not configured'}")
        
        # Check wallet data
        wallets_resp = supabase.table("wallets").select("status, elite_probability, tokens_traded").execute()
        wallets = wallets_resp.data if wallets_resp.data else []
        
        if wallets:
            status_counts = {}
            for w in wallets:
                status = w.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            
            logger.info(f"📊 Wallet Distribution:")
            for status, count in status_counts.items():
                logger.info(f"  {status}: {count}")
            
            # Check trades
            trades_resp = supabase.table("trades").select("status").execute()
            trades = trades_resp.data if trades_resp.data else []
            trade_counts = {}
            for t in trades:
                status = t.get("status", "unknown")
                trade_counts[status] = trade_counts.get(status, 0) + 1
            
            logger.info(f"📈 Trade Distribution:")
            for status, count in trade_counts.items():
                logger.info(f"  {status}: {count}")
            
            # Show elite wallets
            elite_wallets = [w for w in wallets if w.get("status") == "elite"]
            if elite_wallets:
                avg_prob = sum(w.get("elite_probability", 0) for w in elite_wallets) / len(elite_wallets)
                logger.info(f"🌟� Elite Wallets: {len(elite_wallets)} with avg probability: {avg_prob:.2%}")
    except Exception as e:
        logger.error(f"Debug error: {e}")

def periodic_debug():
    """Run debug status every 10 minutes"""
    while True:
        time.sleep(600)  # 10 minutes
        debug_status()

# === SCORING LOGIC ===
def score_wallets():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        logger.info("\n" + "="*60)
        logger.info("🔍 Starting AI scoring cycle...")
        logger.info("="*60)
        
        try:
            # Check for sells
            sells_found = check_for_sells()
            
            # Update wallet stats
            wallets_resp = supabase.table("wallets").select("address").execute()
            wallets = [w["address"] for w in wallets_resp.data] if wallets_resp.data else []
            
            # Train model if possible
            trained = train_model()
            if not trained:
                logger.warning("Model training skipped - insufficient data")
            
            logger.info(f"Scoring {len(wallets)} wallets...")
            elite_count = 0
            
            for wallet in wallets:
                closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                closed_trades = closed_resp.data if closed_resp.data else []
                closed_rois = [t["roi"] for t in closed_trades]
                wins = 0
                avg_roi = 0
                elite_probability = 0.0
                
                if len(closed_trades) >= MIN_TRADES:
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    avg_roi = sum(closed_rois) / len(closed_rois)
                    win_rate = wins / len(closed_rois)
                    
                    if ML_AVAILABLE:
                        hold_times_sec = []
                        entry_mcs = []
                        for t in closed_trades:
                            if t.get("sell_ts") and t.get("buy_ts"):
                                hold_times_sec.append(t["sell_ts"] - t["buy_ts"])
                            if t.get("entry_market_cap") is not None:
                                entry_mcs.append(t["entry_market_cap"])
                        avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                        avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                        wallet_features = {
                            "tokens_traded": len(closed_trades),
                            "avg_hold_time_min": avg_hold_time,
                            "avg_pump_entry_mc": avg_entry_mc
                        }
                        elite_probability = predict_wallet_score(wallet_features)
                        status = "elite" if elite_probability >= ELITE_THRESHOLD else "evaluating"
                    else:
                        status = "elite" if win_rate >= 0.6 and avg_roi >= MIN_ROI else "evaluating"
                else:
                    status = "candidate"
                
                update_wallet_status(wallet, len(closed_trades), wins, avg_roi, status, elite_probability)
                if status == "elite":
                    elite_count += 1
                    logger.info(f"✅ ELITE FOUND: {wallet[:12]}... | AI: {elite_probability:.2%} | ROI: {avg_roi:.1f}x | Trades: {len(closed_trades)}")
            
            # Log elite wallets summary
            elite_resp = supabase.table("wallets").select("address, elite_probability, total_roi, tokens_traded").eq("status", "elite").execute()
            elite = elite_resp.data if elite_resp.data else []
            
            if elite:
                logger.info(f"\n🌟� ELITE WALLETS: {len(elite)} found")
                for w in elite[:5]:  # Show top 5
                    logger.info(f"  - {w['address'][:12]}... | AI: {w.get('elite_probability', 0.0):.2%} | ROI: {w.get('total_roi', 0):.1f}x | Trades: {w.get('tokens_trades', 0)}")
            else:
                logger.info("\n⏳ No elite wallets yet. Keep collecting data...")
                
        except Exception as e:
            logger.error(f"Scoring Error: {e}")

# === WEBSOCKET LISTENER ===
async def main():
    # Start background threads
    threading.Thread(target=score_wallets, daemon=True).start()
    threading.Thread(target=periodic_debug, daemon=True).start()
    
    # Initial debug status
    debug_status()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("✅ Connected to PumpPortal")
                
                # Subscribe to ALL new tokens and trades
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                logger.info("✅ Subscribed to token creation and trade events")
                
                message_count = 0
                last_status = time.time()
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        message_count += 1
                        
                        # Status update every 30 seconds
                        if time.time() - last_status > 30:
                            logger.info(f"📊 Status: Received {message_count} messages in last 30s")
                            message_count = 0
                            last_status = time.time()
                        
                        # Handle token creation events (initial buys)
                        if data.get("txType") == "create":
                            wallet = data.get("traderPublicKey")
                            sol = data.get("solAmount")
                            mint = data.get("mint")
                            market_cap = data.get("marketCapSol", 0)
                            signature = data.get("signature")
                            if wallet and sol and mint and sol >= MIN_BUY_SOL:
                                save_buy(wallet, mint, sol, market_cap, signature)
                        
                        # Handle regular trade events (buys and sells)
                        elif data.get("txType") in ["buy", "sell"]:
                            wallet = data.get("traderPublicKey")
                            sol = data.get("solAmount")
                            mint = data.get("mint")
                            signature = data.get("signature")
                            
                            if data.get("txType") == "buy" and wallet and sol and mint and sol >= MIN_BUY_SOL:
                                # For regular buys, check if we already have an open trade
                                open_trade = supabase.table("trades").select("id").eq("wallet", wallet).eq("token_mint", mint).eq("status", "open").execute()
                                if not open_trade.data:  # Only save if no open trade exists
                                    market_cap = data.get("marketCapSol", 0)
                                    save_buy(wallet, mint, sol, market_cap, signature)
                            
                            elif data.get("txType") == "sell" and wallet and mint:
                                # For sells, check if we have an open trade to close
                                open_trade = supabase.table("trades").select("id, buy_sol").eq("wallet", wallet).eq("token_mint", mint).eq("status", "open").execute()
                                if open_trade.data:
                                    close_trade_in_db(wallet, mint, sol, open_trade.data[0]["buy_sol"], signature)
                                
                    except Exception as e:
                        logger.error(f"Message Error: {e}")
        except Exception as e:
            logger.error(f"WebSocket Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    logger.info("\n" + "="*60)
    logger.info("🚀 PUMP.AI AGENT STARTING")
    logger.info("="*60)
    logger.info(f"⚙️ Config: MIN_BUY={MIN_BUY_SOL} SOL | MIN_TRADES={MIN_TRADES} | MIN_ROI={MIN_ROI}x")
    logger.info(f"🎯 Elite Threshold: {ELITE_THRESHOLD:.0%}")
    logger.info(f"🤖 ML Available: {ML_AVAILABLE}")
    logger.info(f"📊 Scoring Interval: {CHECK_INTERVAL_SEC} seconds")
    logger.info(f"🔍 Sell Detection: Pump.fun API + Helius RPC")
    logger.info("="*60)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n⏹️ Agent stopped by user.")
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}")
