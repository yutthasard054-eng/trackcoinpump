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
        # === NEW IMPORTS FOR AI ===
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import pandas as pd
import joblib # Used for saving/loading the trained model

# === AI AGENT CONFIGURATION ===
MODEL_FILE = 'elite_wallet_model.pkl'

# --- AI MODEL FUNCTIONS ---

def load_training_data():
    """Fetches and prepares data for model training."""
    try:
        # Fetch all wallets with enough closed trades
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        
        data = resp.data if resp.data else []
        if not data:
            print("AI Trainer: Not enough data to train model yet.")
            return None, None
            
        df = pd.DataFrame(data)
        
        # 1. Feature Selection (X): The predictive features we engineered
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']]
        
        # 2. Target Variable (y): Convert status ('elite' or 'demoted') to binary (1 or 0)
        df['is_elite'] = df['status'].apply(lambda s: 1 if s == 'elite' else 0)
        y = df['is_elite']
        
        # Scale features for better model performance
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        # Save the scaler, as it must be used for future predictions
        joblib.dump(scaler, 'scaler.pkl') 
        
        return X_scaled, y
        
    except Exception as e:
        print(f"AI Trainer Error (load_training_data): {e}")
        return None, None

def train_model():
    """Trains and saves the Logistic Regression model."""
    X, y = load_training_data()
    if X is None:
        return
        
    print("AI Trainer: Starting model training...")
    
    # Simple split for training/testing
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # Initialize and train the model
    model = LogisticRegression()
    model.fit(X_train, y_train)
    
    # Evaluate (optional but good practice)
    score = model.score(X_test, y_test)
    print(f"AI Trainer: Model accuracy on test set: {score:.4f}")
    
    # Save the trained model to a file
    joblib.dump(model, MODEL_FILE)
    print(f"AI Trainer: Model saved to {MODEL_FILE}")

def predict_wallet_score(wallet_features):
    """Loads the model and predicts the probability of a wallet being elite."""
    try:
        # 1. Load the trained model and scaler
        model = joblib.load(MODEL_FILE)
        scaler = joblib.load('scaler.pkl')
        
        # 2. Prepare and scale the single feature vector
        features_df = pd.DataFrame([wallet_features], columns=['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc'])
        features_scaled = scaler.transform(features_df)
        
        # 3. Predict the probability of the positive class (1 = elite)
        # .predict_proba() returns probabilities for [0, 1]. We want the elite (1) probability.
        probability = model.predict_proba(features_scaled)[0][1]
        
        return probability
        
    except FileNotFoundError:
        print(f"AI Predictor: Model file {MODEL_FILE} not found. Needs training.")
        return 0.0 # Return 0 if the model hasn't been trained yet
    except Exception as e:
        print(f"AI Predictor Error: {e}")
        return 0.0

# ------------------------------
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
    """Runs periodically to train the model, calculate features, and predict scores."""
    # Run a training cycle at the start of the scoring loop
    train_model() 
    
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        print("🧠 Scoring wallets and generating features...")
        try:
            wallets = get_all_wallets()
            
            # Temporary storage for wallets that meet the minimum trade count
            ready_to_predict = {} 

            for wallet in wallets:
                
                # Fetch only closed trades to calculate features
                closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                closed_trades = closed_resp.data if closed_resp.data else []

                tokens_traded = len(closed_trades)
                
                # 1. Feature Calculation Block
                if tokens_traded < MIN_TRADES:
                    status = "candidate"
                    total_roi = 0
                    wins = 0
                    avg_hold_time = 0
                    avg_entry_mc = 0
                else:
                    closed_rois = [t["roi"] for t in closed_trades]
                    
                    # Feature 1: Avg Hold Time Calculation
                    hold_times_sec = [(t["sell_ts"] - t["buy_ts"]) for t in closed_trades if t["sell_ts"] and t["buy_ts"]]
                    avg_hold_time = (sum(hold_times_sec) / len(hold_times_sec)) / 60 if hold_times_sec else 0.0
                    
                    # Feature 2: Avg Entry Market Cap Calculation
                    entry_mcs = [t["entry_market_cap"] for t in closed_trades if t["entry_market_cap"] is not None]
                    avg_entry_mc = sum(entry_mcs) / len(entry_mcs) if entry_mcs else 0.0
                    
                    # Standard Stats
                    wins = len([r for r in closed_rois if r >= MIN_ROI])
                    total_roi = sum(closed_rois)

                    # Store for prediction
                    wallet_features = {
                        "tokens_traded": tokens_traded,
                        "avg_hold_time_min": avg_hold_time,
                        "avg_pump_entry_mc": avg_entry_mc
                    }
                    ready_to_predict[wallet] = (wallet_features, wins, total_roi)
                    
                    # Placeholder status before prediction runs
                    status = "evaluating" 
                
                # Update wallet record with new calculated features (before prediction)
                try:
                    supabase.table("wallets").update({
                        "tokens_traded": tokens_traded,
                        "wins": wins,
                        "total_roi": total_roi,
                        "avg_hold_time_min": avg_hold_time, 
                        "avg_pump_entry_mc": avg_entry_mc, 
                        "status": status,
                        "last_updated": int(time.time())
                    }).eq("address", wallet).execute()
                except Exception as db_update_e:
                    print(f"⚠️ DB Update Error for {wallet}: {db_update_e}")

            # --- AI PREDICTION STEP ---
            if ready_to_predict:
                print("AI Predictor: Starting live scoring...")
                for wallet, (features, wins, total_roi) in ready_to_predict.items():
                    # Get the predictive probability
                    elite_probability = predict_wallet_score(features)
                    
                    # The wallet status is now based on the AI's prediction!
                    # We use a 0.85 (85%) probability as the new "elite" threshold
                    if elite_probability >= 0.85:
                        status = "elite"
                    else:
                        status = "demoted"
                        
                    # Final update with AI score
                    supabase.table("wallets").update({
                        "status": status,
                        "elite_probability": elite_probability # You need to add this column to Supabase!
                    }).eq("address", wallet).execute()
                    
                    print(f"🧠 AI Score for {wallet}: {elite_probability:.4f} -> {status}")

            # Log elite list
            elite_resp = supabase.table("wallets").select("address, total_roi, elite_probability").eq("status", "elite").execute()
            elite = elite_resp.data if elite_resp.data else []
            # ... rest of your logging ...

        except Exception as e:
            print(f"⚠️ Critical Scoring Error: {e}")
            
# ------------------------------

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
