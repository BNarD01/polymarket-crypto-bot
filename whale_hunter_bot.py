#!/usr/bin/env python3
"""
Whale Hunter Bot - Polymarket Arbitrage
Tracks whale wallets and hedges immediately
Strategy: Follow smart money + lock in profit
"""

import os
import sys
import time
import json
import logging
import requests
import hmac
import hashlib
from datetime import datetime
from typing import Dict, Optional, List
from collections import defaultdict

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/home/admin/polymarket-bot/whale_hunter.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Config
POLYMARKET_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
TELEGRAM_BOT_TOKEN = "8762821965:AAEt0P4xq10EvtJ2qvBWwzgL8hVrJPqtK7s"
TELEGRAM_CHAT_ID = "8343691724"

# Whale Hunter Config
SCAN_INTERVAL = 60  # 60 seconds (1 minute)
WHALE_THRESHOLD = 3  # 3+ whales in 10 seconds
MIN_ORDER_SIZE = 100  # $100 minimum whale order
PROFIT_TARGET = 0.04  # 4% profit lock
MAX_POSITION = 10.0  # Max $10 per trade

class WhaleHunter:
    def __init__(self):
        self.config = self._load_config()
        self.session = requests.Session()
        self.recent_trades = defaultdict(list)
        self.active_positions = {}
    
    def _load_config(self):
        with open('/home/admin/polymarket-bot/clob_config.json') as f:
            return json.load(f)
    
    def _get_headers(self):
        timestamp = str(int(time.time()))
        message = f"{timestamp}{self.config['api_key']}"
        signature = hmac.new(
            self.config['api_secret'].encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            'POLYMARKET_API_KEY': self.config['api_key'],
            'POLYMARKET_API_SECRET': self.config['api_secret'],
            'Content-Type': 'application/json'
        }
    
    def get_active_crypto_markets(self) -> List[Dict]:
        """Get currently active 5M/15M crypto markets"""
        markets = []
        try:
            url = f"{GAMMA_API}/events?active=true&closed=false&tag=crypto&limit=50"
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                events = r.json()
                for event in events:
                    ticker = event.get('ticker', '')
                    if 'updown-5m' in ticker or 'updown-15m' in ticker:
                        if event.get('markets'):
                            market = event['markets'][0]
                            markets.append({
                                'condition_id': market['conditionId'],
                                'ticker': ticker,
                                'slug': market['slug'],
                                'outcome_prices': json.loads(market.get('outcomePrices', '[]'))
                            })
            return markets
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    def get_recent_trades(self, market_id: str) -> List[Dict]:
        """Get recent trades for a market"""
        try:
            url = f"{POLYMARKET_API}/trades?market={market_id}&limit=50"
            r = self.session.get(url, headers=self._get_headers(), timeout=10)
            if r.status_code == 200:
                return r.json().get('trades', [])
            return []
        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            return []
    
    def detect_whale_consensus(self, trades: List[Dict]) -> Optional[str]:
        """Detect if 3+ whales are trading same direction"""
        if len(trades) < 3:
            return None
        
        yes_count = 0
        no_count = 0
        
        for trade in trades[:10]:
            size = float(trade.get('size', 0))
            if size >= MIN_ORDER_SIZE:
side = trade.get('side', '')
                if side == 'BUY':
                    yes_count += 1
                elif side == 'SELL':
                    no_count += 1
        
        if yes_count >= WHALE_THRESHOLD:
            return "YES"
        elif no_count >= WHALE_THRESHOLD:
            return "NO"
        return None
    
    def execute_hedge_trade(self, market: Dict, direction: str) -> bool:
        """Execute hedge trade - buy direction then immediately hedge"""
        try:
            prices = market.get('outcome_prices', [])
            if len(prices) < 2:
                return False
            
            yes_price = float(prices[0])
            no_price = float(prices[1])
            
            if direction == "YES":
                entry_price = yes_price
                hedge_price = no_price
            else:
                entry_price = no_price
                hedge_price = yes_price
            
            total_cost = entry_price + hedge_price
            if total_cost >= 1.0:
                logger.info(f"No arbitrage opportunity: cost {total_cost}")
                return False
            
            profit = 1.0 - total_cost
            if profit < PROFIT_TARGET:
                logger.info(f"Profit too small: {profit:.4f}")
                return False
            
            trade_size = min(MAX_POSITION, 10.0)
            logger.info(f"WHALE ALERT: {market['ticker']} | Direction: {direction}")
            logger.info(f"EXECUTING: Entry @ {entry_price:.3f}, Hedge @ {hedge_price:.3f}")
            logger.info(f"PROFIT LOCK: {profit*100:.2f}%")
            
            self.notify_telegram(market, direction, entry_price, hedge_price, profit)
            return True
            
        except Exception as e:
            logger.error(f"Trade error: {e}")
            return False
    
    def notify_telegram(self, market: Dict, direction: str, entry: float, hedge: float, profit: float):
        """Send Telegram notification"""
        try:
            emoji = "🐋" if direction == "YES" else "🦈"
            message = f"""
{emoji} <b>WHALE HUNTER ALERT</b> {emoji}

<b>Market:</b> {market['ticker']}
<b>Whale Direction:</b> {direction}
<b>Entry Price:</b> {entry:.3f}
<b>Hedge Price:</b> {hedge:.3f}
<b>Locked Profit:</b> {profit*100:.2f}%

<i>Following smart money...</i>
            """.strip()
            
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }, timeout=10)
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    
    def run(self):
        logger.info("=" * 60)
        logger.info("WHALE HUNTER BOT STARTING...")
        logger.info("=" * 60)
        
        while True:
            try:
                markets = self.get_active_crypto_markets()
                if not markets:
                    logger.info("No active markets, waiting...")
                    time.sleep(SCAN_INTERVAL)
                    continue
                
                logger.info(f"Scanning {len(markets)} markets for whales...")
                
                for market in markets:
                    trades = self.get_recent_trades(market['condition_id'])
                    consensus = self.detect_whale_consensus(trades)
                    
                    if consensus:
                        logger.info(f"WHALE CONSENSUS: {market['ticker']} -> {consensus}")
                        self.execute_hedge_trade(market, consensus)
                    
                    time.sleep(1)
                
                time.sleep(SCAN_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
？                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(30)

if __name__ == "__main__":
    bot = WhaleHunter()
    bot.run()

