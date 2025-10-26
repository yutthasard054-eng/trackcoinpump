import argparse
import asyncio
import json
import logging
import signal
from collections import defaultdict
from typing import Dict, Any

import websockets

# In-memory wallet profiler
WALLET_STATS: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"buys": [], "sells": [], "tokens": set()})
TOKEN_CREATED_AT: Dict[str, float] = {}
ELITE_WALLETS = set()

# Limits to avoid unbounded memory growth
MAX_TRADES_PER_WALLET = 500
EARLY_WINDOW_SECONDS = 30.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai_agent")

def prune_wallet(wallet: str) -> None:
    """Keep per-wallet trade lists bounded in size."""
    stats = WALLET_STATS[wallet]
    if len(stats["buys"]) > MAX_TRADES_PER_WALLET:
        stats["buys"] = stats["buys"][-MAX_TRADES_PER_WALLET:]
    if len(stats["sells"]) > MAX_TRADES_PER_WALLET:
        stats["sells"] = stats["sells"][-MAX_TRADES_PER_WALLET:]

def score_wallet(wallet: str) -> Dict[str, Any]:
    """
    Score a wallet based on matched early buys/sells.
    Returns a profile dict with is_elite flag and metrics.
    """
    stats = WALLET_STATS[wallet]
    buys = stats["buys"]
    # Map token -> best (earliest) sell seen for that token
    sells = {}
    for s in stats["sells"]:
        t = s["token"]
        # keep the earliest sell for the token (smallest ts) to pair with early buy
        if t not in sells or s["ts"] < sells[t]["ts"]:
            sells[t] = s

    # Match completed trades (buy then sell for same token)
    results = []
    for buy in buys:
        sell = sells.get(buy["token"])
        if sell and buy["sol"] and buy["sol"] > 0:
            try:
                roi = float(sell["sol"]) / float(buy["sol"])
                results.append(roi)
            except Exception:
                continue

    # Not enough data to evaluate
    if len(results) < 3:
        return {"is_elite": False}

    win_rate = len([r for r in results if r >= 2.0]) / len(results)
    avg_roi = sum(results) / len(results) if results else 0.0

    is_elite = win_rate >= 0.6 and avg_roi >= 3.0

    return {
        "is_elite": is_elite,
        "wallet": wallet,
        "win_rate": round(win_rate, 3),
        "avg_roi": round(avg_roi, 2),
        "trades": len(results),
    }


async def connect_and_listen(uri: str, early_window: float = EARLY_WINDOW_SECONDS) -> None:
    """
    Connect to the websocket endpoint and maintain a persistent subscription.
    Reconnects automatically on errors with a short delay.
    """
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                logger.info("Connected to PumpPortal at %s", uri)

                # Subscribe to ALL new tokens
                await websocket.send(json.dumps({"method": "subscribeNewToken"}))
                # Subscribe to ALL trades (empty keys = global feed)
                await websocket.send(json.dumps({"method": "subscribeTokenTrade", "keys": []}))

                async for message in websocket:
                    try:
                        data = json.loads(message)
                        dtype = data.get("type")
                        if dtype == "newToken":
                            payload = data.get("data", {})
                            mint = payload.get("mint")
                            ts_ms = payload.get("timestamp")
                            if mint and ts_ms:
                                TOKEN_CREATED_AT[mint] = ts_ms / 1000.0

                        elif dtype == "tokenTrade":
                            trade = data.get("data", {})
                            mint = trade.get("token")
                            wallet = trade.get("account")
                            sol = trade.get("solAmount")
                            ts_ms = trade.get("timestamp")
                            ttype = trade.get("type")

                            if not (mint and wallet and ts_ms):
                                continue

                            ts = ts_ms / 1000.0
                            created = TOKEN_CREATED_AT.get(mint, ts)
                            # Only track early trades (within early_window seconds of token creation)
                            if ts - created <= early_window:
                                if ttype == "buy":
                                    WALLET_STATS[wallet]["buys"].append({"token": mint, "sol": sol, "ts": ts})
                                    WALLET_STATS[wallet]["tokens"].add(mint)
                                elif ttype == "sell":
                                    WALLET_STATS[wallet]["sells"].append({"token": mint, "sol": sol, "ts": ts})

                                prune_wallet(wallet)

                                profile = score_wallet(wallet)
                                if profile.get("is_elite") and wallet not in ELITE_WALLETS:
                                    ELITE_WALLETS.add(wallet)
                                    logger.info(
                                        "ELITE WALLET PROMOTED: %s | Win Rate: %s, Avg ROI: %sx, Trades: %s",
                                        wallet,
                                        profile.get("win_rate"),
                                        profile.get("avg_roi"),
                                        profile.get("trades"),
                                    )
                    except Exception as e:
                        logger.warning("Error processing message: %s", e)

        except Exception as e:
            logger.error("Connection error: %s", e)
            await asyncio.sleep(5)  # Wait before reconnecting


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Install signal handlers for graceful shutdown (SIGINT/SIGTERM)."""
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
    except NotImplementedError:
        # add_signal_handler is not available on Windows event loop policy in some cases
        pass


async def _shutdown() -> None:
    logger.info("Shutdown requested. Exiting...")
    # Let running tasks finish briefly
    await asyncio.sleep(0.1)
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()


def parse_args():
    parser = argparse.ArgumentParser(description="PumpPortal elite wallet profiler")
    parser.add_argument(
        "--uri", "-u", default="wss://pumpportal.fun/api/data", help="WebSocket URI for PumpPortal feed"
    )
    parser.add_argument(
        "--early-window",
        "-w",
        type=float,
        default=EARLY_WINDOW_SECONDS,
        help="Seconds after token creation considered 'early' (default: 30)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    loop = asyncio.get_event_loop()
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(connect_and_listen(args.uri, early_window=args.early_window))
    except asyncio.CancelledError:
        logger.info("Main task cancelled, exiting.")
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
