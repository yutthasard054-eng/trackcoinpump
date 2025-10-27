import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client
from solana.rpc.api import Client
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
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is required!")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

MIN_BUY_SOL = float(os.getenv("MIN_BUY_SOL", "0.1"))
MIN_TRADES = int(os.getenv("MIN_TRADES", "5"))
MIN_ROI = float(os.getenv("MIN_ROI", "3.0"))
ELITE_THRESHOLD = float(os.getenv("ELITE_THRESHOLD", "0.90"))
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "180"))

logger = logging.getLogger("PumpAI")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
executor = ThreadPoolExecutor(max_workers=5)

# === DATABASE FUNCTIONS ===
def save_buy(wallet, token_mint, sol_amount, market_cap=0):
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
            "address": wallet, "first_seen": ts, "last_updated": ts
        }).execute()
        logger.info(f"🛒 BUY: {wallet[:12]}... | {sol_amount:.4f} SOL | {token_mint[:12]}...")
        return True
    except Exception as e:
        if "23505" in str(e): return True
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
        logger.info(f"💰 SELL: {wallet[:12]}... | {token_mint[:12]}... | ROI: {roi:.2f}x")
        return True
    except Exception as e:
        logger.error(f"DB Error (close_trade): {e}")
        return False

def get_open_trades():
    try:
        resp = supabase.table("trades").select("id, wallet, token_mint, buy_sol, buy_ts").eq("status", "open").execute()
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

# === MARKET DATA FUNCTIONS ===
def get_token_market_cap(token_mint):
    """Fallback to Pump.fun (may fail — that's OK)"""
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/coins/{token_mint}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("usd_market_cap", 0)
        return 0
    except Exception as e:
        logger.debug(f"Error getting market cap for {token_mint[:12]}: {e}")
        return 0

def get_token_trades_via_helius(token_mint):
    """PRIMARY METHOD: Use Helius to fetch trades"""
    if not HELIUS_API_KEY:
        logger.warning("Helius API key not set - cannot fetch trades")
        return []
    
    url = f"https://api.helius.xyz/v0/addresses/{token_mint}/transactions"
    try:
        resp = requests.get(url, params={
            "api-key": HELIUS_API_KEY,
            "type": "TOKEN_PROGRAM_INSTRUCTION",
            "limit": 100
        }, timeout=10)
        
        if resp.status_code != 200:
            logger.debug(f"Helius returned {resp.status_code} for {token_mint[:12]}")
            return []
        
        txs = resp.json()
        trades = []
        
        for tx in txs:
            try:
                wallet = tx["transaction"]["message"]["accountKeys"][0]
                pre_bal = tx["meta"]["preBalances"][0]
                post_bal = tx["meta"]["postBalances"][0]
                sol_change = (pre_bal - post_bal) / 1e9
                sol_amount = abs(sol_change)
                tx_type = "buy" if sol_change > 0 else "sell"
                
                trades.append({
                    "user": wallet,
                    "type": tx_type,
                    "sol_amount": sol_amount,
                    "timestamp": tx.get("blockTime", 0)
                })
            except (KeyError, TypeError, IndexError):
                continue
        
        return trades
        
    except Exception as e:
        logger.debug(f"Helius trade fetch error for {token_mint[:12]}: {e}")
        return []

# === SELL DETECTION ===
def check_for_sells():
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
        
        # PRIMARY: Helius
        trades = get_token_trades_via_helius(mint)
        
        # FALLBACK: Pump.fun (if Helius returns nothing)
        if not trades:
            try:
                resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=50", timeout=5)
                if resp.status_code == 200:
                    raw_trades = resp.json()
                    for t in raw_trades:
                        trades.append({
                            "user": t.get("user") or t.get("traderPublicKey") or t.get("wallet"),
                            "type": t.get("type", "").lower(),
                            "sol_amount": t.get("sol_amount", 0),
                            "timestamp": t.get("timestamp", 0)
                        })
            except Exception as e:
                logger.debug(f"Pump.fun fallback failed for {mint[:12]}: {e}")
        
        # Check for matching sell
        for t in trades:
            user = t.get("user")
            tx_type = t.get("type", "").lower()
            timestamp = t.get("timestamp", 0)
            
            if (tx_type == "sell" and 
                user == wallet and 
                timestamp > buy_ts):
                
                sell_sol = t.get("sol_amount", 0)
                if close_trade_in_db(wallet, mint, sell_sol, buy_sol):
                    sells_found += 1
                    break
    
    if sells_found > 0:
        logger.info(f"✅ Found and closed {sells_found} sell(s)")
    else:
        logger.info("❌ No new sells found in this cycle")
    
    return sells_found

# === MACHINE LEARNING ===
def load_training_data():
    if not ML_AVAILABLE: return None, None
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        data = resp.data if resp.data else []
        if not data or len(data) < 10: 
            logger.warning(f"AI Trainer: Insufficient data ({len(data)} wallets. Need at least 10.")
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
    try:
        model_exists = os.path.exists('elite_wallet_model.pkl')
        scaler_exists = os.path.exists('scaler.pkl')
        logger.info(f"🤖 AI Model Status:")
        logger.info(f"  Model file exists: {model_exists}")
        logger.info(f"  Scaler file exists: {scaler_exists}")
        logger.info(f"  Helius API: {'✅ Configured' if HELIUS_API_KEY else '❌ Not configured'}")
        
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
            
            trades_resp = supabase.table("trades").select("status").execute()
            trades = trades_resp.data if trades_resp.data else []
            trade_counts = {}
            for t in trades:
                status = t.get("status", "unknown")
                trade_counts[status] = trade_counts.get(status, 0) + 1
            logger.info(f"📈 Trade Distribution:")
            for status, count in trade_counts.items():
                logger.info(f"  {status}: {count}")
            
            elite_wallets = [w for w in wallets if w.get("status") == "elite"]
            if elite_wallets:
                avg_prob = sum(w.get("elite_probability", 0) for w in elite_wallets) / len(elite_wallets)
                logger.info(f"🌟 Elite Wallets: {len(elite_wallets)} with avg probability: {avg_prob:.2%}")
    except Exception as e:
        logger.error(f"Debug error: {e}")

def periodic_debug():
    while True:
        time.sleep(600)
        debug_status()

# === SCORING LOGIC ===
def score_wallets():
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        logger.info("\n" + "="*60)
        logger.info("🔍 Starting AI scoring cycle...")
        logger.info("="*60)
        
        try:
            sells_found = check_for_sells()
            
            wallets_resp = supabase.table("wallets").select("address").execute()
            wallets = [w["address"] for w in (wallets_resp.data if wallets_resp.data else [])]
            
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
                avg_roi = sum(closed_rois) / len(closed_rois) if closed_rois else 0
                win_rate = wins / len(closed_rois) if closed_rois else 0
                
                elite_probability = 0.0
                
                if len(closed_trades) >= MIN_TRADES:
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    avg_roi = sum(closed_rois) / len(closed_rois) if closed_rois else 0
                    win_rate = wins / len(closed_trades) if closed_trades else 0
                    
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
            
            elite_resp = supabase.table("wallets").select("address, elite_probability, total_roi, tokens_traded").eq("status", "elite").execute()
            elite = elite_resp.data if elite_resp.data else []
            
            if elite:
                logger.info(f"\n🌟 Elite Wallets: {len(elite)} found")
                for w in elite[:5]:
                    logger.info(f"  - {w['address'][:12]}... | AI: {w.get('elite_probability', 0.0):.2%} | ROI: {w.get('total_roi', 0):.1f}x | Trades: {w.get('tokens_traded', 0)}")
            else:
                logger.info("\n⏳ No elite wallets yet. Keep collecting data...")
                
        except Exception as e:
            logger.error(f"Scoring Error: {e}")

# === WEBSOCKET LISTENER ===
async def main():
    threading.Thread(target=score_wallets, daemon=True).start()
    threading.Thread(target=periodic_debug, daemon=True).start()
    
    debug_status()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("✅ Connected to PumpPortal")
                
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                logger.info("✅ Subscribed to token creation and trade events")
                
                message_count = 0
                last_status = time.time()
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        message_count += 1
                        
                        if time.time() - last_status > 30:
                            logger.info(f"📊 Status: Received {message_count} messages in last 30s")
                            message_count = 0
                            last_status = time.time()
                        
                        if data.get("txType") == "create":
                            wallet = data.get("traderPublicKey")
                            sol = data.get("solAmount")
                            mint = data.get("mint")
                            market_cap = data.get("marketCapSol", 0)
                            if wallet and sol and mint and sol >= MIN_BUY_SOL:
                                save_buy(wallet, mint, sol, market_cap)
                        
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
    logger.info(f"🔍 Sell Detection: Helius (primary), Pump.fun (fallback)")
    logger.info("="*60)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n⏹️ Agent stopped by user.")
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}")
