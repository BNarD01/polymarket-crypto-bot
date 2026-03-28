#!/usr/bin/env python3
"""
BTC 5m/15m Arbitrage Bot - Polymarket
Strategy:
  1. Same-market YES+NO arb: buy YES + NO when their sum < 1.0 - fee, locking risk-free profit
  2. Cross-timeframe spread: trade when 5m and 15m prices diverge beyond a threshold
Budget: 50 USDC (configurable)
"""

import os
import sys
import time
import json
import hmac
import base64
import hashlib
import logging
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ─── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

POLYMARKET_CLOB  = "https://clob.polymarket.com"
GAMMA_API        = "https://gamma-api.polymarket.com"

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("btc_arb.log"),
    ],
)
log = logging.getLogger("btc_arb")


# ─── Position tracking ─────────────────────────────────────────────────────────
@dataclass
class Position:
    token_id:    str
    market_slug: str
    side:        str    # 'YES' or 'NO'
    entry_price: float
    quantity:    float
    label:       str    # human-readable e.g. '5m-YES'
    opened_at:   float = field(default_factory=time.time)


# ─── Helpers ───────────────────────────────────────────────────────────────────
def load_config() -> Dict:
    if not os.path.exists(CONFIG_FILE):
        log.error(f"Config file not found: {CONFIG_FILE}")
        log.error("Copy config.template.json -> config.json and fill in your credentials.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    required = ["api_key", "api_secret", "api_passphrase",
                "telegram_token", "telegram_chat_id"]
    for key in required:
        if not cfg.get(key):
            log.error(f"Missing required config key: {key}")
            sys.exit(1)
    if not cfg.get("dry_run", True) and not cfg.get("private_key"):
        log.error("private_key is required in config.json for live trading (dry_run=false).")
        log.error("Get it from MetaMask: Account details -> Show private key")
        sys.exit(1)
    return cfg


def clob_headers(cfg: Dict, method: str, path: str, body: str = "") -> Dict:
    """
    Polymarket CLOB L2 auth header.
    Secret is base64-encoded; decode it first.
    Signature = base64(HMAC-SHA256(timestamp + method + path + body, decoded_secret))
    """
    ts = str(int(time.time()))
    msg = ts + method.upper() + path + body
    secret = base64.b64decode(cfg["api_secret"])
    sig = base64.b64encode(
        hmac.new(secret, msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY-API-KEY":        cfg["api_key"],
        "POLY-SIGNATURE":      sig,
        "POLY-TIMESTAMP":      ts,
        "POLY-PASSPHRASE":     cfg["api_passphrase"],
        "Content-Type":        "application/json",
    }


# ─── Market discovery ──────────────────────────────────────────────────────────
class MarketFinder:
    def __init__(self, session: requests.Session):
        self.session = session

    def get_btc_markets(self) -> Dict[str, Optional[Dict]]:
        """
        Return {'5m': market_dict, '15m': market_dict} for active BTC updown markets.
        Each dict has keys: condition_id, token_yes, token_no, yes_price, no_price, ticker
        """
        result: Dict[str, Optional[Dict]] = {"5m": None, "15m": None}

        now_ts = int(time.time())
        # Round to current 5-min and 15-min interval boundaries
        # Polymarket slug pattern: btc-updown-5m-{timestamp}  btc-updown-15m-{timestamp}
        ts_5m  = (now_ts // 300) * 300   # floor to 5-min boundary
        ts_15m = (now_ts // 900) * 900   # floor to 15-min boundary

        # Try current and adjacent intervals (in case of clock skew)
        slugs_to_try = {
            "5m":  [f"btc-updown-5m-{ts_5m + i*300}"  for i in range(-1, 3)],
            "15m": [f"btc-updown-15m-{ts_15m + i*900}" for i in range(-1, 3)],
        }

        for label, slug_list in slugs_to_try.items():
            for slug in slug_list:
                try:
                    url = f"{GAMMA_API}/events?slug={slug}"
                    r = self.session.get(url, timeout=10)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    events = data if isinstance(data, list) else [data]
                    for event in events:
                        if not event or not event.get("markets"):
                            continue
                        m = event["markets"][0]
                        try:
                            prices = m.get("outcomePrices", "[0.5,0.5]")
                            if isinstance(prices, str):
                                prices = json.loads(prices)
                            yes_price = float(prices[0]) if prices else 0.5
                            no_price  = float(prices[1]) if len(prices) > 1 else 0.5
                            tokens = m.get("clobTokenIds", "[]")
                            if isinstance(tokens, str):
                                tokens = json.loads(tokens)
                        except Exception:
                            continue
                        # Skip resolved/bad markets
                        # Valid binary market: YES+NO ≈ 1.0 and neither at extreme
                        price_sum = yes_price + no_price
                        if yes_price <= 0.05 or yes_price >= 0.95:
                            log.info(f"Skipping near-resolved market: {slug} YES={yes_price:.4f}")
                            continue
                        if abs(price_sum - 1.0) > 0.15:
                            log.info(f"Skipping bad-data market: {slug} sum={price_sum:.4f}")
                            continue
                        result[label] = {
                            "condition_id": m.get("conditionId", ""),
                            "token_yes":    tokens[0] if len(tokens) > 0 else "",
                            "token_no":     tokens[1] if len(tokens) > 1 else "",
                            "yes_price":    yes_price,
                            "no_price":     no_price,
                            "ticker":       slug,
                            "slug":         slug,
                        }
                        log.info(f"Found {label} market: {slug}  YES={yes_price:.4f}  NO={no_price:.4f}")
                        break
                except Exception as e:
                    log.warning(f"Slug lookup error {slug}: {e}")
                if result[label]:
                    break

        return result



# ─── Order book ────────────────────────────────────────────────────────────────
class OrderBook:
    def __init__(self, session: requests.Session, cfg: Dict):
        self.session = session
        self.cfg = cfg

    def best_ask(self, token_id: str) -> Optional[float]:
        """Return the best ask (cheapest offer) for a token."""
        if not token_id:
            return None
        try:
            r = self.session.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}", timeout=10)
            if r.status_code == 404:
                return None  # No order book yet (thin market)
            r.raise_for_status()
            asks = r.json().get("asks", [])
            if not asks:
                return None
            return float(asks[0]["price"])
        except Exception:
            return None

    def clob_prices(self, condition_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get YES/NO mid prices directly from CLOB market endpoint."""
        if not condition_id:
            return None, None
        try:
            r = self.session.get(f"{POLYMARKET_CLOB}/markets/{condition_id}", timeout=10)
            if r.status_code != 200:
                return None, None
            m = r.json()
            tokens = m.get("tokens", [])
            yes_p = no_p = None
            for t in tokens:
                outcome = t.get("outcome", "").upper()
                price   = t.get("price")
                if price is not None:
                    if outcome == "YES":
                        yes_p = float(price)
                    elif outcome == "NO":
                        no_p = float(price)
            return yes_p, no_p
        except Exception:
            return None, None

    def get_live_prices(self, market: Dict) -> Tuple[Optional[float], Optional[float]]:
        """Return None, None — short-duration markets use Gamma prices (more reliable than CLOB)."""
        return None, None


# ─── Trade executor ────────────────────────────────────────────────────────────
class TradeExecutor:
    def __init__(self, session: requests.Session, cfg: Dict):
        self.session = session
        self.cfg     = cfg
        self.dry_run = cfg.get("dry_run", True)
        self._clob   = None

        if not self.dry_run:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                pk = cfg["private_key"].strip()
                if not pk.startswith("0x"):
                    pk = "0x" + pk
                self._clob = ClobClient(
                    host=POLYMARKET_CLOB,
                    key=pk,
                    chain_id=137,   # Polygon mainnet
                    creds=ApiCreds(
                        api_key=cfg["api_key"],
                        api_secret=cfg["api_secret"],
                        api_passphrase=cfg["api_passphrase"],
                    ),
                    signature_type=0,   # 0=EOA(MetaMask), 1=Polymarket proxy
                )
                log.info("CLOB client initialised (LIVE mode)")
            except ImportError:
                log.error("py-clob-client not installed. Run: pip install py-clob-client")
                sys.exit(1)
            except Exception as e:
                log.error(f"CLOB client init error: {e}")
                sys.exit(1)

    def place_order(self, token_id: str, side: str, size: float, price: float) -> Optional[Dict]:
        """
        Place a FOK order on the CLOB.
        side: 'BUY' | 'SELL'
        """
        if not token_id:
            log.warning("place_order called with empty token_id, skipping.")
            return None

        info = f"token={token_id[:12]}... size={size:.2f} price={price:.4f}"

        if self.dry_run:
            log.info(f"[DRY RUN] Would place {side} order: {info}")
            return {"status": "dry_run"}

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size, 2),
                side=side,   # 'BUY' or 'SELL' string directly
            )
            signed_order = self._clob.create_order(order_args)
            resp = self._clob.post_order(signed_order, OrderType.FOK)
            log.info(f"Order placed: {side} {info} | resp={resp}")
            return resp if resp else None
        except Exception as e:
            log.error(f"Order placement error: {e}")
            return None


# ─── Telegram notifier ─────────────────────────────────────────────────────────
class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, msg: str):
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={
                "chat_id":    self.chat_id,
                "text":       msg,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            log.warning(f"Telegram error: {e}")


# ─── Arbitrage engine ──────────────────────────────────────────────────────────
class ArbEngine:
    """
    Two strategies:
    A. Same-market YES+NO: buy YES and NO simultaneously when total cost < 1.0 - FEE
    B. Cross-timeframe spread: if |P(YES_5m) - P(YES_15m)| > SPREAD_THRESHOLD,
       buy the cheaper side and sell (buy NO of) the more expensive side.
    """

    # Polymarket taker fee is ~2% per side → round-trip ~4%
    # Strategy A min profit after fees
    FEE_RATE         = 0.02           # per-side taker fee estimate
    MIN_PROFIT_A     = 0.01           # 1% net after fees
    MIN_SPREAD_B     = 0.08           # 8% price gap for cross-market trade
    MAX_TRADE_USDC   = 10.0           # max USDC per leg to preserve budget
    SPREAD_COOLDOWN  = 300            # seconds between cross-spread trades (5 min)
    TAKE_PROFIT      = 0.20           # close position when price up 20%
    STOP_LOSS        = 0.10           # close position when price down 10%

    def __init__(self, ob: OrderBook, executor: TradeExecutor, tg: Telegram,
                 budget: float, cfg: Dict, session: requests.Session):
        self.ob       = ob
        self.executor = executor
        self.tg       = tg
        self.session  = session
        self.budget   = budget        # total available USDC
        self.spent    = 0.0           # USDC spent so far
        self.recovered = 0.0          # USDC recovered from closed positions
        self.pnl      = 0.0           # realized P&L
        self.trades   = 0
        self.dry_run  = cfg.get("dry_run", True)
        self._last_spread_time = 0.0  # cooldown tracker for cross-spread trades
        self.positions: List[Position] = []

    @property
    def available(self) -> float:
        return max(0.0, self.budget - self.spent + self.recovered)

    def _get_current_price(self, pos: Position) -> Optional[float]:
        """Fetch latest price for a position's token from Gamma API."""
        try:
            url = f"{GAMMA_API}/events?slug={pos.market_slug}"
            r = self.session.get(url, timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            events = data if isinstance(data, list) else [data]
            for event in events:
                if not event or not event.get("markets"):
                    continue
                m = event["markets"][0]
                prices = m.get("outcomePrices", "[0.5,0.5]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                return float(prices[0]) if pos.side == "YES" else float(prices[1])
        except Exception:
            return None

    def check_positions(self):
        """Check all open positions for take-profit or stop-loss conditions."""
        if not self.positions:
            return
        closed = []
        for pos in self.positions:
            current = self._get_current_price(pos)
            if current is None:
                continue
            change = (current - pos.entry_price) / pos.entry_price
            if change >= self.TAKE_PROFIT:
                reason = f"TAKE PROFIT +{change*100:.1f}%"
            elif change <= -self.STOP_LOSS:
                reason = f"STOP LOSS {change*100:.1f}%"
            else:
                log.info(f"[Position {pos.label}] entry={pos.entry_price:.4f} "
                         f"current={current:.4f} pnl={change*100:+.1f}%")
                continue
            # Exit the position
            res = self.executor.place_order(pos.token_id, "SELL", pos.quantity, current)
            if res:
                revenue     = current * pos.quantity
                cost        = pos.entry_price * pos.quantity
                realized    = revenue - cost
                self.pnl      += realized
                self.recovered += revenue
                closed.append(pos)
                msg = (
                    f"<b>Position closed [{pos.label}]</b> - {reason}\n"
                    f"Entry: {pos.entry_price:.4f} -&gt; Exit: {current:.4f}\n"
                    f"Qty: {pos.quantity:.2f} | Realized P&amp;L: <b>${realized:+.4f}</b>\n"
                    f"Budget available: ${self.available:.2f}"
                )
                log.info(msg.replace("<b>", "").replace("</b>", "")
                         .replace("&amp;", "&").replace("-&gt;", "->"))
                self.tg.send(msg)
        for pos in closed:
            self.positions.remove(pos)

    # ── Strategy A: same-market risk-free arb ──────────────────────────────────
    def check_same_market_arb(self, market: Dict, label: str) -> bool:
        yes_ask, no_ask = self.ob.get_live_prices(market)
        if yes_ask is None or no_ask is None:
            # Fall back to last known prices from Gamma
            yes_ask = market["yes_price"]
            no_ask  = market["no_price"]

        total_cost = yes_ask + no_ask
        # After paying 2% fee on each side: effective cost = total_cost * (1 + 2*FEE_RATE)
        effective_cost = total_cost * (1 + 2 * self.FEE_RATE)
        profit_pct     = 1.0 - effective_cost

        log.info(f"[{label}] YES={yes_ask:.4f} NO={no_ask:.4f} "
                 f"sum={total_cost:.4f} profit_after_fees={profit_pct*100:.2f}%")

        if profit_pct < self.MIN_PROFIT_A:
            return False
        if self.available < 5.0:
            log.warning("Insufficient budget for trade.")
            return False

        # Size: spend evenly on YES and NO up to MAX_TRADE_USDC per leg
        leg_size = min(self.MAX_TRADE_USDC, self.available / 2)
        yes_qty  = round(leg_size / yes_ask, 2)
        no_qty   = round(leg_size / no_ask, 2)

        log.info(f"ARBIT OPPORTUNITY [{label}] profit={profit_pct*100:.2f}%  "
                 f"YES qty={yes_qty} @ {yes_ask}  NO qty={no_qty} @ {no_ask}")

        res_yes = self.executor.place_order(market["token_yes"], "BUY", yes_qty, yes_ask)
        res_no  = self.executor.place_order(market["token_no"],  "BUY", no_qty,  no_ask)

        if res_yes and res_no:
            cost = leg_size * 2
            locked_profit = (yes_qty + no_qty) * 1.0 - cost  # each share pays $1
            self.spent += cost
            self.pnl   += locked_profit
            self.trades += 1
            # Strategy A is risk-free arb — no TP/SL needed, holds to settlement
            msg = (
                f"<b>Arb [{label}] executed!</b>\n"
                f"YES {yes_qty} @ {yes_ask:.4f}\n"
                f"NO  {no_qty} @ {no_ask:.4f}\n"
                f"Locked profit: <b>${locked_profit:.4f}</b> ({profit_pct*100:.2f}%)\n"
                f"Budget used: ${self.spent:.2f} / ${self.budget:.2f}"
            )
            log.info(msg.replace("<b>", "").replace("</b>", ""))
            self.tg.send(msg)
            return True
        return False

    # ── Strategy B: cross-timeframe spread ────────────────────────────────────
    def check_cross_spread(self, m5: Dict, m15: Dict) -> bool:
        """
        If 15m YES price > 5m YES price by >= MIN_SPREAD_B:
          Buy 5m YES (cheaper) + Buy 15m NO (opposing side of expensive)
        Logic: 15m UP should roughly equal P(at least one UP in 3 5m windows).
        If 15m is priced too high relative to 5m, we sell 15m UP (buy NO) and
        buy 5m YES expecting mean reversion.
        """
        # Use Gamma prices as primary source (most accurate for short-duration markets)
        # Only override with live prices if they are valid and consistent with Gamma
        yes5, no5   = m5["yes_price"],  m5["no_price"]
        yes15, no15 = m15["yes_price"], m15["no_price"]

        live_yes5, live_no5 = self.ob.get_live_prices(m5)
        if live_yes5 and live_no5 and abs(live_yes5 + live_no5 - 1.0) <= 0.05:
            if abs(live_yes5 - yes5) <= 0.20:  # within 20% of Gamma price
                yes5, no5 = live_yes5, live_no5

        live_yes15, live_no15 = self.ob.get_live_prices(m15)
        if live_yes15 and live_no15 and abs(live_yes15 + live_no15 - 1.0) <= 0.05:
            if abs(live_yes15 - yes15) <= 0.20:
                yes15, no15 = live_yes15, live_no15

        spread = yes15 - yes5
        log.info(f"[Cross-spread] 5m YES={yes5:.4f}  15m YES={yes15:.4f}  "
                 f"spread={spread*100:.2f}%")

        if abs(spread) < self.MIN_SPREAD_B:
            return False
        if self.available < 5.0:
            return False

        # Cooldown: only one spread trade per SPREAD_COOLDOWN window
        elapsed = time.time() - self._last_spread_time
        if elapsed < self.SPREAD_COOLDOWN:
            log.info(f"[Cross-spread] Cooldown active ({self.SPREAD_COOLDOWN - elapsed:.0f}s left), skipping.")
            return False

        if spread > 0:
            # 15m overpriced vs 5m: buy 5m YES + buy 15m NO
            buy_token     = m5["token_yes"]
            buy_price     = yes5
            hedge_token   = m15["token_no"]
            hedge_price   = no15
            direction_lbl = "5m-YES / 15m-NO"
        else:
            # 5m overpriced vs 15m: buy 15m YES + buy 5m NO
            buy_token     = m15["token_yes"]
            buy_price     = yes15
            hedge_token   = m5["token_no"]
            hedge_price   = no5
            direction_lbl = "15m-YES / 5m-NO"

        if buy_price <= 0 or hedge_price <= 0:
            log.warning("Zero price detected, skipping spread trade.")
            return False
        leg_size  = min(self.MAX_TRADE_USDC, self.available / 2)
        buy_qty   = round(leg_size / buy_price,   2)
        hedge_qty = round(leg_size / hedge_price, 2)

        log.info(f"SPREAD TRADE [{direction_lbl}]  spread={abs(spread)*100:.2f}%  "
                 f"buy {buy_qty} @ {buy_price:.4f}  hedge {hedge_qty} @ {hedge_price:.4f}")

        res_buy   = self.executor.place_order(buy_token,   "BUY", buy_qty,   buy_price)
        res_hedge = self.executor.place_order(hedge_token, "BUY", hedge_qty, hedge_price)

        if res_buy and res_hedge:
            cost = leg_size * 2
            self.spent += cost
            self.trades += 1
            self._last_spread_time = time.time()
            # Track both legs for TP/SL monitoring
            buy_slug   = m5["slug"]   if spread <= 0 else m5["slug"]
            hedge_slug = m15["slug"]  if spread > 0  else m5["slug"]
            if spread > 0:
                buy_slug   = m5["slug"]
                hedge_slug = m15["slug"]
                buy_side   = "YES"
                hedge_side = "NO"
            else:
                buy_slug   = m15["slug"]
                hedge_slug = m5["slug"]
                buy_side   = "YES"
                hedge_side = "NO"
            self.positions.append(Position(
                token_id=buy_token, market_slug=buy_slug,
                side=buy_side, entry_price=buy_price,
                quantity=buy_qty, label=f"{direction_lbl} leg1",
            ))
            self.positions.append(Position(
                token_id=hedge_token, market_slug=hedge_slug,
                side=hedge_side, entry_price=hedge_price,
                quantity=hedge_qty, label=f"{direction_lbl} leg2",
            ))
            msg = (
                f"<b>Spread trade [{direction_lbl}]</b>\n"
                f"Spread: {abs(spread)*100:.2f}%\n"
                f"Leg 1: {buy_qty} @ {buy_price:.4f}\n"
                f"Leg 2: {hedge_qty} @ {hedge_price:.4f}\n"
                f"Cost: ${cost:.2f} | Budget left: ${self.available:.2f}\n"
                f"TP: +20% | SL: -10% (auto-exit)"
            )
            log.info(msg.replace("<b>", "").replace("</b>", ""))
            self.tg.send(msg)
            return True
        return False


# ─── Main bot ─────────────────────────────────────────────────────────────────
class BtcArbBot:
    SCAN_INTERVAL  = 30   # seconds between scans
    MARKET_REFRESH = 300  # refresh market info every 5 minutes

    def __init__(self):
        self.cfg      = load_config()
        self.session  = requests.Session()
        self.session.headers.update({"User-Agent": "btc-arb-bot/1.0"})

        budget = float(self.cfg.get("budget_usdc", 50.0))
        dry    = self.cfg.get("dry_run", True)

        self.finder   = MarketFinder(self.session)
        self.ob       = OrderBook(self.session, self.cfg)
        self.executor = TradeExecutor(self.session, self.cfg)
        self.tg       = Telegram(
            self.cfg["telegram_token"],
            self.cfg["telegram_chat_id"],
        )
        self.engine   = ArbEngine(self.ob, self.executor, self.tg, budget, self.cfg, self.session)

        mode = "DRY RUN" if dry else "LIVE"
        log.info(f"BTC Arb Bot initialised | mode={mode} budget=${budget:.2f}")

    def run(self):
        log.info("=" * 60)
        log.info(" BTC 5m/15m ARBITRAGE BOT  —  Polymarket")
        log.info("=" * 60)
        self.tg.send("<b>BTC Arb Bot started</b>\nWatching 5m and 15m markets...")

        markets: Dict[str, Optional[Dict]] = {"5m": None, "15m": None}
        last_market_refresh = 0

        while True:
            try:
                now = time.time()

                # ── Refresh market info periodically ──────────────────────
                if now - last_market_refresh >= self.MARKET_REFRESH:
                    markets = self.finder.get_btc_markets()
                    last_market_refresh = now

                    if not markets["5m"] and not markets["15m"]:
                        log.warning("No BTC 5m/15m markets found. Retrying in 60s.")
                        time.sleep(60)
                        continue

                m5  = markets.get("5m")
                m15 = markets.get("15m")

                # ── Check budget ───────────────────────────────────────────
                if self.engine.available < 5.0:
                    log.info(f"Budget exhausted. Spent=${self.engine.spent:.2f} "
                             f"P&L=${self.engine.pnl:.4f}  Trades={self.engine.trades}")
                    self.tg.send(
                        f"Budget exhausted.\nSpent: ${self.engine.spent:.2f}\n"
                        f"P&L: ${self.engine.pnl:.4f}\nTrades: {self.engine.trades}"
                    )
                    break

                # ── Check open positions for TP/SL ────────────────────────
                self.engine.check_positions()

                # ── Strategy A: same-market arb ───────────────────────────
                if m5:
                    self.engine.check_same_market_arb(m5, "5m")
                if m15:
                    self.engine.check_same_market_arb(m15, "15m")

                # ── Strategy B: cross-timeframe spread ────────────────────
                if m5 and m15:
                    self.engine.check_cross_spread(m5, m15)

                log.info(
                    f"Budget: ${self.engine.available:.2f} left  "
                    f"Spent=${self.engine.spent:.2f}  "
                    f"Recovered=${self.engine.recovered:.2f}  "
                    f"P&L=${self.engine.pnl:.4f}  "
                    f"Trades={self.engine.trades}  "
                    f"Positions={len(self.engine.positions)}"
                )

                time.sleep(self.SCAN_INTERVAL)

            except KeyboardInterrupt:
                log.info("Interrupted by user.")
                break
            except Exception as e:
                log.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(30)

        log.info("=" * 60)
        log.info(f"FINAL  Spent=${self.engine.spent:.2f}  "
                 f"P&L=${self.engine.pnl:.4f}  Trades={self.engine.trades}")
        log.info("=" * 60)
        self.tg.send(
            f"<b>Bot stopped</b>\n"
            f"Spent: ${self.engine.spent:.2f}\n"
            f"P&amp;L: ${self.engine.pnl:.4f}\n"
            f"Trades: {self.engine.trades}"
        )


if __name__ == "__main__":
    BtcArbBot().run()
