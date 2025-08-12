
import os, time, hmac, hashlib, requests, re
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ========= ENV =========
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
API_KEY = os.getenv("MEXC_API_KEY", "")
SECRET  = os.getenv("MEXC_SECRET_KEY", "")
PAPER   = os.getenv("PAPER_MODE", "true").lower() == "true"
MAX_USDT = float(os.getenv("MAX_ORDER_USDT", "300"))
BASE = "https://api.mexc.com"

bot = Bot(token=TG_TOKEN)
dp  = Dispatcher(bot)

def ts(): return int(time.time()*1000)
def sign(q: str): return hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

def mexc(method, path, params=None, signed=False):
    headers = {"X-MEXC-APIKEY": API_KEY}
    params = params or {}
    if signed:
        params["timestamp"] = ts()
        params["recvWindow"] = 50000
        q = "&".join([f"{k}={params[k]}" for k in sorted(params)])
        params["signature"] = sign(q)
    r = requests.get(BASE+path, params=params, headers=headers, timeout=20) if method=="GET"             else requests.post(BASE+path, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def get_price(symbol):
    d = mexc("GET", "/api/v3/ticker/price", {"symbol": symbol})
    return float(d["price"])

def place_spot_order(symbol, side, qty=None, quote_usdt=None, order_type="MARKET", limit_price=None):
    if quote_usdt is not None and qty is None:
        px = get_price(symbol)
        qty = max(round(float(quote_usdt)/px, 6), 0.000001)
    p = {"symbol": symbol, "side": side, "type": order_type, "quantity": qty}
    if order_type == "LIMIT":
        p["price"] = f"{float(limit_price):.8f}"
        p["timeInForce"] = "GTC"
    return {"paper": True, "order": p} if PAPER else mexc("POST", "/api/v3/order", p, signed=True)

def place_tp_limit(symbol, qty, tp_px):
    p = {"symbol": symbol,"side":"SELL","type":"LIMIT","timeInForce":"GTC","quantity":qty,"price":f"{tp_px:.8f}"}
    return {"paper": True, "tp": p} if PAPER else mexc("POST","/api/v3/order",p, signed=True)

def place_sl_stoplimit(symbol, qty, stop_px, lim_px):
    p = {"symbol":symbol,"side":"SELL","type":"STOP_LOSS_LIMIT","timeInForce":"GTC",
         "quantity":qty,"stopPrice":f"{stop_px:.8f}","price":f"{lim_px:.8f}"}
    return {"paper": True, "sl": p} if PAPER else mexc("POST","/api/v3/order",p, signed=True)

def pair(sym): return sym.upper()+"USDT"

SIG_RE = re.compile(
 r"^SIG\s+(BUY|SELL)\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)USDT\s+@(?:(MKT)|LIM=(\d+(?:\.\d+)?))\s+TP=(\d+(?:\.\d+)?)\s+SL=(\d+(?:\.\d+)?)\s*(?:\nR:\s*(.+))?$",
 re.IGNORECASE
)

@dp.message_handler(commands=["start"])
async def start(m: types.Message):
    if m.from_user.id != ALLOWED_ID:
        return await m.answer("⛔️ Нет доступа.")
    await m.answer(
        f"Готов к работе. PAPER_MODE={PAPER}\n"
        "Формат:\nSIG BUY SOL 20USDT @MKT TP=212 SL=188\nR: причина"
    )

@dp.message_handler(commands=["balance"])
async def balance(m: types.Message):
    if m.from_user.id != ALLOWED_ID: return
    try:
        data = mexc("GET","/api/v3/account", signed=True)
        bals = {b["asset"]: float(b["free"]) for b in data.get("balances",[]) if float(b["free"])>0}
        txt = "\n".join([f"{k}: {v:.4f}" for k,v in sorted(bals.items(), key=lambda x:-x[1])[:12]]) or "Пусто."
        await m.answer("Баланс:\n"+txt)
    except Exception as e:
        await m.answer(f"Ошибка баланса: {e}")

@dp.message_handler(lambda msg: msg.from_user.id == ALLOWED_ID)
async def handle_sig(m: types.Message):
    t = m.text.strip()
    mt = SIG_RE.match(t)
    if not mt: return
    side, sym, usdt, mkt, lim, tp, sl, reason = mt.groups()
    usdt = float(usdt); tp=float(tp); sl=float(sl)
    order_type = "MARKET" if mkt else "LIMIT"
    lim = float(lim) if lim else None
    symbol = pair(sym)
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"ok|{side}|{symbol}|{usdt}|{order_type}|{lim or 0}|{tp}|{sl}|{reason or ''}"),
        InlineKeyboardButton("✖️ Отменить", callback_data="cancel")
    )
    await m.answer(
        f"Сделка:\n• {side} {symbol}\n• {usdt} USDT\n• {order_type}{' @ '+str(lim) if lim else ''}\n"
        f"• TP: {tp}  • SL: {sl}\nПричина: {reason or '—'}\nПодтвердить?",
        reply_markup=kb
    )

@dp.callback_query_handler(lambda c: c.from_user.id == ALLOWED_ID and c.data.startswith('ok|'))
async def approve(c: types.CallbackQuery):
    _, side, symbol, usdt, order_type, lim, tp, sl, reason = c.data.split("|", 8)
    usdt=float(usdt); lim=float(lim); tp=float(tp); sl=float(sl)
    try:
        res = place_spot_order(symbol, side, quote_usdt=usdt,
                               order_type=order_type, limit_price=lim if order_type=='LIMIT' else None)
        await c.message.edit_reply_markup()
        msg = f"✅ Ордер отправлен{' (PAPER)' if PAPER else ''}\n{side} {symbol} на {usdt} USDT\n"
        if side.upper()=='BUY':
            if 'order' in res and 'quantity' in res['order']:
                qty = float(res['order']['quantity'])
            else:
                px = get_price(symbol)
                qty = round(usdt/px, 6)
            tp_res = place_tp_limit(symbol, qty, tp)
            try:
                sl_res = place_sl_stoplimit(symbol, qty, sl, sl*0.997)
            except Exception as e:
                sl_res = f"SL не создан: {e}"
            msg += f"TP создан: {tp_res}\nSL создан: {sl_res}\n"
        await c.message.answer(msg + f"Причина: {reason or '—'}")
    except Exception as e:
        await c.message.answer(f"⚠️ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data=='cancel')
async def cancel(c: types.CallbackQuery):
    await c.message.edit_reply_markup()
    await c.message.answer("Отменил.")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
