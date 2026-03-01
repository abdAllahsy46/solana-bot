import os,asyncio,logging,httpx,base58
from datetime import datetime,time as dtime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.transaction import VersionedTransaction
from telegram import Update,InlineKeyboardButton,InlineKeyboardMarkup
from telegram.ext import Application,CommandHandler,MessageHandler,CallbackQueryHandler,ContextTypes,filters
import anthropic

TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN","")
CLAUDE_API_KEY=os.getenv("CLAUDE_API_KEY","")
WALLET_PRIVKEY=os.getenv("WALLET_PRIVATE_KEY","")
YOUR_CHAT_ID=int(os.getenv("YOUR_CHAT_ID","0"))
SOL_MINT="So11111111111111111111111111111111111111112"
USDC_MINT="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_URL="https://api.mainnet-beta.solana.com"

state={"running":False,"total_trades":0,"daily_profit_usd":0.0,"win":0,"lose":0,"start_balance_sol":0.0,"current_balance_sol":0.0,"max_trade_usd":float(os.getenv("MAX_TRADE_USD","10")),"stop_loss_pct":float(os.getenv("STOP_LOSS_PCT","2")),"daily_loss_limit_usd":float(os.getenv("DAILY_LOSS_LIMIT","5")),"daily_loss_today":0.0,"strategy":"Momentum","owner_chat_id":YOUR_CHAT_ID,"last_sol_price":0.0}

logging.basicConfig(level=logging.INFO)
log=logging.getLogger(__name__)

def load_keypair():
    pk=WALLET_PRIVKEY.strip()
    if not pk:return None
    try:return Keypair.from_bytes(base58.b58decode(pk))
    except:return None

keypair=load_keypair()

async def get_balance_sol():
    if not keypair:return 0.0
    try:
        async with AsyncClient(RPC_URL) as c:
            r=await c.get_balance(keypair.pubkey(),commitment=Confirmed)
            return r.value/1_000_000_000
    except:return 0.0

async def get_sol_price():
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r=await h.get("https://api.coingecko.com/api/v3/simple/price",params={"ids":"solana","vs_currencies":"usd"})
            p=r.json()["solana"]["usd"]
            state["last_sol_price"]=p
            return p
    except:return state["last_sol_price"] or 150.0

async def get_quote(inp,out,amt):
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r=await h.get("https://quote-api.jup.ag/v6/quote",params={"inputMint":inp,"outputMint":out,"amount":amt,"slippageBps":50})
            if r.status_code==200:return r.json()
    except:pass
    return None

async def do_swap(quote):
    if not keypair:return None
    try:
        async with httpx.AsyncClient(timeout=30) as h:
            r=await h.post("https://quote-api.jup.ag/v6/swap",json={"quoteResponse":quote,"userPublicKey":str(keypair.pubkey()),"wrapAndUnwrapSol":True,"dynamicComputeUnitLimit":True,"prioritizationFeeLamports":1000})
            if r.status_code!=200:return None
            tx=VersionedTransaction.from_bytes(base58.b58decode(r.json()["swapTransaction"]))
            tx.sign([keypair])
            async with AsyncClient(RPC_URL) as c:
                res=await c.send_raw_transaction(bytes(tx),opts={"skip_preflight":False,"preflight_commitment":"confirmed"})
                return str(res.value)
    except Exception as e:
        log.error(f"swap error:{e}")
        return None

def safety_check(usd):
    if usd>state["max_trade_usd"]:return False,f"❌ تجاوز الحد ${state['max_trade_usd']}"
    if state["daily_loss_today"]>=state["daily_loss_limit_usd"]:return False,"🛑 تجاوز حد الخسارة اليومية"
    if state["current_balance_sol"]<(usd/(state["last_sol_price"] or 150))+0.01:return False,"❌ رصيد غير كافٍ"
    return True,"✅"

async def ask_claude(msg):
    if not CLAUDE_API_KEY:return "أضف CLAUDE_API_KEY في Railway"
    try:
        c=anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        sys=f"أنت مساعد بوت تداول سولانا. البوت:{'يعمل' if state['running'] else 'متوقف'} الرصيد:{state['current_balance_sol']:.4f}SOL سعر SOL:${state['last_sol_price']:.2f} صفقات:{state['total_trades']} ربح:${state['daily_profit_usd']:+.2f} أجب بالعربية بإيجاز."
        r=c.messages.create(model="claude-sonnet-4-20250514",max_tokens=400,system=sys,messages=[{"role":"user","content":msg}])
        return r.content[0].text
    except Exception as e:return f"خطأ:{str(e)[:60]}"

def wr():
    t=state["win"]+state["lose"]
    return round(state["win"]/t*100,1) if t else 0

def kb():
    lbl="⏸ إيقاف"if state["running"]else"▶️ تشغيل"
    return InlineKeyboardMarkup([[InlineKeyboardButton(lbl,callback_data="toggle"),InlineKeyboardButton("📊 الحالة",callback_data="status")],[InlineKeyboardButton("📈 التقرير",callback_data="report"),InlineKeyboardButton("💰 الرصيد",callback_data="balance")],[InlineKeyboardButton("🧠 تحليل",callback_data="analyze"),InlineKeyboardButton("⚙️ إعدادات",callback_data="settings")]])

async def cmd_start(update,ctx):
    cid=update.effective_chat.id
    state["owner_chat_id"]=cid
    sol=await get_balance_sol()
    price=await get_sol_price()
    state["current_balance_sol"]=sol
    state["start_balance_sol"]=sol
    w=str(keypair.pubkey())[:8]+"..."if keypair else"❌ غير محمّلة"
    await update.message.reply_text(f"🤖 *بوت سولانا الحقيقي*\n━━━━━━━━━━━━━━━\n🔑 المحفظة: `{w}`\n💰 الرصيد: `{sol:.4f} SOL` (≈`${sol*price:.2f}`)\n📊 سعر SOL: `${price:.2f}`\n━━━━━━━━━━━━━━━\n🛡 حد الصفقة: `${state['max_trade_usd']}`\n🛡 خسارة يومية: `${state['daily_loss_limit_usd']}`\n━━━━━━━━━━━━━━━\n📌 معرفك: `{cid}`\n_(أضفه في Railway كـ YOUR_CHAT_ID)_\n\nاضغط تشغيل للبدء 👇",parse_mode="Markdown",reply_markup=kb())

async def cmd_status(update,ctx):
    sol=await get_balance_sol()
    price=await get_sol_price()
    state["current_balance_sol"]=sol
    pnl=(sol-state["start_balance_sol"])*price
    txt=f"📊 *الحالة*\n━━━━━━━━━━━━━━━\n🤖 {'🟢 يعمل'if state['running']else'🔴 متوقف'}\n💰 `{sol:.4f} SOL` (≈`${sol*price:.2f}`)\n📈 SOL: `${price:.2f}`\n🔄 الصفقات: `{state['total_trades']}`\n✅`{state['win']}` ❌`{state['lose']}` 🎯`{wr()}%`\n💵 اليوم: `${state['daily_profit_usd']:+.2f}`\n📊 الكلي: `${pnl:+.2f}`"
    msg=update.message or update.callback_query.message
    await msg.reply_text(txt,parse_mode="Markdown",reply_markup=kb())

async def handle_buttons(update,ctx):
    q=update.callback_query
    await q.answer()
    if q.data=="toggle":
        state["running"]=not state["running"]
        if state["running"]:asyncio.create_task(trading_loop(ctx.application))
        await q.edit_message_text("✅ البوت يعمل!"if state["running"]else"⛔ البوت متوقف.",reply_markup=kb())
    elif q.data=="status":await cmd_status(update,ctx)
    elif q.data=="balance":
        sol=await get_balance_sol();price=await get_sol_price()
        state["current_balance_sol"]=sol
        await q.edit_message_text(f"💰 *الرصيد*\n`{sol:.6f} SOL`\n≈`${sol*price:.2f}`",parse_mode="Markdown",reply_markup=kb())
    elif q.data=="report":
        await q.edit_message_text(f"📈 *تقرير اليوم*\n🔄`{state['total_trades']}` ✅`{state['win']}` ❌`{state['lose']}`\n🎯`{wr()}%` 💵`${state['daily_profit_usd']:+.2f}`\n🛑 خسائر:`${state['daily_loss_today']:.2f}`/`${state['daily_loss_limit_usd']}`",parse_mode="Markdown",reply_markup=kb())
    elif q.data=="analyze":
        await q.edit_message_text("🧠 كلود يحلل...",reply_markup=kb())
        txt=await ask_claude("حلل سوق سولانا وأعطني توصية قصيرة.")
        await q.edit_message_text(f"🧠 *تحليل:*\n\n{txt}",parse_mode="Markdown",reply_markup=kb())
    elif q.data=="settings":
        await q.edit_message_text(f"⚙️ *الإعدادات*\n💵 حد الصفقة:`${state['max_trade_usd']}`\n📉 Stop Loss:`{state['stop_loss_pct']}%`\n🛑 خسارة يومية:`${state['daily_loss_limit_usd']}`\n\nلتغيير اكتب مثلاً:\n_'غير حد الصفقة إلى 5'_",parse_mode="Markdown",reply_markup=kb())

async def handle_message(update,ctx):
    txt=update.message.text.strip()
    if any(w in txt for w in["وقف","إيقاف","ايقاف"]):
        state["running"]=False
        await update.message.reply_text("⛔ تم الإيقاف.",reply_markup=kb());return
    if any(w in txt for w in["شغل","تشغيل","ابدأ","شغّل"]):
        state["running"]=True
        asyncio.create_task(trading_loop(ctx.application))
        await update.message.reply_text("✅ البوت يعمل!",reply_markup=kb());return
    import re
    if"حد الصفقة"in txt:
        n=re.findall(r'\d+\.?\d*',txt)
        if n:state["max_trade_usd"]=float(n[0]);await update.message.reply_text(f"✅ حد الصفقة=${n[0]}",reply_markup=kb());return
    t=await update.message.reply_text("🧠 كلود يفكر...")
    r=await ask_claude(txt)
    await t.edit_text(f"🤖 {r}",reply_markup=kb())

async def trading_loop(app):
    owner=state["owner_chat_id"]
    last_price=await get_sol_price()
    in_pos=False;entry=0.0;amt_sol=0.0
    while state["running"]:
        await asyncio.sleep(300)
        if not state["running"]:break
        try:
            price=await get_sol_price()
            sol=await get_balance_sol()
            state["current_balance_sol"]=sol
            chg=((price-last_price)/last_price)*100
            if not in_pos and chg>=0.5:
                usd=state["max_trade_usd"]
                ok,reason=safety_check(usd)
                if not ok:await app.bot.send_message(owner,f"⚠️ {reason}");last_price=price;continue
                sol_amt=usd/price
                q=await get_quote(SOL_MINT,USDC_MINT,int(sol_amt*1_000_000_000))
                if q:
                    sig=await do_swap(q)
                    if sig:
                        in_pos=True;entry=price;amt_sol=sol_amt;state["total_trades"]+=1
                        await app.bot.send_message(owner,f"🟢 *شراء!*\n💵`${price:.2f}` 📦`{sol_amt:.4f}SOL`\n🎯هدف:`${price*1.01:.2f}` 🛑وقف:`${price*0.997:.2f}`\n[Solscan](https://solscan.io/tx/{sig})",parse_mode="Markdown")
            elif in_pos:
                pct=((price-entry)/entry)*100
                if pct>=1.0 or pct<=-0.3:
                    q=await get_quote(USDC_MINT,SOL_MINT,int(amt_sol*price*1_000_000))
                    if q:
                        sig=await do_swap(q)
                        if sig:
                            pnl=amt_sol*(price-entry)
                            state["win"if pnl>0 else"lose"]+=1
                            state["daily_profit_usd"]+=pnl
                            if pnl<0:state["daily_loss_today"]+=abs(pnl)
                            in_pos=False
                            await app.bot.send_message(owner,f"{'✅'if pnl>0 else'❌'} *بيع {'ربح'if pnl>0 else'خسارة'}*\n📊`{pct:+.2f}%` 💰`${pnl:+.2f}`\n💵اليوم:`${state['daily_profit_usd']:+.2f}`\n[Solscan](https://solscan.io/tx/{sig})",parse_mode="Markdown")
            last_price=price
        except Exception as e:log.error(f"loop err:{e}");await asyncio.sleep(60)

async def daily_report(ctx):
    owner=state["owner_chat_id"]
    if not owner:return
    a=await ask_claude(f"قيّم: {state['total_trades']} صفقة، ربح ${state['daily_profit_usd']:.2f}، نجاح {state['win']} خسارة {state['lose']}. توصية لغد.")
    await ctx.bot.send_message(owner,f"📅 *تقرير {datetime.now().strftime('%Y/%m/%d')}*\n🔄`{state['total_trades']}` ✅`{state['win']}` ❌`{state['lose']}`\n💵`${state['daily_profit_usd']:+.2f}`\n\n🤖{a}",parse_mode="Markdown")
    state["daily_profit_usd"]=0.0;state["daily_loss_today"]=0.0;state["total_trades"]=0;state["win"]=0;state["lose"]=0

def main():
    if not TELEGRAM_TOKEN:print("❌ أضف TELEGRAM_TOKEN!");return
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,handle_message))
    app.job_queue.run_daily(daily_report,time=dtime(hour=21,minute=0))
    print("✅ البوت جاهز!")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":main()
