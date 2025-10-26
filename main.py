import asyncio
import websockets
import json
import requests
import time
import threading
import sqlite3
import os

# === PERSISTENT DATABASE SETUP ===
DB_PATH = "/persistent/wallets.db"

def init_db():
    os.makedirs("/persistent", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            address TEXT PRIMARY KEY,
            first_seen REAL,
            last_updated REAL,
            tokens_traded INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            total_roi REAL DEFAULT 0.0,
            status TEXT DEFAULT 'candidate',
            last_buy_ts REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT,
            token_mint TEXT,
            buy_sol REAL,
            sell_sol REAL,
            buy_ts REAL,
            sell_ts REAL,
            roi REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()

def save_buy(wallet, token_mint, sol_amount):
    conn = sqlite3.connect(DB_PATH)
    ts = time.time()
    conn.execute("""
        INSERT INTO trades (wallet, token_mint, buy_sol, buy_ts, status)
        VALUES (?, ?, ?, ?, 'open')
    """, (wallet, token_mint, sol_amount, ts))
    conn.execute("""
        INSERT OR IGNORE INTO wallets (address, first_seen, last_updated, last_buy_ts)
        VALUES (?, ?, ?, ?)
    """, (wallet, ts, ts, ts))
    conn.commit()
    conn.close()

def get_all_wallets():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT address FROM wallets").fetchall()
    conn.close()
    return [r[0] for r in rows]

def update_wallet_status(wallet, tokens_traded, wins, total_roi, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE wallets
        SET tokens_traded = ?, wins = ?, total_roi = ?, status = ?, last_updated = ?
        WHERE address = ?
    """, (tokens_traded, wins, total_roi, status, time.time(), wallet))
    conn.commit()
    conn.close()

def get_open_trades(wallet):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT token_mint, buy_sol, buy_ts FROM trades
        WHERE wallet = ? AND status = 'open'
    """, (wallet,)).fetchall()
    conn.close()
    return rows

def close_trade(wallet, token_mint, sell_sol):
    conn = sqlite3.connect(DB_PATH)
    buy = conn.execute("""
        SELECT buy_sol FROM trades WHERE wallet = ? AND token_mint = ? AND status = 'open'
    """, (wallet, token_mint)).fetchone()
    if buy:
        roi = sell_sol / buy[0] if buy[0] > 0 else 0
        conn.execute("""
            UPDATE trades
            SET sell_sol = ?, sell_ts = ?, roi = ?, status = 'closed'
            WHERE wallet = ? AND token_mint = ?
        """, (sell_sol, time.time(), roi, wallet, token_mint))
    conn.commit()
    conn.close()

def get_trades(mint):
    try:
        resp = requests.get(f"https://frontend-api.pump.fun/trades/{mint}?limit=100", timeout=5)
        return resp.json() if resp.status_code == 200 else []
    except:
        return []

def score_wallets():
    MIN_TRADES = 3
    MIN_WIN_RATE = 0.6
    MIN_ROI = 2.0
    while True:
        time.sleep(900)  # Every 15 minutes
        print("🧠 Scoring wallets...")
        try:
            wallets = get_all_wallets()
            for wallet in wallets:
                open_trades = get_open_trades(wallet)
                for mint, buy_sol, buy_ts in open_trades:
                    trades = get_trades(mint)
                    sells = [t for t in trades if t["type"] == "sell" and t["user"] == wallet]
                    if sells:
                        close_trade(wallet, mint, sells[-1]["sol_amount"])
                
                conn = sqlite3.connect(DB_PATH)
                closed = conn.execute("""
                    SELECT roi FROM trades WHERE wallet = ? AND status = 'closed'
                """, (wallet,)).fetchall()
                conn.close()
                
                if len(closed) < MIN_TRADES:
                    status = "candidate"
                else:
                    wins = len([r for r in closed if r[0] >= MIN_ROI])
                    win_rate = wins / len(closed)
                    avg_roi = sum(r[0] for r in closed) / len(closed) if closed else 0
                    status = "elite" if win_rate >= MIN_WIN_RATE and avg_roi >= MIN_ROI else "demoted"
                
                update_wallet_status(wallet, len(closed), wins, avg_roi, status)
                if status == "elite":
                    print(f"✅ ELITE: {wallet}")
                elif status == "demoted":
                    print(f"❌ DEMOTED: {wallet}")
            
            # Log elite list
            conn = sqlite3.connect(DB_PATH)
            elite = conn.execute("SELECT address FROM wallets WHERE status = 'elite'").fetchall()
            conn.close()
            if elite:
                print("🌟 ELITE WALLETS:")
                for (w,) in elite:
                    print(f"  - {w}")
        except Exception as e:
            print(f"⚠️ Scoring error: {e}")

# === MAIN AI AGENT ===
async def main():
    init_db()
    threading.Thread(target=score_wallets, daemon=True).start()
    
    uri = "wss://pumpportal.fun/api/data"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✅ Connected to PumpPortal")
                
                # Subscribe to ALL new tokens
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                # Subscribe to ALL trades (empty keys = global feed)
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))
                
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if data.get("txType") == "create":
                            wallet = data["traderPublicKey"]
                            sol = data["solAmount"]
                            mint = data["mint"]
                            if sol >= 0.1:  # Only track ≥0.1 SOL
                                save_buy(wallet, mint, sol)
                                print(f"🛒 Tracking: {wallet} | {sol} SOL | {mint}")
                    except Exception as e:
                        print(f"⚠️ Message error: {e}")
        except Exception as e:
            print(f"💥 Connection error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
