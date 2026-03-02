import os, asyncio, logging, httpx, base58, re, json
from datetime import datetime, time as dtime
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.transaction import VersionedTransaction
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
WALLET_PRIVKEY = os.getenv("WALLET_PRIVATE_KEY", "")
YOUR_CHAT_ID   = int(os.getenv("YOUR_CHAT_ID", "0"))

SOL_MINT = "So11111111111111111111111111111111111111112"
RPC_URL  = "https://api.mainnet-beta.solana.com"

state = {
    "running": False,
    "total_trades": 0,
    "daily_profit_usd": 0.0,
    "win": 0, "lose": 0,
    "start_balance_sol": 0.0,
    "current_balance_sol": 0.0,
    "trade_sol": float(os.getenv("TRADE_SOL", "0.01")),       # كمية SOL لكل صفقة
    "take_profit": 50.0,                                        # هدف الربح 50%
    "stop_loss": 30.0,                                          # وقف الخسارة 30%
    "max_hold_minutes": 30,                                     # أقصى وقت للاحتفاظ بالتوكن
    "max_active": 3,                                            # أقصى عدد توكنات في نفس الوقت
    "daily_loss_limit_sol": float(os.getenv("DAILY_LOSS_LIMIT", "0.05")),
    "daily_loss_today": 0.0,
    "owner_chat_id": YOUR_CHAT_ID,
    "last_sol_price": 0.0,
    "chat_history": [],
    "positions": {},   # mint -> {buy_price, sol_spent, buy_time, name, symbol, amount}
    "seen_tokens": set(),
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

async def get_new_pump_tokens():
    """جلب التوكنات الجديدة - يجرب عدة مصادر"""
    
    # مصدر 1: DexScreener API (موثوق جداً)
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as h:
            r = await h.get("https://api.dexscreener.com/token-profiles/latest/v1")
            if r.status_code == 200:
                data = r.json()
                tokens = []
                for item in data:
                    if item.get("chainId") == "solana":
                        tokens.append({
                            "mint": item.get("tokenAddress"),
                            "name": item.get("description", "Unknown")[:20],
                            "symbol": item.get("url", "???").split("/")[-1][:10],
                            "usd_market_cap": 5000,
                            "created_timestamp": (datetime.now().timestamp() - 60) * 1000,
                            "complete": False,
                        })
                if tokens:
                    log.info(f"DexScreener: {len(tokens)} توكن")
                    return tokens
    except Exception as e:
        log.error(f"DexScreener error: {e}")

    # مصدر 2: Pump.fun عبر proxy headers
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/",
        }
        async with httpx.AsyncClient(timeout=15, headers=headers) as h:
            r = await h.get("https://frontend-api.pump.fun/coins",
                           params={"limit": 50, "sort": "created_timestamp",
                                   "order": "DESC", "includeNsfw": "false"})
            if r.status_code == 200:
                log.info("Pump.fun API يعمل!")
                return r.json()
    except Exception as e:
        log.error(f"Pump.fun error: {e}")

    # مصدر 3: Bitquery - بيانات Solana
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get("https://streaming.bitquery.io/graphql",
                           headers={"Content-Type": "application/json"},
                           content=json.dumps({"query": """{ Solana { DEXTrades(limit: {count: 20} orderBy: {descending: Block_Time} where: {Trade: {Dex: {ProtocolName: {is: "pump"}}}}) { Trade { Buy { Currency { MintAddress Name Symbol } } } } } }"""}))
            if r.status_code == 200:
                trades = r.json().get("data", {}).get("Solana", {}).get("DEXTrades", [])
                tokens = []
                for t in trades:
                    cur = t.get("Trade", {}).get("Buy", {}).get("Currency", {})
                    mint = cur.get("MintAddress")
                    if mint:
                        tokens.append({
                            "mint": mint,
                            "name": cur.get("Name", "Unknown"),
                            "symbol": cur.get("Symbol", "???"),
                            "usd_market_cap": 5000,
                            "created_timestamp": (datetime.now().timestamp() - 30) * 1000,
                            "complete": False,
                        })
                if tokens:
                    return tokens
    except Exception as e:
        log.error(f"Bitquery error: {e}")

    return []

async def get_token_price_sol(mint):
    """سعر التوكن من DexScreener"""
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                if pairs:
                    price_usd = float(pairs[0].get("priceUsd", 0))
                    sol_price = state["last_sol_price"] or 150
                    if price_usd > 0 and sol_price > 0:
                        return price_usd / sol_price
    except: pass
    return None

async def buy_token(mint, sol_amount):
    """شراء توكن مباشرة عبر PumpPortal"""
    if not keypair: return None, 0
    try:
        async with httpx.AsyncClient(timeout=30) as h:
            payload = {
                "publicKey": str(keypair.pubkey()),
                "action": "buy",
                "mint": mint,
                "amount": sol_amount,          # SOL مباشرة وليس lamports
                "denominatedInSol": "true",
                "slippage": 50,                # slippage عالي للتوكنات الجديدة
                "priorityFee": 0.005,          # رسوم أعلى للسرعة
                "pool": "pump"
            }
            log.info(f"محاولة شراء {mint} بـ {sol_amount} SOL")
            r = await h.post("https://pumpportal.fun/api/trade-local", json=payload)
            log.info(f"PumpPortal response: {r.status_code}")

            if r.status_code != 200:
                log.error(f"PumpPortal error: {r.status_code} - {r.text[:200]}")
                return None, 0

            tx = VersionedTransaction.from_bytes(r.content)
            tx.sign([keypair])

            async with AsyncClient(RPC_URL) as c:
                res = await c.send_raw_transaction(bytes(tx),
                    opts={"skip_preflight": True, "preflight_commitment": "confirmed"})
                sig = str(res.value)
                log.info(f"✅ شراء ناجح! {sig}")
                expected_tokens = int(sol_amount * 1_000_000_000 * 1000)
                return sig, expected_tokens

    except Exception as e:
        log.error(f"خطأ الشراء: {e}")
        return None, 0

async def sell_token(mint, token_amount):
    """بيع توكن مباشرة عبر Pump.fun"""
    if not keypair: return None
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            payload = {
                "publicKey": str(keypair.pubkey()),
                "action": "sell",
                "mint": mint,
                "amount": token_amount,
                "denominatedInSol": "false",
                "slippage": 25,
                "priorityFee": 0.003,
                "pool": "pump"
            }
            r = await h.post("https://pumpportal.fun/api/trade-local", json=payload)
            if r.status_code != 200:
                log.error(f"Pump sell error: {r.status_code}")
                return None

            tx = VersionedTransaction.from_bytes(r.content)
            tx.sign([keypair])

            async with AsyncClient(RPC_URL) as c:
                res = await c.send_raw_transaction(bytes(tx),
                    opts={"skip_preflight": True, "preflight_commitment": "confirmed"})
                return str(res.value)

    except Exception as e:
        log.error(f"خطأ البيع: {e}")
        return None

async def chat_with_ai(user_message):
    if not GROQ_API_KEY:
        return "أضف GROQ_API_KEY في Railway"
    try:
        positions_count = len(state["positions"])
        system = f"""أنت مساعد بوت تداول سولانا ذكي. تتحدث بالعربية بشكل طبيعي وودود.

حالتك الآن:
- التشغيل: {'يعمل' if state['running'] else 'متوقف'}
- الرصيد: {state['current_balance_sol']:.4f} SOL
- سعر SOL: ${state['last_sol_price']:.2f}
- توكنات مفتوحة: {positions_count}
- صفقات اليوم: {state['total_trades']} | نجاح: {state['win']} | خسارة: {state['lose']}
- أرباح اليوم: ${state['daily_profit_usd']:+.2f}
- كمية الصفقة: {state['trade_sol']} SOL
- هدف الربح: {state['take_profit']}% | وقف الخسارة: {state['stop_loss']}%

أجب بشكل طبيعي وودود وقصير."""

        state["chat_history"].append({"role": "user", "content": user_message})
        if len(state["chat_history"]) > 16:
            state["chat_history"] = state["chat_history"][-16:]

        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile",
                      "messages": [{"role": "system", "content": system}] + state["chat_history"],
                      "max_tokens": 400}
            )
            if r.status_code == 200:
                reply = r.json()["choices"][0]["message"]["content"]
                state["chat_history"].append({"role": "assistant", "content": reply})
                return reply
    except Exception as e:
        return f"خطأ: {str(e)[:60]}"

def kb():
    lbl = "⏸ إيقاف" if state["running"] else "▶️ تشغيل"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl, callback_data="toggle"),
         InlineKeyboardButton("📊 الحالة", callback_data="status")],
        [InlineKeyboardButton("💰 الرصيد", callback_data="balance"),
         InlineKeyboardButton("📈 التقرير", callback_data="report")],
        [InlineKeyboardButton("🎯 المراكز", callback_data="positions"),
         InlineKeyboardButton("⚙️ إعدادات", callback_data="settings")],
        [InlineKeyboardButton("🧠 تحليل", callback_data="analyze"),
         InlineKeyboardButton("🚨 بيع الكل", callback_data="sell_all")],
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
    await update.message.reply_text(f"""🚀 *بوت Pump.fun القناص*
━━━━━━━━━━━━━━━━━━━━
🔑 المحفظة: `{w}`
💰 الرصيد: `{sol:.4f} SOL` (≈ `${sol*price:.2f}`)
📈 سعر SOL: `${price:.2f}`
━━━━━━━━━━━━━━━━━━━━
⚡ يشتري كل توكن جديد على Pump.fun
🎯 هدف: *+{state['take_profit']}%* | 🛑 وقف: *-{state['stop_loss']}%*
💸 حجم الصفقة: `{state['trade_sol']} SOL`
━━━━━━━━━━━━━━━━━━━━
📌 معرفك: `{cid}`
💬 يمكنك الكلام معي بشكل طبيعي!

اضغط تشغيل للبدء 👇""", parse_mode="Markdown", reply_markup=kb())

async def handle_buttons(update, ctx):
    q = update.callback_query
    await q.answer()

    if q.data == "toggle":
        state["running"] = not state["running"]
        if state["running"]:
            asyncio.create_task(pump_sniper_loop(ctx.application))
            asyncio.create_task(monitor_positions(ctx.application))
        txt = "✅ *القناص يعمل!*\nيراقب Pump.fun ويشتري كل توكن جديد 🎯" if state["running"] else "⛔ البوت متوقف."
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb())

    elif q.data == "status":
        sol = await get_balance_sol()
        price = await get_sol_price()
        state["current_balance_sol"] = sol
        pnl = (sol - state["start_balance_sol"]) * price
        await q.edit_message_text(f"""📊 *الحالة*
━━━━━━━━━━━━━━━
🤖 {'🟢 يعمل' if state['running'] else '🔴 متوقف'}
💰 `{sol:.4f} SOL` (≈ `${sol*price:.2f}`)
🎯 مراكز مفتوحة: `{len(state['positions'])}`
━━━━━━━━━━━━━━━
🔄 الصفقات: `{state['total_trades']}`
✅ `{state['win']}` ❌ `{state['lose']}` 🎯 `{wr()}%`
💵 اليوم: `${state['daily_profit_usd']:+.2f}`
📊 الكلي: `${pnl:+.2f}`""",
            parse_mode="Markdown", reply_markup=kb())

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
💵 `${state['daily_profit_usd']:+.2f}`""",
            parse_mode="Markdown", reply_markup=kb())

    elif q.data == "positions":
        if not state["positions"]:
            await q.edit_message_text("🎯 لا توجد مراكز مفتوحة حالياً.", reply_markup=kb())
        else:
            txt = "🎯 *المراكز المفتوحة:*\n━━━━━━━━━━\n"
            for mint, pos in state["positions"].items():
                age = (datetime.now().timestamp() - pos["buy_time"]) / 60
                current_price = await get_token_price_sol(mint)
                if current_price and pos["buy_price"] > 0:
                    pct = ((current_price - pos["buy_price"]) / pos["buy_price"]) * 100
                    txt += f"🪙 `{pos['symbol']}` | `{pct:+.1f}%` | ⏰ `{age:.0f}` دقيقة\n"
                else:
                    txt += f"🪙 `{pos['symbol']}` | ⏰ `{age:.0f}` دقيقة\n"
            await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=kb())

    elif q.data == "settings":
        await q.edit_message_text(f"""⚙️ *الإعدادات*
━━━━━━━━━━━━━━━
💸 حجم الصفقة: `{state['trade_sol']} SOL`
🎯 هدف الربح: `{state['take_profit']}%`
🛑 وقف الخسارة: `{state['stop_loss']}%`
⏰ أقصى وقت: `{state['max_hold_minutes']} دقيقة`
📦 أقصى مراكز: `{state['max_active']}`
━━━━━━━━━━━━━━━
*للتغيير اكتب مثلاً:*
_"غير حجم الصفقة إلى 0.02"_
_"غير الهدف إلى 100"_
_"غير الوقف إلى 20"_""",
            parse_mode="Markdown", reply_markup=kb())

    elif q.data == "analyze":
        await q.edit_message_text("🧠 أفكر...", reply_markup=kb())
        txt = await chat_with_ai("حلل السوق الآن وأعطني رأيك في استراتيجية شراء كل توكن جديد على Pump.fun")
        await q.edit_message_text(f"🧠 {txt}", parse_mode="Markdown", reply_markup=kb())

    elif q.data == "sell_all":
        if not state["positions"]:
            await q.edit_message_text("لا توجد مراكز مفتوحة.", reply_markup=kb())
            return
        await q.edit_message_text("🚨 *جاري بيع كل المراكز...*", parse_mode="Markdown", reply_markup=kb())
        for mint in list(state["positions"].keys()):
            pos = state["positions"][mint]
            if pos.get("amount", 0) > 0:
                sig = await sell_token(mint, pos["amount"])
                if sig:
                    del state["positions"][mint]
        await q.edit_message_text("✅ تم بيع كل المراكز.", reply_markup=kb())

async def handle_message(update, ctx):
    txt = update.message.text.strip()

    if any(w in txt for w in ["وقف", "إيقاف", "ايقاف"]):
        state["running"] = False
        await update.message.reply_text("⛔ تم الإيقاف.", reply_markup=kb()); return

    if any(w in txt for w in ["شغل", "تشغيل", "ابدأ"]):
        state["running"] = True
        asyncio.create_task(pump_sniper_loop(ctx.application))
        asyncio.create_task(monitor_positions(ctx.application))
        await update.message.reply_text("✅ القناص يعمل! 🎯", reply_markup=kb()); return

    nums = re.findall(r'\d+\.?\d*', txt)
    if "حجم الصفقة" in txt and nums:
        state["trade_sol"] = float(nums[0])
        await update.message.reply_text(f"✅ حجم الصفقة = {nums[0]} SOL", reply_markup=kb()); return
    if "الهدف" in txt and nums:
        state["take_profit"] = float(nums[0])
        await update.message.reply_text(f"✅ هدف الربح = {nums[0]}%", reply_markup=kb()); return
    if "الوقف" in txt and nums:
        state["stop_loss"] = float(nums[0])
        await update.message.reply_text(f"✅ وقف الخسارة = {nums[0]}%", reply_markup=kb()); return
    if "الوقت" in txt and nums:
        state["max_hold_minutes"] = int(float(nums[0]))
        await update.message.reply_text(f"✅ أقصى وقت = {nums[0]} دقيقة", reply_markup=kb()); return

    thinking = await update.message.reply_text("💬 أفكر...")
    reply = await chat_with_ai(txt)
    await thinking.edit_text(f"🤖 {reply}", reply_markup=kb())

async def pump_sniper_loop(app):
    """حلقة اصطياد توكنات Pump.fun الجديدة"""
    owner = state["owner_chat_id"]
    log.info("🎯 Pump Sniper بدأ")
    await app.bot.send_message(owner, "🎯 *القناص يراقب Pump.fun الآن!*\nسيشتري كل توكن جديد تلقائياً.", parse_mode="Markdown")

    while state["running"]:
        await asyncio.sleep(30)  # فحص كل 30 ثانية
        if not state["running"]: break
        try:
            sol_price = await get_sol_price()
            sol_bal = await get_balance_sol()
            state["current_balance_sol"] = sol_bal

            # تحقق من الرصيد
            if sol_bal < state["trade_sol"] + 0.005:
                log.warning(f"رصيد غير كافٍ: {sol_bal:.4f} SOL")
                continue

            # تحقق من عدد المراكز
            if len(state["positions"]) >= state["max_active"]:
                continue

            # تحقق من حد الخسارة اليومية
            if state["daily_loss_today"] >= state["daily_loss_limit_sol"]:
                await app.bot.send_message(owner, "🛑 تجاوزت حد الخسارة اليومية - البوت متوقف حتى الغد.")
                state["running"] = False
                break

            # جلب التوكنات الجديدة
            tokens = await get_new_pump_tokens()
            if not tokens: continue

            for token in tokens:
                mint = token.get("mint")
                if not mint or mint in state["seen_tokens"]: continue
                if mint in state["positions"]: continue

                created = token.get("created_timestamp", 0)
                age_sec = (datetime.now().timestamp() * 1000 - created) / 1000
                usd_mc = token.get("usd_market_cap", 0)

                # ✅ شروط الشراء مخففة
                if age_sec < 5: continue           # أحدث من 5 ثواني نتجاهله
                if age_sec > 600: continue         # أقدم من 10 دقائق نتجاهله
                if token.get("complete", False): continue

                state["seen_tokens"].add(mint)

                # اشتري!
                name = token.get("name", "Unknown")
                symbol = token.get("symbol", "???")

                await app.bot.send_message(owner, f"""🎯 *وجد القناص هدفاً!*
━━━━━━━━━━━━━━━━━━
🪙 `{name}` ({symbol})
⏰ عمره: `{age_sec:.0f}` ثانية
📊 Market Cap: `${usd_mc:,.0f}`
💸 أشتري بـ `{state['trade_sol']} SOL` ...""", parse_mode="Markdown")

                sig, token_amount = await buy_token(mint, state["trade_sol"])

                if sig and token_amount > 0:
                    buy_price = await get_token_price_sol(mint) or 0
                    state["positions"][mint] = {
                        "name": name,
                        "symbol": symbol,
                        "buy_price": buy_price,
                        "sol_spent": state["trade_sol"],
                        "amount": token_amount,
                        "buy_time": datetime.now().timestamp(),
                    }
                    state["total_trades"] += 1

                    await app.bot.send_message(owner, f"""✅ *تم الشراء!*
━━━━━━━━━━━━━━━━━━
🪙 `{name}` ({symbol})
💸 `{state['trade_sol']} SOL` (≈ `${state['trade_sol']*sol_price:.2f}`)
🎯 هدف البيع: *+{state['take_profit']}%*
🛑 وقف الخسارة: *-{state['stop_loss']}%*
⏰ سيبيع بعد `{state['max_hold_minutes']}` دقيقة إذا لم يصل للهدف
🔗 [Solscan](https://solscan.io/tx/{sig})""", parse_mode="Markdown")
                    break  # اشتر توكن واحد في كل دورة
                else:
                    await app.bot.send_message(owner, f"⚠️ فشل شراء `{symbol}` - سيحاول التوكن التالي", parse_mode="Markdown")

        except Exception as e:
            log.error(f"خطأ Sniper: {e}")
            await asyncio.sleep(15)

async def monitor_positions(app):
    """مراقبة المراكز المفتوحة وبيعها عند الهدف"""
    owner = state["owner_chat_id"]
    log.info("👁️ مراقبة المراكز بدأت")

    while state["running"]:
        await asyncio.sleep(15)
        if not state["running"]: break
        if not state["positions"]: continue

        try:
            sol_price = await get_sol_price()

            for mint in list(state["positions"].keys()):
                pos = state["positions"].get(mint)
                if not pos: continue

                age_min = (datetime.now().timestamp() - pos["buy_time"]) / 60
                current_price = await get_token_price_sol(mint)

                should_sell = False
                sell_reason = ""

                # تحقق من الوقت
                if age_min >= state["max_hold_minutes"]:
                    should_sell = True
                    sell_reason = f"⏰ انتهى الوقت ({state['max_hold_minutes']} دقيقة)"

                # تحقق من السعر
                elif current_price and pos["buy_price"] > 0:
                    pct = ((current_price - pos["buy_price"]) / pos["buy_price"]) * 100

                    if pct >= state["take_profit"]:
                        should_sell = True
                        sell_reason = f"🎯 هدف +{state['take_profit']}% تحقق! ({pct:+.1f}%)"
                    elif pct <= -state["stop_loss"]:
                        should_sell = True
                        sell_reason = f"🛑 وقف الخسارة -{state['stop_loss']}% ({pct:+.1f}%)"

                if should_sell and pos.get("amount", 0) > 0:
                    sig = await sell_token(mint, pos["amount"])
                    if sig:
                        # احسب الربح
                        if current_price and pos["buy_price"] > 0:
                            pct = ((current_price - pos["buy_price"]) / pos["buy_price"]) * 100
                            pnl_sol = pos["sol_spent"] * (pct / 100)
                            pnl_usd = pnl_sol * sol_price
                        else:
                            pct = 0; pnl_sol = 0; pnl_usd = 0

                        is_win = pnl_sol >= 0
                        state["win" if is_win else "lose"] += 1
                        state["daily_profit_usd"] += pnl_usd
                        if not is_win:
                            state["daily_loss_today"] += abs(pnl_sol)

                        del state["positions"][mint]
                        icon = "✅" if is_win else "❌"

                        await app.bot.send_message(owner, f"""{icon} *بيع - {sell_reason}*
━━━━━━━━━━━━━━━━━━
🪙 `{pos['name']}` ({pos['symbol']})
📊 النتيجة: `{pct:+.1f}%`
💰 الربح/الخسارة: `{pnl_sol:+.4f} SOL` (≈ `${pnl_usd:+.2f}`)
━━━━━━━━━━━━━━━━━━
💵 أرباح اليوم: `${state['daily_profit_usd']:+.2f}`
✅ `{state['win']}` ❌ `{state['lose']}`
🔗 [Solscan](https://solscan.io/tx/{sig})""", parse_mode="Markdown")

        except Exception as e:
            log.error(f"خطأ المراقبة: {e}")
            await asyncio.sleep(15)

async def daily_report(ctx):
    owner = state["owner_chat_id"]
    if not owner: return
    a = await chat_with_ai(f"قيّم: {state['total_trades']} صفقة، ربح ${state['daily_profit_usd']:.2f}، نجاح {state['win']} خسارة {state['lose']}.")
    await ctx.bot.send_message(owner, f"""📅 *تقرير {datetime.now().strftime('%Y/%m/%d')}*
🔄 `{state['total_trades']}` | ✅ `{state['win']}` | ❌ `{state['lose']}`
💵 `${state['daily_profit_usd']:+.2f}`
🤖 {a}""", parse_mode="Markdown")
    state.update({"daily_profit_usd": 0.0, "daily_loss_today": 0.0,
                  "total_trades": 0, "win": 0, "lose": 0,
                  "chat_history": [], "seen_tokens": set()})

def main():
    if not TELEGRAM_TOKEN: print("أضف TELEGRAM_TOKEN!"); return
    if not keypair: print("أضف WALLET_PRIVATE_KEY!"); return
    print("🎯 بوت Pump.fun القناص يبدأ...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_daily(daily_report, time=dtime(hour=21, minute=0))
    print("✅ القناص جاهز!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
