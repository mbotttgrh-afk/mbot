#!/usr/bin/env python3
"""
TwoOrders Binance Futures v7.20 (Hedge Mode + 75x Leverage)
Corrected: rate-limit guard, ban wait, notional safety, stable client IDs
"""

import time
import math
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# ================== CONFIG ==================
API_KEY = "IpOidDHnWB0lOeRlcsPP7okXMAb5reepNRwh61CXAzfPiVC5zISTyKBzaNtaGCWR"
API_SECRET = "327HIyvbAJBytXCVQbMv4ZJi5SuZcO4i4FBG7BhB7TEBAF7L86lWkD62gja4miWa"
USE_TESTNET = False  # set False for live

SYMBOL = "DOGEUSDT"
LEVERAGE = 75

# Entry
SPACING_PRICE = 0.00001
BASE_LOT = 70
SL_DISTANCE = 0.0005

# Multi-order
BASE_ORDERS_PER_SIDE = 1
MAX_ORDERS_PER_SIDE = 2

# Recenter
RECENTER_ENABLED = True
RECENTER_DISTANCE = 0.005

# Trailing SL
TRAIL_START = 0.001
TRAIL_DISTANCE = 0.002
LOCKED_PROFIT = 0.0005

# Martingale
LOSS_MULTIPLIER = 3.0
MAX_MULTIPLIER = 27.0
MAX_LOT = 500
RESET_MARTINGALE = False

MAGIC = 123456789
POLL_INTERVAL = 5  # Increased from 2s to 5s to reduce request rate

# ================== GLOBALS ==================
client = None
g_mult = 1.0
g_anchor = 0.0
g_symbol_info = None
g_step_size = None
g_min_qty = None
g_tick_size = None
g_digits = 8
g_last_trade_id = 0
g_hedge_mode = False
g_last_request_time = 0
g_min_request_interval = 0.6  # Minimum interval between two REST requests (seconds)

# ================== RATE LIMIT GUARD ==================
def safe_call(func, *args, **kwargs):
    """Wrap any Binance REST call with rate-limit guard and ban wait."""
    global g_last_request_time
    elapsed = time.time() - g_last_request_time
    if elapsed < g_min_request_interval:
        time.sleep(g_min_request_interval - elapsed)

    try:
        result = func(*args, **kwargs)
        g_last_request_time = time.time()
        return result
    except BinanceAPIException as e:
        if e.code == -1003 or getattr(e, "status_code", None) == 418:
            # IP banned — wait long enough for ban to lift
            print(f"[RATE LIMIT] IP banned. Waiting 180s before retry...")
            time.sleep(180)
            return None
        if getattr(e, "status_code", None) == 429:
            print("[RATE LIMIT] 429 received. Waiting 60s...")
            time.sleep(60)
            return None
        raise

# ================== HELPERS ==================
def init_client():
    global client, g_symbol_info, g_step_size, g_min_qty, g_tick_size, g_digits, g_hedge_mode
    client = Client(
        API_KEY, API_SECRET,
        testnet=USE_TESTNET,
        requests_params={"timeout": 30},
    )
    client.futures_ping()

    # Cancel all normal orders
    print("[INIT] Cleaning existing orders on", SYMBOL, "...")
    try:
        safe_call(client.futures_cancel_all_open_orders, symbol=SYMBOL)
        print("[INIT] All open orders cancelled.")
    except BinanceAPIException as e:
        if e.code == -2011 or "Unknown order" in e.message:
            print("[INIT] No open orders to cancel.")
        else:
            print(f"[INIT] Cancel-all warning: {e.message}")

    # Cancel algo orders
    try:
        algo_orders = safe_call(client.futures_get_open_algo_orders, symbol=SYMBOL)
        if algo_orders:
            for o in algo_orders:
                try:
                    safe_call(client.futures_cancel_algo_order, symbol=SYMBOL, algoId=o["algoId"])
                except Exception:
                    pass
            print(f"[INIT] Cancelled {len(algo_orders)} algo order(s).")
    except Exception as e:
        print(f"[INIT] Algo cancel warning: {e}")

    # Enable Hedge Mode
    try:
        safe_call(client.futures_change_position_mode, dualSidePosition=True)
        print("[INIT] Hedge Mode ENABLED")
        g_hedge_mode = True
    except BinanceAPIException as e:
        if "No need to change position side" in e.message:
            print("[INIT] Hedge Mode already active")
            g_hedge_mode = True
        else:
            print(f"[INIT] FATAL: Cannot enable Hedge Mode: {e.message}")
            raise SystemExit(1)

    # Set leverage
    try:
        safe_call(client.futures_change_leverage, symbol=SYMBOL, leverage=LEVERAGE)
        print(f"[INIT] Leverage set to {LEVERAGE}x")
    except BinanceAPIException as e:
        print(f"[INIT] Leverage error: {e.message}")

    # Symbol filters
    info = safe_call(client.futures_exchange_info)
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            g_symbol_info = s
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    g_step_size = float(f["stepSize"])
                    g_min_qty = float(f["minQty"])
                if f["filterType"] == "PRICE_FILTER":
                    g_tick_size = float(f["tickSize"])
            g_digits = int(s["pricePrecision"])
            break

    if g_symbol_info is None:
        raise RuntimeError(f"Symbol {SYMBOL} not found")

    print(f"[INIT] {SYMBOL} step={g_step_size} minQty={g_min_qty} tick={g_tick_size}")

def normalize_qty(qty):
    if g_step_size is None:
        return qty
    q = Decimal(str(qty)).quantize(Decimal(str(g_step_size)), rounding=ROUND_DOWN)
    result = float(q)
    if g_min_qty is not None and result < g_min_qty:
        result = g_min_qty
    return result

def normalize_price(price):
    if g_tick_size is None:
        return price
    p = round(price / g_tick_size) * g_tick_size
    return round(p, g_digits)

def fetch_price():
    t = safe_call(client.futures_symbol_ticker, symbol=SYMBOL)
    return float(t["price"]) if t else None

def fetch_positions():
    poss = safe_call(client.futures_position_information, symbol=SYMBOL)
    if not poss:
        return []
    return [p for p in poss if abs(float(p["positionAmt"])) > 0]

def fetch_open_orders():
    return safe_call(client.futures_get_open_orders, symbol=SYMBOL) or []

def fetch_open_algo_orders():
    try:
        return safe_call(client.futures_get_open_algo_orders, symbol=SYMBOL) or []
    except Exception:
        return []

def count_my_positions():
    return len(fetch_positions())

def count_my_orders():
    return len(fetch_open_orders()) + len(fetch_open_algo_orders())

def count_long_positions():
    return sum(1 for p in fetch_positions()
               if p.get("positionSide") == "LONG" and float(p["positionAmt"]) > 0)

def count_short_positions():
    return sum(1 for p in fetch_positions()
               if p.get("positionSide") == "SHORT" and float(p["positionAmt"]) < 0)

def count_long_orders():
    n = 0
    for o in fetch_open_orders():
        if o.get("positionSide") == "LONG" and o["side"] == "BUY":
            n += 1
    for o in fetch_open_algo_orders():
        if o.get("positionSide") == "LONG" and o.get("side") == "BUY":
            n += 1
    return n

def count_short_orders():
    n = 0
    for o in fetch_open_orders():
        if o.get("positionSide") == "SHORT" and o["side"] == "SELL":
            n += 1
    for o in fetch_open_algo_orders():
        if o.get("positionSide") == "SHORT" and o.get("side") == "SELL":
            n += 1
    return n

def cancel_all_my_orders():
    n = 0
    for o in fetch_open_orders():
        try:
            safe_call(client.futures_cancel_order, symbol=SYMBOL, orderId=o["orderId"])
            n += 1
        except Exception:
            pass
    for o in fetch_open_algo_orders():
        try:
            safe_call(client.futures_cancel_algo_order, symbol=SYMBOL, algoId=o["algoId"])
            n += 1
        except Exception:
            pass
    if n:
        print(f"  Cancelled {n} order(s)")
    return n

def cancel_side_orders(cancel_long: bool):
    n = 0
    target_side = "LONG" if cancel_long else "SHORT"
    for o in fetch_open_orders():
        if o.get("positionSide") == target_side:
            try:
                safe_call(client.futures_cancel_order, symbol=SYMBOL, orderId=o["orderId"])
                n += 1
            except Exception:
                pass
    for o in fetch_open_algo_orders():
        if o.get("positionSide") == target_side:
            try:
                safe_call(client.futures_cancel_algo_order, symbol=SYMBOL, algoId=o["algoId"])
                n += 1
            except Exception:
                pass
    if n:
        print(f"  Cancelled {n} {target_side} order(s)")

# ================== MARTINGALE ==================
def loss_streak():
    if g_mult <= 1.0:
        return 0
    return int(round(math.log(g_mult) / math.log(LOSS_MULTIPLIER)))

def orders_per_side():
    n = BASE_ORDERS_PER_SIDE
    streak = loss_streak()
    for _ in range(streak):
        n *= 2
        if n >= MAX_ORDERS_PER_SIDE:
            return MAX_ORDERS_PER_SIDE
    return max(1, min(n, MAX_ORDERS_PER_SIDE))

# ================== TRADING ==================
def place_entry_orders(anchor, lot, n_per_side):
    placed = 0
    for level in range(1, n_per_side + 1):
        buy_price = normalize_price(anchor + SPACING_PRICE * level)
        sell_price = normalize_price(anchor - SPACING_PRICE * level)

        buy_cid = f"ENT_L_{MAGIC}_{level}_{int(anchor * 1e8)}"
        try:
            safe_call(client.futures_create_algo_order,
                algoType="CONDITIONAL",
                symbol=SYMBOL,
                side=SIDE_BUY,
                positionSide="LONG",
                type="STOP_MARKET",
                quantity=lot,
                triggerPrice=buy_price,
                workingType="CONTRACT_PRICE",
                clientAlgoId=buy_cid,
            )
            placed += 1
            print(f"  BuyStop L{level} (LONG) @ {buy_price} ok")
        except BinanceAPIException as e:
            if "Duplicate" in str(e.message):
                placed += 1
                print(f"  BuyStop L{level} already exists")
            else:
                print(f"  BuyStop L{level} failed: {e.message}")

        sell_cid = f"ENT_S_{MAGIC}_{level}_{int(anchor * 1e8)}"
        try:
            safe_call(client.futures_create_algo_order,
                algoType="CONDITIONAL",
                symbol=SYMBOL,
                side=SIDE_SELL,
                positionSide="SHORT",
                type="STOP_MARKET",
                quantity=lot,
                triggerPrice=sell_price,
                workingType="CONTRACT_PRICE",
                clientAlgoId=sell_cid,
            )
            placed += 1
            print(f"  SellStop L{level} (SHORT) @ {sell_price} ok")
        except BinanceAPIException as e:
            if "Duplicate" in str(e.message):
                placed += 1
                print(f"  SellStop L{level} already exists")
            else:
                print(f"  SellStop L{level} failed: {e.message}")

    return placed

def place_sl_for_position(pos):
    amt = float(pos["positionAmt"])
    entry = float(pos["entryPrice"])
    pos_side = pos.get("positionSide", "BOTH")

    if pos_side == "LONG":
        sl_price = normalize_price(entry - SL_DISTANCE)
        side = SIDE_SELL
    else:
        sl_price = normalize_price(entry + SL_DISTANCE)
        side = SIDE_BUY

    cid = f"SL_{MAGIC}_{int(entry * 1e8)}"
    try:
        safe_call(client.futures_create_algo_order,
            algoType="CONDITIONAL",
            symbol=SYMBOL,
            side=side,
            positionSide=pos_side,
            type="STOP_MARKET",
            triggerPrice=sl_price,
            closePosition="true",
            workingType="CONTRACT_PRICE",
            clientAlgoId=cid,
        )
        print(f"  SL placed: {pos_side} {side} trigger={sl_price} ok")
        return True
    except BinanceAPIException as e:
        if "Duplicate" in str(e.message):
            print(f"  SL already exists for {pos_side}")
            return True
        print(f"  SL placement failed: {e.message}")
        return False

def update_trailing_sl():
    positions = fetch_positions()
    if not positions:
        return

    price = fetch_price()
    if price is None:
        return
    sl_orders = fetch_open_algo_orders()

    for pos in positions:
        amt = float(pos["positionAmt"])
        entry = float(pos["entryPrice"])
        pos_side = pos.get("positionSide", "BOTH")

        if pos_side == "LONG" and amt > 0:
            profit = price - entry
            if profit < TRAIL_START:
                continue
            target_sl = max(price - TRAIL_DISTANCE, entry + LOCKED_PROFIT)
            current_sl = None
            for o in sl_orders:
                if o.get("positionSide") == "LONG" and o.get("side") == SIDE_SELL:
                    current_sl = float(o.get("triggerPrice", 0))
                    break
            if current_sl and target_sl <= current_sl:
                continue
            for o in sl_orders:
                if o.get("positionSide") == "LONG" and o.get("side") == SIDE_SELL:
                    try:
                        safe_call(client.futures_cancel_algo_order, symbol=SYMBOL, algoId=o["algoId"])
                    except Exception:
                        pass
            place_sl_for_position(pos)

        elif pos_side == "SHORT" and amt < 0:
            profit = entry - price
            if profit < TRAIL_START:
                continue
            target_sl = min(price + TRAIL_DISTANCE, entry - LOCKED_PROFIT)
            current_sl = None
            for o in sl_orders:
                if o.get("positionSide") == "SHORT" and o.get("side") == SIDE_BUY:
                    current_sl = float(o.get("triggerPrice", 0))
                    break
            if current_sl and target_sl >= current_sl:
                continue
            for o in sl_orders:
                if o.get("positionSide") == "SHORT" and o.get("side") == SIDE_BUY:
                    try:
                        safe_call(client.futures_cancel_algo_order, symbol=SYMBOL, algoId=o["algoId"])
                    except Exception:
                        pass
            place_sl_for_position(pos)

def check_fills_and_update_martingale():
    global g_mult, g_last_trade_id
    try:
        trades = safe_call(client.futures_account_trades, symbol=SYMBOL, limit=50)
        if not trades:
            return
    except Exception:
        return
    trades.sort(key=lambda t: int(t["id"]))
    for t in trades:
        tid = int(t["id"])
        if tid <= g_last_trade_id:
            continue
        g_last_trade_id = tid
        oid = str(t.get("orderId", ""))
        cid = t.get("clientOrderId", "") or ""
        if not (oid.startswith(f"SL_{MAGIC}") or cid.startswith(f"SL_{MAGIC}")):
            continue
        pnl = float(t.get("realizedPnl", 0)) - float(t.get("commission", 0))
        if pnl > 0:
            g_mult = 1.0
            print(f"  WIN +{pnl:.4f} -> mult reset to 1.0")
        else:
            g_mult = min(g_mult * LOSS_MULTIPLIER, MAX_MULTIPLIER)
            print(f"  LOSS {pnl:.4f} -> mult {g_mult:.2f}")

def display_dashboard():
    price = fetch_price()
    positions = fetch_positions()
    orders = fetch_open_orders() + fetch_open_algo_orders()

    print("\n" + "=" * 55)
    print(f"  TwoOrders Hedge | {SYMBOL} | {LEVERAGE}x")
    print(f"  Price: {price if price else 'N/A'}")
    print(f"  Positions: {len(positions)}")
    print(f"  Orders: {len(orders)}")
    print(f"  Anchor: {g_anchor if g_anchor > 0 else 'None'}")
    if g_anchor > 0 and price:
        print(f"  Drift: {abs(price - g_anchor):.6f} / {RECENTER_DISTANCE}")
    print(f"  Multiplier: x{g_mult:.2f}  |  Streak: {loss_streak()}")
    print(f"  Next lot: {normalize_qty(min(BASE_LOT * g_mult, MAX_LOT))}")
    print(f"  Orders/side: {orders_per_side()}")

    if positions:
        print("  --- Positions ---")
        for p in positions:
            amt = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            side = p.get("positionSide", "?")
            pnl = float(p["unRealizedProfit"])
            print(f"    {side} qty={abs(amt)} entry={entry} uPnL={pnl:.4f}")

    if orders:
        print("  --- Orders ---")
        for o in orders:
            stp = o.get("triggerPrice", o.get("stopPrice", "N/A"))
            print(f"    {o.get('positionSide')} {o.get('side')} "
                  f"{o.get('type', o.get('orderType'))} trigger={stp}")

    print("=" * 55)

# ================== MAIN LOOP ==================
def main():
    global g_anchor, g_mult

    init_client()
    print(f"=== TwoOrders Hedge Mode | {SYMBOL} | {LEVERAGE}x ===")

    if RESET_MARTINGALE:
        g_mult = 1.0

    while True:
        try:
            price = fetch_price()
            if price is None:
                time.sleep(30)
                continue

            pos_count = count_my_positions()
            ord_count = count_my_orders()

            if RECENTER_ENABLED and pos_count == 0 and ord_count > 0 and g_anchor > 0:
                drift = abs(price - g_anchor)
                if drift >= RECENTER_DISTANCE:
                    print(f"\n[RECENTER] drift={drift:.6f} >= {RECENTER_DISTANCE}")
                    cancel_all_my_orders()
                    g_anchor = 0
                    ord_count = 0

            long_pos = count_long_positions()
            short_pos = count_short_positions()
            long_ord = count_long_orders()
            short_ord = count_short_orders()

            if long_pos > 0 and short_ord > 0:
                print("LONG filled -> cancel SHORT pendings")
                cancel_side_orders(cancel_long=False)
            if short_pos > 0 and long_ord > 0:
                print("SHORT filled -> cancel LONG pendings")
                cancel_side_orders(cancel_long=True)

            for p in fetch_positions():
                amt = float(p["positionAmt"])
                if abs(amt) < (g_min_qty or 0) * 0.5:
                    continue
                pos_side = p.get("positionSide")
                sl_exists = False
                for o in fetch_open_algo_orders():
                    if o.get("positionSide") == pos_side and o.get("type") == "STOP_MARKET":
                        if str(o.get("closePosition")).lower() == "true":
                            sl_exists = True
                            break
                if not sl_exists:
                    print(f"  Missing SL for {pos_side}, placing...")
                    place_sl_for_position(p)

            if pos_count == 0 and ord_count == 0:
                lot = normalize_qty(min(BASE_LOT * g_mult, MAX_LOT))
                n = orders_per_side()
                anchor = price

                acct = safe_call(client.futures_account)
                if acct is None:
                    time.sleep(30)
                    continue
                free_margin = float(acct["availableBalance"])
                total_notional = lot * price * n * 2
                required_margin = total_notional / LEVERAGE

                # Notional safety: each order must be >= 5 USDT
                if total_notional < 5:
                    print(f"  SKIP: notional {total_notional:.2f} < 5 USDT minimum")
                    time.sleep(POLL_INTERVAL)
                    continue

                if required_margin > free_margin * 0.8:
                    print(f"  SKIP: margin tight (need={required_margin:.4f} "
                          f"free={free_margin:.4f} notional={total_notional:.2f})")
                    time.sleep(POLL_INTERVAL)
                    continue

                print(f"\n[NEW CYCLE] mult=x{g_mult:.2f} lot={lot} orders/side={n}")
                print(f"  anchor={anchor}  notional={total_notional:.2f} "
                      f"reqMargin={required_margin:.4f} free={free_margin:.4f}")
                placed = place_entry_orders(anchor, lot, n)
                if placed > 0:
                    g_anchor = anchor
                    print(f"  Placed {placed} orders")

            update_trailing_sl()
            check_fills_and_update_martingale()
            display_dashboard()

        except Exception as e:
            print(f"\n[ERROR] {e}")
            time.sleep(15)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
