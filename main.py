import asyncio
import websockets
import json
import requests
import time
import threading
from supabase import create_client

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
# Increased to 0.5 SOL to focus on higher-conviction, potentially less-bot-driven trades
MIN_BUY_SOL = 0.5    
# Increased to 5 trades for richer feature data and more reliable AI scoring
MIN_TRADES = 5       
# Increased to 3.0x to train the AI on wallets that find SPECTACULAR pumps
MIN_ROI = 3.0        
# Set to 30 minutes for stability, reduced database load, and efficient ML scoring
CHECK_INTERVAL_SEC = 1800 
TOKEN_INFO_URL = "https://frontend-api.pump.fun/tokens/" 

# === AI AGENT CONFIGURATION ===
MODEL_FILE = 'elite_wallet_model.pkl'

# === DATA ENRICHMENT FUNCTIONS ===

def get_market_cap(token_mint):
    """Fetches the current market cap for a given token."""
    try:
        response = requests.get(f"{TOKEN_INFO_URL}{token_mint}", timeout=5)
        response.raise_for_status() 
        data = response.json()
        market_cap = data.get("market_cap_sol") 
        if market_cap:
            return float(market_cap) 
        return 0.0
    except requests.exceptions.RequestException:
        # Suppress API errors to keep the agent running
        return 0.0
    except Exception:
        # Suppress parsing errors
        return 0.0

# === AI MODEL FUNCTIONS ===

def load_training_data():
    """Fetches and prepares data for model training."""
    try:
        resp = supabase.table("wallets").select("tokens_traded, avg_hold_time_min, avg_pump_entry_mc, status").gte("tokens_traded", MIN_TRADES).execute()
        
        data = resp.data if resp.data else []
        if not data:
            return None, None
            
        df = pd.DataFrame(data)
        
        # 1. Feature Selection (X)
        X = df[['tokens_traded', 'avg_hold_time_min', 'avg_pump_entry_mc']]
        
        # 2. Target Variable (y): 1 for 'elite', 0 for 'demoted'
        df['is_elite'] = df['status'].apply(lambda s: 1 if s == 'elite' else 0)
        y = df['is_elite']
        
        # Scale features 
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        # Save the scaler for future predictions
        joblib.dump(scaler, 'scaler.pkl') 
        
        return X_scaled, y
        
    except Exception as e:
        print(f"AI Trainer Error (load_training_data): {e}")
        return None, None

def train_model():
    """Trains and saves the Logistic Regression model."""
    X, y = load_training_data()
    if X is None or len(X) == 0 or len(y.unique()) < 2:
        return
        
    print("AI Trainer: Starting model training...")
    
    # Split data for training/testing
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # Initialize and train the model
    model = LogisticRegression(class_weight='balanced', solver='liblinear') 
    model.fit(X_train, y_train)
    
    # Evaluate (optional)
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
        probability = model.predict_proba(features_scaled)[0][1]
        
        return probability
        
    except FileNotFoundError:
        return 0.0 
    except Exception:
        return 0.0

# === DATABASE FUNCTIONS ===

def get_all_wallets():
    """Fetches all tracked wallet addresses."""
    resp = supabase.table("wallets").select("address").execute()
    return [w["address"] for w in resp.data]

def save_buy(wallet, token_mint, sol_amount):
    ts = int(time.time())
    market_cap = get_market_cap(token_mint)

    try:
        # Use upsert to prevent duplicate entry errors
        supabase.table("trades").upsert({
            "wallet": wallet,
            "token_mint": token_mint,
            "buy_sol": sol_amount,
            "buy_ts": ts,
            "status": "open",
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
        error_details = str(e)
        if "23505" in error_details and "unique_open_trade" in error_details:
             print(f"ℹ️ SUPPRESS: Duplicate trade detected for {wallet}/{token_mint}. Already tracking.")
             return True
        else:
             print(f"DB Error (save_buy) for {wallet}/{token_mint}: {e}")
             return False


def save_sell(wallet, token_mint, sol_amount):
    ts = int(time.time())
    
    try:
        # 1. Fetch the open trade to calculate ROI
        resp = supabase.table("trades").select("buy_sol").eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").limit(1).execute()
        
        if not resp.data:
            return False 
        
        buy_sol = resp.data[0]["buy_sol"]
        roi = sol_amount / buy_sol if buy_sol else 0.0
        
        # 2. Update the trade status to closed
        supabase.table("trades").update({
            "sell_sol": sol_amount,
            "sell_ts": ts,
            "roi": roi,
            "status": "closed"
        }).eq("wallet", wallet).eq("token_mint", token_mint).eq("status", "open").execute()
        
        print(f"💰 Closing Trade: {wallet} | ROI: {roi:.2f}x | {token_mint}")
        return True
    except Exception as e:
        print(f"DB Error (save_sell) for {wallet}/{token_mint}: {e}")
        return False


def score_wallets():
    """Runs periodically to train the model, calculate features, and predict scores."""
    # Run a training cycle at the start of the scoring loop
    train_model() 
    
    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        print("🧠 Scoring wallets and generating features...")
        try:
            wallets = get_all_wallets()
            
            ready_to_predict = {} 

            for wallet in wallets:
                
                # Initialize variables to prevent "referenced before assignment" error
                wins = 0
                total_roi = 0.0
                avg_hold_time = 0.0
                avg_entry_mc = 0.0

                # Fetch only closed trades to calculate features
                closed_resp = supabase.table("trades").select("roi, buy_ts, sell_ts, entry_market_cap").eq("wallet", wallet).eq("status", "closed").execute()
                closed_trades = closed_resp.data if closed_resp.data else []

                tokens_traded = len(closed_trades)
                status = "candidate"
                
                # 1. Feature Calculation Block
                if tokens_traded >= MIN_TRADES:
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
                    
                    # Use 0.90 (90%) as the high-confidence elite threshold
                    if elite_probability >= 0.90:  
                        status = "elite"
                    else:
                        status = "demoted"
                        
                    # Final update with AI score
                    supabase.table("wallets").update({
                        "status": status,
                        "elite_probability": elite_probability 
                    }).eq("address", wallet).execute()
                    
                    print(f"🧠 AI Score for {wallet}: {elite_probability:.4f} -> {status}")

            # Log elite list
            elite_resp = supabase.table("wallets").select("address, total_roi, elite_probability").eq("status", "elite").execute()
            elite = elite_resp.data if elite_resp.data else []
            if elite:
                print("🌟 ELITE WALLETS FOUND:")
                for w in elite:
                    print(f"  - {w['address']} (ROI: {w['total_roi']:.2f}x, AI: {w['elite_probability']:.4f})")
            else:
                print("⏳ No elite wallets yet.")

        except Exception as e:
            print(f"⚠️ Critical Scoring Error: {e}")
            
# === WEBSOCKET LISTENER
