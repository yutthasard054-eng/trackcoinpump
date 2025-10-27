import asyncio
import websockets
import json
import time
import os
import logging

# Simple logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("DEBUG")

# Track what we receive
message_stats = {
    "total_messages": 0,
    "connection_attempts": 0,
    "last_message_time": None,
    "message_types": {},
    "sample_messages": []
}

async def debug_websocket():
    """Minimal WebSocket debugger to understand what's happening"""
    uri = "wss://pumpportal.fun/api/data"
    
    while True:
        try:
            message_stats["connection_attempts"] += 1
            logger.info(f"🔌 Connection attempt #{message_stats['connection_attempts']}")
            
            async with websockets.connect(uri) as ws:
                logger.info("✅ WebSocket connected successfully!")
                
                # Try different subscription methods
                subscriptions = [
                    {"method": "subscribeNewToken"},
                    {"method": "subscribeTokenTrades"},
                    {"method": "subscribeTokenTrade", "keys": []},
                    {"method": "subscribeAllTrades"}
                ]
                
                for sub in subscriptions:
                    try:
                        await ws.send(json.dumps(sub))
                        logger.info(f"📤 Sent subscription: {sub}")
                        await asyncio.sleep(0.5)  # Small delay between subscriptions
                    except Exception as e:
                        logger.error(f"❌ Failed to send {sub}: {e}")
                
                logger.info("⏳ Listening for messages...")
                
                # Listen for messages with detailed logging
                async for message in ws:
                    try:
                        message_stats["total_messages"] += 1
                        message_stats["last_message_time"] = time.time()
                        
                        # Parse the message
                        data = json.loads(message)
                        
                        # Analyze message structure
                        if isinstance(data, dict):
                            msg_type = data.get('method', 'unknown')
                            message_stats["message_types"][msg_type] = message_stats["message_types"].get(msg_type, 0) + 1
                            
                            # Log first 10 messages in detail
                            if len(message_stats["sample_messages"]) < 10:
                                message_stats["sample_messages"].append({
                                    "num": message_stats["total_messages"],
                                    "type": msg_type,
                                    "keys": list(data.keys()),
                                    "content": data
                                })
                                
                                logger.info(f"\n{'='*60}")
                                logger.info(f"📨 MESSAGE #{message_stats['total_messages']}")
                                logger.info(f"Type: {msg_type}")
                                logger.info(f"Keys: {list(data.keys())}")
                                
                                # Check for trade-related content
                                trade_indicators = ['txType', 'type', 'trader', 'user', 'wallet', 'mint', 'token', 'amount', 'sol']
                                found_trade_fields = {k: v for k, v in data.items() if any(indicator in k.lower() for indicator in trade_indicators)}
                                
                                if found_trade_fields:
                                    logger.info("🎯 TRADE-RELATED FIELDS FOUND:")
                                    for k, v in found_trade_fields.items():
                                        if isinstance(v, str) and len(v) > 20:
                                            logger.info(f"  {k}: {v[:20]}...")
                                        else:
                                            logger.info(f"  {k}: {v}")
                                else:
                                    logger.info("❌ No trade-related fields detected")
                                
                                logger.info(f"Full message: {json.dumps(data, indent=2)}")
                                logger.info(f"{'='*60}\n")
                        
                        # Log summary every 50 messages
                        if message_stats["total_messages"] % 50 == 0:
                            logger.info(f"\n📊 SUMMARY (Received {message_stats['total_messages']} messages):")
                            logger.info(f"  Message types: {message_stats['message_types']}")
                            logger.info(f"  Last message: {time.strftime('%H:%M:%S', time.localtime(message_stats['last_message_time']))}")
                            logger.info(f"  Rate: {message_stats['total_messages'] / (time.time() - start_time + 1):.1f} msg/sec\n")
                    
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON decode error: {e}")
                        logger.error(f"Raw message: {message[:200]}...")
                    except Exception as e:
                        logger.error(f"❌ Message processing error: {e}")
                        logger.error(f"Raw message: {message[:200]}...")
                
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"🔌 WebSocket closed: {e}")
            logger.info("⏳ Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"❌ Connection error: {e}")
            logger.info("⏳ Reconnecting in 10 seconds...")
            await asyncio.sleep(10)

async def monitor_activity():
    """Monitor activity and report if we're not receiving messages"""
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        
        if message_stats["last_message_time"]:
            time_since_last = time.time() - message_stats["last_message_time"]
            if time_since_last > 60:  # No messages for 1 minute
                logger.warning(f"⚠️ No messages received for {time_since_last:.0f} seconds!")
                logger.warning(f"Total messages so far: {message_stats['total_messages']}")
                logger.warning(f"Message types: {message_stats['message_types']}")
        else:
            logger.warning("⚠️ No messages received yet!")

async def main():
    logger.info("🚀 Starting PumpPortal WebSocket Debugger")
    logger.info("="*60)
    logger.info("This will show us exactly what messages we're receiving")
    logger.info("and help us understand why trades aren't being detected.")
    logger.info("="*60)
    
    global start_time
    start_time = time.time()
    
    # Start both the WebSocket listener and activity monitor
    await asyncio.gather(
        debug_websocket(),
        monitor_activity()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n🛑 Debugger stopped by user")
        
        # Final summary
        logger.info("\n" + "="*60)
        logger.info("📊 FINAL DEBUG SUMMARY")
        logger.info("="*60)
        logger.info(f"Total messages received: {message_stats['total_messages']}")
        logger.info(f"Connection attempts: {message_stats['connection_attempts']}")
        logger.info(f"Message types: {message_stats['message_types']}")
        
        if message_stats["sample_messages"]:
            logger.info("\n📋 Sample messages received:")
            for i, sample in enumerate(message_stats["sample_messages"][:5]):
                logger.info(f"\nSample #{i+1}:")
                logger.info(f"  Type: {sample['type']}")
                logger.info(f"  Keys: {sample['keys']}")
                if any('trade' in str(sample['content']).lower() or 'tx' in str(sample['content']).lower() 
                      for indicator in ['txType', 'type', 'trader', 'user', 'wallet', 'mint', 'token', 'amount', 'sol']):
                    logger.info("  ⚠️ This message contains trade-related data!")
        
        logger.info("="*60)
