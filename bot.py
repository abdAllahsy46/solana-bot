import os, asyncio, logging, httpx, base58, re
from datetime import datetime, time as dtime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.transaction import VersionedTransaction
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import anthropic

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
WALLET_PRIVKEY = os.getenv("WALLET_PRIVATE_KEY", "")
YOUR_CHAT_ID   = int(os.getenv("YOUR_CHAT_ID", "0"))

SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_URL   = "https://api.mainnet-beta.solana.com"

state = {
    "running": False,
    "pump_enabled": True,
    "total_trades": 0,
    "daily_profit_usd": 0.0,
    "win": 0, "lose": 0,
    "start_balance_sol": 0.0,
    "current_balance_sol": 0.0,
    "max_trade_usd": float(os.getenv("MAX_TRADE_USD", "2")),
    "stop_loss_pct": float(os.getenv("STOP_LOSS_PCT", "10")),
    "daily_loss_limit_usd": float(os.getenv("DAILY_LOSS_LIMIT", "5")),
    "daily_loss_today": 0.0,
    "owner_chat_id": YOUR_CHAT_ID,
    "last_sol_price": 0.0,
    "chat_history": [],
    "active_pump_tokens": [],
    "pump_min_liquidity": 5000,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def load_keypair():
    pk = WALLET_PRIVKEY.strip()
    if not pk: return None
    try: return Keypair.from_bytes(base58.b58decode(pk))
    except Exception as e: log.error(f"خطأ المحفظة: {e}"); return None

keypair = load_keypair()

async def get_balance_sol():
    if not keypair: return 0.0
    try:
        async with AsyncClient(RPC_URL) as c:
            r = await c.get_balance(keypair.pubkey(), commitment=Confirmed)
            return r.value / 1_000_000_000
    except: return 0.0

async def get_sol_price():
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids": "solana", "vs_currencies": "usd"})
            p = r.json()["solana"]["usd"]
            state["last_sol_price"] = p
            return p
    except: return state["last_sol_price"] or 150.0

async def get_pump_new_tokens():
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get("https://frontend-api.pump.fun/coins",
                           params={"limit": 20, "sort": "created_timestamp", "order": "DESC", "includeNsfw": False})
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        log.error(f"خطأ Pump.fun: {e}")
    return []

async def filter_pump_tokens(tokens):
    good = []
    for t in tokens:
        try:
            usd_mc = t.get("usd_market_cap", 0)
            created = t.get("created_timestamp", 0)
            age_minutes = (datetime.now().timestamp() * 1000 - created) / 60000
            if usd_mc < state["pump_min_liquidity"]: continue
            if usd_mc > 500000: continue
            if age_minutes > 60: continue
            if age_minutes < 2: continue
            if t.get("complete", False): continue
            good.append({"mint": t.get("mint"), "name": t.get("name", "Unknown"),
                         "symbol": t.get("symbol", "???"), "market_cap": usd_mc,
                         "age_min": round(age_minutes, 1)})
        except: continue
    return good

async def buy_pump_token(mint, sol_amount):
    if not keypair: return None
    try:
        lamports = int(sol_amount * 1_000_000_000)
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get("https://quote-api.jup.ag/v6/quote",
                           params={"inputMint": SOL_MINT, "outputMint": mint,
                                   "amount": lamports, "slippageBps": 1000})
            if r.status_code != 200: return None
            swap = await h.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse": r.json(),
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 5000,
            })
            if swap.status_code != 200: return None
            tx = VersionedTransaction.from_bytes(base58.b58decode(swap.json()["swapTransaction"]))
            tx.sign([keypair])
            async with AsyncClient(RPC_URL) as client:
                res = await client.send_raw_transaction(bytes(tx),
                    opts={"skip_preflight": False, "preflight_commitment": "confirmed"})
                return str(res.value)
    except Exception as e:
        log.error(f"خطأ شراء Pump: {e}")
        return None

async def get_quote(inp, out, amt):
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get("https://quote-api.jup.ag/v6/quote",
                           params={"inputMint": inp, "outputMint": out, "amount": amt, "slippageBps": 50})
            if r.status_code == 200: return r.json()
    except: pass
    return None

async def do_swap(quote):
    if not keypair: return None
    try:
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.post("https://quote-api.jup.ag/v6/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": 1000,
            })
            if r.status_code != 200: return None
            tx = VersionedTransaction.from_bytes(base58.b58decode(r.json()["swapTransaction"]))
            tx.sign([keypair])
            async with AsyncClient(RPC_URL) as c:
                res = await c.send_raw_transaction(bytes(tx),
                    opts={"skip_preflight": False, "preflight_commitment": "confirmed"})
                return str(res.value)
    except Exception as e:
        log.error(f"swap error: {e}")
        return None

def safety_check(usd):
    if usd > state["max_trade_usd"]: return False, f"تجاوز الحد ${state['max_trade_usd']}"
    if state["daily_loss_today"] >= state["daily_loss_limit_usd"]: return False, "تجاوز حد الخسارة اليومية"
    sol_price = state["last_sol_price"] or 150
    if state["current_balance_sol"] < (usd / sol_price) + 0.01: return False, "رصيد غير كافٍ"
    return True, "ok"

async def chat_with_claude(user_message):
    if not CLAUDE_API_KEY:
        return "أضف CLAUDE_API_KEY في Railway"
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        pump_status = "مفعّل" if state["pump_enabled"] else "معطّل"
        system = f"""أنت مساعد بوت تداول سولانا ذكي واسمك سولانا بوت. تتحدث بالعربية بشكل طبيعي وودود كأنك صديق خبير في التداول.

حالتك الآن:
- التشغيل: {'يعمل' if state['running'] else 'متوقف'}
- الرصيد: {state['current_balance_sol']:.4f} SOL (حوالي ${state['current_balance_sol'] * (state['last_sol_price'] or 150):.2f})
- سعر SOL: ${state['last_sol_price']:.2f}
- Pump.fun: {pump_status} | توكنات نشطة: {len(state['active_pump_tokens'])}
- صفقات اليوم: {state['total_trades']} | نجاح: {state['win']} | خسارة: {state['lose']}
- أرباح اليوم: ${state['daily_profit_usd']:+.2f}
- حد الصفقة: ${state['max_trade_usd']}

تعليماتك:
- أجب بشكل طبيعي وودود مثل محادثة حقيقية
- إذا سألك عن التداول أو السوق قدم تحليلاً مفيداً
- إذا سألك عن البوت أخبره بحالته الحقيقية
- لا تكن رسمياً جداً كن ودوداً ومفيداً
- يمكنك الإجابة على أي سؤال عام أيضاً"""

        state["chat_history"].append({"role": "user", "content": user_message})
        if len(state["chat_history"]) > 20:
            state["chat_history"] = state["chat_history"][-20:]

        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=system,
            messages=state["chat_history"]
        )
        reply = resp.content[0].text
        state["chat_history"].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"خطأ: {str(e)[:80]}"

def kb():
    lbl = "⏸ إيقاف" if state["running"] else "▶️ تشغيل"
    pump_lbl = "🚀 Pump: ON" if state["pump_enabled"] else "🚀 Pump: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl, callback_data="toggle"),
         InlineKeyboardButton("📊 الحالة", callback_data="status")],
        [InlineKeyboardButton("📈 التقرير", callback_data="report"),
         InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [InlineKeyboardButton("🧠 تحليل السوق", callback_data="analyze"),
         InlineKeyboardButton("⚙️ إعدادات", callback_data="settings")],
        [InlineKeyboardButton(pump_lbl, callback_data="toggle_pump"),
         InlineKeyboardButton("🎯 توكنات Pump", callback_data="pump_tokens")],
    ])

def wr():
    t = state["win"] + state["lose"]
    return round(state["win"] / t * 100, 1) if t else 0

async def cmd_start(update, ctx):
    cid = update.effective_chat.id
    state["owner_chat_id"] = cid
    sol = await get_balance_sol()
    price = await get_sol_price()
    state["current_balance_sol"] = sol
    state["start_balance_sol"] = sol
    w = str(keypair.pubkey())[:8] + "..." if keypair else "غير محمّلة"
    await update.message.reply_text(f"""🤖 *بوت سولانا المتقدم*
━━━━━━━━━━━━━━━━━━━━
🔑 المحفظة: `{w}`
💰 الرصيد: `{sol:.4f} SOL` (≈ `${sol*price:.2f}`)
📈 سعر SOL: `${price:.2f}`
━━━━━━━━━━━━━━━━━━━━
🚀 Pump.fun: `مفعّل ✅`
🛡 حد الصفقة: `${state['max_trade_usd']}`
🛑 خسارة يومية: `${state['daily_loss_limit_usd']}`
━━━━━━━━━━━━━━━━━━━━
📌 معرفك: `{cid}`

💬 يمكنك الكلام معي بشكل طبيعي!
اضغط تشغيل للبدء 👇""", parse_mode="Markdown", reply_markup=kb())

async def cmd_status(update, ctx):
    sol = await get_balance_sol()
    price = await get_sol_price()
    state["current_balance_sol"] = sol
    pnl = (sol - state["start_balance_sol"]) * price
    msg = update.message or update.callback_query.message
    await msg.reply_text(f"""📊 *الحالة الكاملة*
━━━━━━━━━━━━━━━━━━━━
🤖 {'🟢 يعمل' if state['running'] else '🔴 متوقف'}
🚀 Pump.fun: {'✅ مفعّل' if state['pump_enabled'] else '❌ معطّل'}
💰 `{sol:.4f} SOL` (≈ `${sol*price:.2f}`)
📈 SOL: `${price:.2f}`
━━━━━━━━━━━━━━━━━━━━
🔄 الصفقات: `{state['total_trades']}`
✅ `{state['win']}` ❌ `{state['lose']}` 🎯 `{wr()}%`
💵 اليوم: `${state['daily_profit_usd']:+.2f}`
📊 الكلي: `${pnl:+.2f}`
🎪 توكنات Pump: `{len(state['active_pump_tokens'])}`""",
        parse_mode="Markdown", reply_markup=kb())

async def handle_buttons(update, ctx):
    q = update.callback_query
    await q.answer()

    if q.data == "toggle":
        state["running"] = not state["running"]
        if state["running"]:
            asyncio.create_task(trading_loop(ctx.application))
            if state["pump_enabled"]:
                asyncio.create_task(pump_loop(ctx.application))
        await q.edit_message_text(
            "✅ البوت يعمل! يبحث عن فرص كل 5 دقائق 🚀" if state["running"] else "⛔ البوت متوقف.",
            reply_markup=kb())

    elif q.data == "toggle_pump":
        state["pump_enabled"] = not state["pump_enabled"]
        status = "مفعّل ✅" if state["pump_enabled"] else "معطّل ❌"
        await q.edit_message_text(f"🚀 Pump.fun {status}", reply_markup=kb())

    elif q.data == "pump_tokens":
        if not state["active_pump_tokens"]:
            await q.edit_message_text("🎯 لا توجد توكنات Pump.fun نشطة.", reply_markup=kb())
        else:
            txt = "🎯 *توكنات Pump.fun النشطة:*\n━━━━━━━━━━\n"
            for t in state["active_pump_tokens"]:
                txt += f"🪙 `{t['symbol']}` | SOL: `{t['sol_spent']:.4f}`\n"
            await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb())

    elif q.data == "status":
        await cmd_status(update, ctx)

    elif q.data == "balance":
        sol = await get_balance_sol()
        price = await get_sol_price()
        state["current_balance_sol"] = sol
        await q.edit_message_text(f"💰 *الرصيد*\n`{sol:.6f} SOL`\n≈ `${sol*price:.2f}`",
            parse_mode="Markdown", reply_markup=kb())

    elif q.data == "report":
        await q.edit_message_text(f"""📈 *تقرير اليوم*
━━━━━━━━━━━━━━━
🔄 `{state['total_trades']}` صفقة
✅ `{state['win']}` | ❌ `{state['lose']}` | 🎯 `{wr()}%`
💵 `${state['daily_profit_usd']:+.2f}`
🛑 خسائر: `${state['daily_loss_today']:.2f}` / `${state['daily_loss_limit_usd']}`""",
            parse_mode="Markdown", reply_markup=kb())

    elif q.data == "analyze":
        await q.edit_message_text("🧠 أحلل السوق...", reply_markup=kb())
        txt = await chat_with_claude("حلل سوق سولانا الآن وأعطني توصية، وهل يجب البحث في Pump.fun؟")
        await q.edit_message_text(f"🧠 *تحليل:*\n\n{txt}", parse_mode="Markdown", reply_markup=kb())

    elif q.data == "settings":
        await q.edit_message_text(f"""⚙️ *الإعدادات*
━━━━━━━━━━━━━━━
💵 حد الصفقة: `${state['max_trade_usd']}`
📉 Stop Loss: `{state['stop_loss_pct']}%`
🛑 خسارة يومية: `${state['daily_loss_limit_usd']}`
🚀 Pump.fun: `{'مفعّل' if state['pump_enabled'] else 'معطّل'}`
💧 حد سيولة Pump: `${state['pump_min_liquidity']}`
━━━━━━━━━━━━━━━
*للتغيير اكتب مثلاً:*
_"غير حد الصفقة إلى 3"_
_"غير stop loss إلى 15"_
_"غير الخسارة اليومية إلى 8"_""",
            parse_mode="Markdown", reply_markup=kb())

async def handle_message(update, ctx):
    txt = update.message.text.strip()

    if any(w in txt for w in ["وقف", "إيقاف", "ايقاف"]):
        state["running"] = False
        await update.message.reply_text("⛔ تم الإيقاف.", reply_markup=kb()); return

    if any(w in txt for w in ["شغل", "تشغيل", "ابدأ", "شغّل"]):
        state["running"] = True
        asyncio.create_task(trading_loop(ctx.application))
        if state["pump_enabled"]:
            asyncio.create_task(pump_loop(ctx.application))
        await update.message.reply_text("✅ البوت يعمل!", reply_markup=kb()); return

    nums = re.findall(r'\d+\.?\d*', txt)
    if "حد الصفقة" in txt and nums:
        state["max_trade_usd"] = float(nums[0])
        await update.message.reply_text(f"✅ حد الصفقة = ${nums[0]}", reply_markup=kb()); return
    if ("stop loss" in txt.lower() or "ستوب" in txt) and nums:
        state["stop_loss_pct"] = float(nums[0])
        await update.message.reply_text(f"✅ Stop Loss = {nums[0]}%", reply_markup=kb()); return
    if "خسارة يومية" in txt and nums:
        state["daily_loss_limit_usd"] = float(nums[0])
        await update.message.reply_text(f"✅ الخسارة اليومية = ${nums[0]}", reply_markup=kb()); return

    thinking = await update.message.reply_text("💬 أفكر...")
    reply = await chat_with_claude(txt)
    await thinking.edit_text(f"🤖 {reply}", reply_markup=kb())

async def trading_loop(app):
    owner = state["owner_chat_id"]
    last_price = await get_sol_price()
    in_pos = False; entry = 0.0; amt_sol = 0.0

    while state["running"]:
        await asyncio.sleep(300)
        if not state["running"]: break
        try:
            price = await get_sol_price()
            sol = await get_balance_sol()
            state["current_balance_sol"] = sol
            chg = ((price - last_price) / last_price) * 100

            if not in_pos and chg >= 0.5:
                usd = state["max_trade_usd"]
                ok, reason = safety_check(usd)
                if not ok:
                    await app.bot.send_message(owner, f"⚠️ {reason}")
                    last_price = price; continue

                sol_amt = usd / price
                q = await get_quote(SOL_MINT, USDC_MINT, int(sol_amt * 1_000_000_000))
                if q:
                    sig = await do_swap(q)
                    if sig:
                        in_pos = True; entry = price; amt_sol = sol_amt
                        state["total_trades"] += 1
                        await app.bot.send_message(owner, f"""🟢 *شراء SOL/USDC*
💵 `${price:.2f}` | 📦 `{sol_amt:.4f} SOL`
🎯 هدف: `${price*1.01:.2f}` | 🛑 وقف: `${price*0.997:.2f}`
🔗 [Solscan](https://solscan.io/tx/{sig})""", parse_mode="Markdown")

            elif in_pos:
                pct = ((price - entry) / entry) * 100
                if pct >= 1.0 or pct <= -0.3:
                    q = await get_quote(USDC_MINT, SOL_MINT, int(amt_sol * price * 1_000_000))
                    if q:
                        sig = await do_swap(q)
                        if sig:
                            pnl = amt_sol * (price - entry)
                            state["win" if pnl > 0 else "lose"] += 1
                            state["daily_profit_usd"] += pnl
                            if pnl < 0: state["daily_loss_today"] += abs(pnl)
                            in_pos = False
                            icon = "✅" if pnl > 0 else "❌"
                            await app.bot.send_message(owner, f"""{icon} *بيع {'ربح' if pnl > 0 else 'خسارة'}*
📊 `{pct:+.2f}%` | 💰 `${pnl:+.2f}`
💵 اليوم: `${state['daily_profit_usd']:+.2f}`
🔗 [Solscan](https://solscan.io/tx/{sig})""", parse_mode="Markdown")
            last_price = price

        except Exception as e:
            log.error(f"خطأ التداول: {e}")
            await asyncio.sleep(60)

async def pump_loop(app):
    owner = state["owner_chat_id"]
    bought_mints = set()
    log.info("🚀 Pump.fun loop بدأت")

    while state["running"] and state["pump_enabled"]:
        await asyncio.sleep(60)
        if not state["running"] or not state["pump_enabled"]: break
        try:
            await get_sol_price()
            tokens = await get_pump_new_tokens()
            if not tokens: continue

            good = await filter_pump_tokens(tokens)
            if not good: continue

            token = good[0]
            mint = token["mint"]
            if mint in bought_mints: continue
            if len(state["active_pump_tokens"]) >= 3: continue

            trade_usd = state["max_trade_usd"]
            ok, reason = safety_check(trade_usd)
            if not ok: continue

            sol_price = state["last_sol_price"] or 150
            sol_amount = trade_usd / sol_price

            await app.bot.send_message(owner, f"""🔍 *توكن Pump.fun جديد!*
━━━━━━━━━━━━━━━━━━
🪙 `{token['name']}` ({token['symbol']})
📊 Market Cap: `${token['market_cap']:,.0f}`
⏰ عمره: `{token['age_min']} دقيقة`
💰 سأشتري بـ `${trade_usd}` ...""", parse_mode="Markdown")

            sig = await buy_pump_token(mint, sol_amount)
            if sig:
                bought_mints.add(mint)
                state["active_pump_tokens"].append({
                    "mint": mint, "symbol": token["symbol"],
                    "name": token["name"], "sol_spent": sol_amount,
                    "buy_time": datetime.now().timestamp(),
                })
                state["total_trades"] += 1
                await app.bot.send_message(owner, f"""✅ *شراء Pump.fun تم!*
🪙 `{token['name']}` ({token['symbol']})
💰 `{sol_amount:.4f} SOL` (≈ `${trade_usd}`)
🎯 هدف: +50% | 🛑 وقف: -{state['stop_loss_pct']}%
🔗 [Solscan](https://solscan.io/tx/{sig})
⚠️ تداول توكنات جديدة = مخاطرة عالية!""", parse_mode="Markdown")
            else:
                await app.bot.send_message(owner, f"❌ فشل شراء `{token['symbol']}` - Jupiter لا يدعمه بعد", parse_mode="Markdown")

            # مراقبة والبيع بعد ساعتين
            to_remove = []
            for pos in state["active_pump_tokens"]:
                age_hours = (datetime.now().timestamp() - pos["buy_time"]) / 3600
                if age_hours >= 2:
                    await app.bot.send_message(owner, f"⏰ انتهت مدة `{pos['symbol']}` - يُنصح بالبيع اليدوي", parse_mode="Markdown")
                    to_remove.append(pos)
            for pos in to_remove:
                state["active_pump_tokens"].remove(pos)

        except Exception as e:
            log.error(f"خطأ Pump loop: {e}")
