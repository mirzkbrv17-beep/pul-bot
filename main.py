import asyncio, html, logging, os, time
from contextlib import suppress

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus as S, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup as IM, Message)

logging.basicConfig(level=logging.INFO)

# ================== SOZLAMALAR ==================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))
CONTACT = os.getenv("CONTACT", "mrzaakbarov")

# Telethon (ixtiyoriy) - kanal postlarining ko'rishlar sonini o'qish uchun
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("SESSION_STRING", "")

GROUP_UNIT, GROUP_REWARD = 100, 40_000      # har 100 odam -> 40 000
POST_UNIT, POST_REWARD = 50, 20_000         # har 50 post -> 20 000
DRIVE_UNIT, DRIVE_REWARD = 200, 90_000      # har 200 odam -> 90 000 (min 200)
REF_REWARD = 500                            # har taklif -> 500
MIN_WD, MIN_WD_REF = 200_000, 100_000
REF_WAIT = 24 * 3600

ACCTS = ["Instagram", "PUBG", "Free Fire", "Dream League Soccer 27", "FC Mobile",
         "YouTube", "Google", "Telegram"]

# Tugma kalitlari: (oddiy emoji - zaxira)
FB = {"group": "👥", "post": "📣", "trade": "🛒", "drive": "📈", "invite": "🔗", "bal": "💰",
      "wd": "💸", "adm": "⚙️", "back": "⬅️", "chk": "✅", "add": "➕", "del": "🗑",
      "force": "🔒", "stat": "📊", "get": "📥", "ok": "✅", "no": "❌", "go": "➡️"}
EMO: dict[str, str] = {}   # kalit -> premium emoji id (bazadan yuklanadi)


def B(label, e, cb=None, url=None):
    kw = {"callback_data": cb} if cb else {"url": url}
    pid = EMO.get(e)
    if pid:
        kw["icon_custom_emoji_id"] = pid     # premium (harakatli) emoji
        return IB(text=label, **kw)
    return IB(text=f"{FB.get(e, '')} {label}".strip(), **kw)


async def ed(msg, *a, **kw):
    try:
        return await msg.edit_text(*a, **kw)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


def M(rows):
    return IM(inline_keyboard=rows)


def money(n):
    return f"{n:,}".replace(",", " ") + " so'm"


# ================== BAZA ==================
db: aiosqlite.Connection


async def fetch(sql, *a):
    cur = await db.execute(sql, a)
    return await cur.fetchall()


async def one(sql, *a):
    r = await fetch(sql, *a)
    return r[0] if r else None


async def run(sql, *a):
    cur = await db.execute(sql, a)
    await db.commit()
    return cur


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, ts INTEGER,
  bal INTEGER DEFAULT 0, refbal INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, chat_id INTEGER,
  title TEXT, link TEXT, acct TEXT, ctype TEXT);
CREATE TABLE IF NOT EXISTS progress(uid INTEGER, kind TEXT, total INTEGER DEFAULT 0, PRIMARY KEY(uid,kind));
CREATE TABLE IF NOT EXISTS invited(chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS links(link TEXT PRIMARY KEY, uid INTEGER, chat_id INTEGER);
CREATE TABLE IF NOT EXISTS posts(chat_id INTEGER, msg_id INTEGER, views INTEGER DEFAULT 0, PRIMARY KEY(chat_id,msg_id));
CREATE TABLE IF NOT EXISTS sent_posts(uid INTEGER, chat_id INTEGER, msg_id INTEGER, PRIMARY KEY(uid,chat_id,msg_id));
CREATE TABLE IF NOT EXISTS refs(new_uid INTEGER PRIMARY KEY, ref_uid INTEGER, ts INTEGER, paid INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS withdraws(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
  src TEXT, status TEXT DEFAULT 'wait', ts INTEGER);
CREATE TABLE IF NOT EXISTS tx(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER,
  kind TEXT, note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

bot: Bot
router = Router()


LOCK = asyncio.Lock()


async def credit(uid, col, amount, kind, note=""):
    """Balansga qo'shish/ayirish + tarixga yozish (LOCK ichida chaqiriladi)."""
    await db.execute(f"UPDATE users SET {col}={col}+? WHERE id=?", (amount, uid))
    await db.execute("INSERT INTO tx(uid,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                     (uid, amount, kind, note, int(time.time())))
    await db.commit()


async def bump(uid, kind, n, unit, reward, note):
    amount = 0
    async with LOCK:
        r = await one("SELECT total FROM progress WHERE uid=? AND kind=?", uid, kind)
        old = r["total"] if r else 0
        new = old + n
        await db.execute("INSERT INTO progress(uid,kind,total) VALUES(?,?,?) "
                         "ON CONFLICT(uid,kind) DO UPDATE SET total=?", (uid, kind, new, new))
        blocks = new // unit - old // unit
        if blocks:
            amount = blocks * reward
            await credit(uid, "bal", amount, kind, f"{new} ta")
        else:
            await db.commit()
    if amount:
        with suppress(Exception):
            await bot.send_message(uid, f"🎉 {note}\n+{money(amount)} balansingizga qo'shildi!")


async def progress_of(uid, kind):
    r = await one("SELECT total FROM progress WHERE uid=? AND kind=?", uid, kind)
    return r["total"] if r else 0


# ================== MAJBURIY OBUNA ==================
async def missing_subs(uid):
    miss = []
    for c in await fetch("SELECT * FROM chats WHERE kind='force'"):
        try:
            m = await bot.get_chat_member(c["chat_id"], uid)
            if m.status in (S.LEFT, S.KICKED):
                miss.append(c)
        except Exception:
            miss.append(c)
    return miss


def sub_kb(miss):
    rows = [[B(c["title"], "go", url=c["link"])] for c in miss]
    rows.append([B("Tekshirish", "chk", cb="chk")])
    return M(rows)


async def ensure_user(u, ref=None):
    new = await one("SELECT id FROM users WHERE id=?", u.id) is None
    if new:
        await run("INSERT INTO users(id,name,ts) VALUES(?,?,?)", u.id, u.full_name, int(time.time()))
        if ref and ref != u.id and await one("SELECT id FROM users WHERE id=?", ref):
            await run("INSERT OR IGNORE INTO refs(new_uid,ref_uid,ts) VALUES(?,?,?)", u.id, ref, int(time.time()))


class SubMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        msg = event if isinstance(event, Message) else event.message
        if u is None or msg is None or msg.chat.type != "private":
            return await handler(event, data)
        ref = None
        if isinstance(event, Message) and event.text and event.text.startswith("/start "):
            p = event.text.split(maxsplit=1)[1]
            ref = int(p) if p.isdigit() else None
        await ensure_user(u, ref)
        skip = u.id in ADMINS or (isinstance(event, CallbackQuery) and event.data == "chk")
        miss = [] if skip else await missing_subs(u.id)
        if miss:
            text = "❗ Botdan foydalanish uchun quyidagi kanal/guruhlarga a'zo bo'ling:"
            if isinstance(event, CallbackQuery):
                await event.answer()
            await msg.answer(text, reply_markup=sub_kb(miss))
            return
        res = await handler(event, data)
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer()
        return res


# ================== ASOSIY MENYU ==================
def main_kb(uid):
    rows = [
        [B("Guruhga odam qo'shish", "group", cb="earn:group")],
        [B("Kanal postlarini tarqatish", "post", cb="earn:post")],
        [B("Akkaunt savdo", "trade", cb="earn:trade")],
        [B("Kanalga odam yig'ish", "drive", cb="earn:drive")],
        [B("Botga odam taklif qilish", "invite", cb="earn:ref")],
        [B("Balans", "bal", cb="bal"), B("Pul yechish", "wd", cb="wd")],
    ]
    if uid in ADMINS:
        rows.append([B("Admin panel", "adm", cb="adm")])
    return M(rows)


MENU_TEXT = "💼 <b>Pul ishlash boti</b>\n\nQaysi usul orqali pul ishlamoqchisiz?"


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(MENU_TEXT, reply_markup=main_kb(m.from_user.id))


@router.callback_query(F.data == "chk")
async def chk(c: CallbackQuery):
    miss = await missing_subs(c.from_user.id)
    if miss:
        return await c.answer("Hali hammasiga a'zo bo'lmadingiz!", show_alert=True)
    await c.message.answer(MENU_TEXT, reply_markup=main_kb(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await ed(c.message, MENU_TEXT, reply_markup=main_kb(c.from_user.id))


def back(to="menu"):
    return [B("Orqaga", "back", cb=to)]


# ---------- 1) Guruhga odam qo'shish ----------
@router.callback_query(F.data == "earn:group")
async def earn_group(c: CallbackQuery):
    chats = await fetch("SELECT * FROM chats WHERE kind='group'")
    total = await progress_of(c.from_user.id, "group")
    text = (f"👥 <b>Guruhga odam qo'shish</b>\n\nHar {GROUP_UNIT} ta qo'shgan odam uchun "
            f"<b>{money(GROUP_REWARD)}</b>.\nSiz qo'shgan: <b>{total}</b> ta "
            f"(keyingi mukofotgacha {GROUP_UNIT - total % GROUP_UNIT} ta)\n\n"
            "Quyidagi guruhlardan biriga kirib, odam qo'shing (odamni aynan o'zingiz qo'shishingiz kerak):")
    if not chats:
        text += "\n\n<i>Hozircha guruhlar yo'q.</i>"
    rows = [[B(ch["title"], "go", url=ch["link"])] for ch in chats] + [back()]
    await ed(c.message, text, reply_markup=M(rows))


# ---------- 2) Post tarqatish ----------
@router.callback_query(F.data == "earn:post")
async def earn_post(c: CallbackQuery):
    chats = await fetch("SELECT * FROM chats WHERE kind='post'")
    sent = await progress_of(c.from_user.id, "post")
    text = (f"📣 <b>Kanal postlarini tarqatish</b>\n\nHar {POST_UNIT} ta post uchun <b>{money(POST_REWARD)}</b>.\n"
            f"Olingan postlar: <b>{sent}</b> ta\n\nKanalni tanlang, bot sizga eng kam ko'rilgan postlarni "
            "forward qiladi. Ularni guruh va kanallarga tarqating.")
    rows = [[B(ch["title"], "get", cb=f"getpost:{ch['id']}")] for ch in chats] + [back()]
    await ed(c.message, text, reply_markup=M(rows))


@router.callback_query(F.data.startswith("getpost:"))
async def getpost(c: CallbackQuery):
    ch = await one("SELECT * FROM chats WHERE id=?", int(c.data.split(":")[1]))
    if not ch:
        return await c.answer("Topilmadi", show_alert=True)
    uid = c.from_user.id
    await sync_views(ch["chat_id"])
    unsent = await fetch(
        "SELECT p.msg_id, p.views FROM posts p WHERE p.chat_id=? AND NOT EXISTS("
        "SELECT 1 FROM sent_posts s WHERE s.uid=? AND s.chat_id=p.chat_id AND s.msg_id=p.msg_id) "
        "ORDER BY p.views ASC", ch["chat_id"], uid)
    if not unsent:
        return await c.answer("Hozircha yangi post yo'q. Keyinroq qaytib ko'ring.", show_alert=True)
    thr = (unsent[0]["views"] // 100 + 1) * 100        # 100 -> 200 -> 300 ...
    batch = [p for p in unsent if p["views"] < thr][:POST_UNIT]
    await c.answer("Postlar yuborilmoqda...")
    n = 0
    for p in batch:
        try:
            await bot.forward_message(uid, ch["chat_id"], p["msg_id"])
            await run("INSERT OR IGNORE INTO sent_posts VALUES(?,?,?)", uid, ch["chat_id"], p["msg_id"])
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            continue
    await bump(uid, "post", n, POST_UNIT, POST_REWARD, f"{n} ta post olindi.")
    await bot.send_message(uid, f"✅ {n} ta post yuborildi (ko'rishlar &lt; {thr}). Tarqating!",
                           reply_markup=M([back("earn:post")]))


# ---------- 3) Akkaunt savdo ----------
@router.callback_query(F.data == "earn:trade")
async def earn_trade(c: CallbackQuery):
    rows = [[B(a, "trade", cb=f"acct:{i}")] for i, a in enumerate(ACCTS)] + [back()]
    await ed(c.message, "🛒 <b>Akkaunt savdo</b>\n\nQaysi turdagi akkauntni sotmoqchi yoki "
                              "sotib olmoqchisiz?", reply_markup=M(rows))


@router.callback_query(F.data.startswith("acct:"))
async def acct(c: CallbackQuery):
    a = ACCTS[int(c.data.split(":")[1])]
    chats = await fetch("SELECT * FROM chats WHERE kind='trade' AND (acct=? OR acct='all')", a)
    text = f"🛒 <b>{a}</b>\n\n📢 Kanalda sotmoqchi bo'lsangiz — @{CONTACT} ga murojaat qiling.\n" \
           "👥 Guruhda e'lon bermoqchi bo'lsangiz — guruhga kirib e'lon qoldiring."
    rows = []
    for ch in chats:
        icon = "Kanal" if ch["ctype"] == "channel" else "Guruh"
        rows.append([B(f"{icon}: {ch['title']}", "go", url=ch["link"])])
    if any(ch["ctype"] == "channel" for ch in chats) or not chats:
        rows.append([B(f"@{CONTACT} ga yozish", "go", url=f"https://t.me/{CONTACT}")])
    rows.append(back("earn:trade"))
    await ed(c.message, text, reply_markup=M(rows))


# ---------- 4) Kanalga odam yig'ish ----------
@router.callback_query(F.data == "earn:drive")
async def earn_drive(c: CallbackQuery):
    chats = await fetch("SELECT * FROM chats WHERE kind='drive'")
    total = await progress_of(c.from_user.id, "drive")
    text = (f"📈 <b>Kanalga odam yig'ish</b>\n\nHar {DRIVE_UNIT} ta odam uchun <b>{money(DRIVE_REWARD)}</b> "
            f"(minimal {DRIVE_UNIT} ta).\nSiz qo'shgan: <b>{total}</b> ta\n\n"
            "Kanalni tanlang — bot sizga maxsus havola yaratadi:")
    rows = [[B(ch["title"], "drive", cb=f"drv:{ch['id']}")] for ch in chats] + [back()]
    await ed(c.message, text, reply_markup=M(rows))


@router.callback_query(F.data.startswith("drv:"))
async def drv(c: CallbackQuery):
    ch = await one("SELECT * FROM chats WHERE id=?", int(c.data.split(":")[1]))
    uid = c.from_user.id
    r = await one("SELECT link FROM links WHERE uid=? AND chat_id=?", uid, ch["chat_id"])
    if r:
        link = r["link"]
    else:
        il = await bot.create_chat_invite_link(ch["chat_id"], name=f"u{uid}"[:32])
        link = il.invite_link
        await run("INSERT INTO links VALUES(?,?,?)", link, uid, ch["chat_id"])
    total = await progress_of(uid, "drive")
    await ed(c.message, 
        f"📈 <b>{html.escape(ch['title'])}</b>\n\nSizning maxsus havolangiz:\n{link}\n\n"
        f"Hozircha qo'shilgan: <b>{total}</b> ta\nHavolani tarqating — bot avtomatik hisoblaydi.",
        reply_markup=M([back("earn:drive")]))


# ---------- 5) Botga taklif ----------
@router.callback_query(F.data == "earn:ref")
async def earn_ref(c: CallbackQuery):
    me = await bot.get_me()
    uid = c.from_user.id
    ok = (await one("SELECT COUNT(*) n FROM refs WHERE ref_uid=? AND paid=1", uid))["n"]
    wait = (await one("SELECT COUNT(*) n FROM refs WHERE ref_uid=? AND paid=0", uid))["n"]
    await ed(c.message, 
        f"🔗 <b>Botga odam taklif qilish</b>\n\nHar bir do'st uchun <b>{money(REF_REWARD)}</b> — "
        "agar u 24 soat botdan chiqmasa va majburiy kanallarni tark etmasa.\n\n"
        f"Havolangiz:\nhttps://t.me/{me.username}?start={uid}\n\n"
        f"✅ Hisoblangan: {ok} ta\n⏳ 24 soat kutilmoqda: {wait} ta",
        reply_markup=M([back()]))


async def referral_loop():
    while True:
        try:
            due = await fetch("SELECT * FROM refs WHERE paid=0 AND ts<?", int(time.time()) - REF_WAIT)
            for r in due:
                good = not await missing_subs(r["new_uid"])
                if good:
                    try:
                        await bot.send_chat_action(r["new_uid"], "typing")
                    except TelegramForbiddenError:
                        good = False
                    except Exception:
                        pass
                await run("UPDATE refs SET paid=? WHERE new_uid=?", 1 if good else 2, r["new_uid"])
                if good:
                    async with LOCK:
                        await credit(r["ref_uid"], "refbal", REF_REWARD, "ref", f"do'st {r['new_uid']}")
                    with suppress(Exception):
                        await bot.send_message(r["ref_uid"], f"🎉 Do'stingiz 24 soat qoldi: +{money(REF_REWARD)}")
        except Exception:
            logging.exception("referral_loop")
        await asyncio.sleep(600)


# ================== BALANS / YECHISH ==================
@router.callback_query(F.data == "bal")
async def bal(c: CallbackQuery):
    uid = c.from_user.id
    u = await one("SELECT * FROM users WHERE id=?", uid)
    g, p, d = [await progress_of(uid, k) for k in ("group", "post", "drive")]
    rok = (await one("SELECT COUNT(*) n FROM refs WHERE ref_uid=? AND paid=1", uid))["n"]
    rwait = (await one("SELECT COUNT(*) n FROM refs WHERE ref_uid=? AND paid=0", uid))["n"]
    hist = await fetch("SELECT amount,kind FROM tx WHERE uid=? ORDER BY id DESC LIMIT 5", uid)
    names = {"group": "Guruh", "post": "Post", "drive": "Kanal", "ref": "Taklif", "withdraw": "Yechish",
             "refund": "Qaytarildi", "admin": "Admin"}
    h = "\n".join(f"{'+' if t['amount'] > 0 else ''}{money(t['amount'])} — {names.get(t['kind'], t['kind'])}"
                  for t in hist) or "—"
    await ed(c.message,
             f"💰 <b>Balans</b>\n\nAsosiy: <b>{money(u['bal'])}</b>\nTaklif balansi: <b>{money(u['refbal'])}</b>\n"
             f"Jami: <b>{money(u['bal'] + u['refbal'])}</b>\n\n"
             f"📊 <b>Jarayon</b>\n👥 Guruh: {g % GROUP_UNIT}/{GROUP_UNIT} (jami {g})\n"
             f"📣 Post: {p % POST_UNIT}/{POST_UNIT} (jami {p})\n📈 Kanal: {d % DRIVE_UNIT}/{DRIVE_UNIT} (jami {d})\n"
             f"🔗 Taklif: {rok} ta hisoblangan, {rwait} ta kutilmoqda\n\n"
             f"🧾 <b>Oxirgi amallar</b>\n{h}\n\n"
             f"Minimal yechish: {money(MIN_WD)} (taklif balansi: {money(MIN_WD_REF)})",
             reply_markup=M([back()]))


@router.callback_query(F.data == "wd")
async def wd(c: CallbackQuery):
    u = await one("SELECT * FROM users WHERE id=?", c.from_user.id)
    rows = []
    if u["bal"] >= MIN_WD:
        rows.append([B(f"Asosiy: {money(u['bal'])}", "wd", cb="wdo:main")])
    if u["refbal"] >= MIN_WD_REF:
        rows.append([B(f"Taklif: {money(u['refbal'])}", "wd", cb="wdo:ref")])
    if not rows:
        return await c.answer(f"Yechish uchun asosiy balans kamida {money(MIN_WD)}, "
                              f"taklif balansi kamida {money(MIN_WD_REF)} bo'lishi kerak.", show_alert=True)
    rows.append(back())
    await ed(c.message, "💸 Qaysi balansdan yechasiz?", reply_markup=M(rows))


@router.callback_query(F.data.startswith("wdo:"))
async def wdo(c: CallbackQuery):
    src = c.data.split(":")[1]
    col, mn = ("bal", MIN_WD) if src == "main" else ("refbal", MIN_WD_REF)
    uid = c.from_user.id
    async with LOCK:
        u = await one("SELECT * FROM users WHERE id=?", uid)
        amount = u[col]
        if amount < mn:
            return await c.answer("Balans yetarli emas", show_alert=True)
        await db.execute(f"UPDATE users SET {col}=0 WHERE id=?", (uid,))
        cur = await db.execute("INSERT INTO withdraws(uid,amount,src,ts) VALUES(?,?,?,?)",
                               (uid, amount, src, int(time.time())))
        wid = cur.lastrowid
        await db.execute("INSERT INTO tx(uid,amount,kind,note,ts) VALUES(?,?,?,?,?)",
                         (uid, -amount, "withdraw", f"#{wid}", int(time.time())))
        await db.commit()
    await ed(c.message, f"✅ So'rov yuborildi: <b>{money(amount)}</b>\n\n"
                        f"Pulni olish uchun @{CONTACT} ga yozing va so'rov raqamini ayting: <b>#{wid}</b>",
             reply_markup=M([[B(f"@{CONTACT} ga yozish", "go", url=f"https://t.me/{CONTACT}")], back()]))
    for a in ADMINS:
        with suppress(Exception):
            await bot.send_message(
                a, f"💸 So'rov #{wid}\nFoydalanuvchi: <a href='tg://user?id={uid}'>{html.escape(c.from_user.full_name)}</a>"
                   f" (<code>{uid}</code>)\nSumma: {money(amount)} ({src})",
                reply_markup=M([[B("To'landi", "ok", cb=f"wda:{wid}"), B("Rad etish", "no", cb=f"wdr:{wid}")]]))


@router.callback_query(F.data.startswith(("wda:", "wdr:")))
async def wd_admin(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    act, wid = c.data.split(":")
    async with LOCK:
        w = await one("SELECT * FROM withdraws WHERE id=?", int(wid))
        if not w or w["status"] != "wait":
            return await c.answer("Allaqachon ko'rilgan", show_alert=True)
        if act == "wda":
            await db.execute("UPDATE withdraws SET status='paid' WHERE id=?", (w["id"],))
            await db.commit()
        else:
            await db.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (w["id"],))
            await credit(w["uid"], "bal" if w["src"] == "main" else "refbal", w["amount"], "refund", f"#{w['id']}")
    if act == "wda":
        await bot.send_message(w["uid"], f"✅ #{w['id']} so'rovingiz bo'yicha {money(w['amount'])} to'landi.")
    else:
        await bot.send_message(w["uid"], f"❌ #{w['id']} so'rov rad etildi, pul balansga qaytarildi.")
    await ed(c.message, c.message.html_text + f"\n\n<b>{'TO`LANDI' if act == 'wda' else 'RAD ETILDI'}</b>")


# ================== ADMIN PANEL ==================
KINDS = {  # kalit: (sarlavha, emoji kaliti)
    "group": ("Guruhga odam qo'shish", "group"),
    "post": ("Kanal post", "post"),
    "trade": ("Akkaunt savdo", "trade"),
    "drive": ("Kanalga odam yig'ish", "drive"),
    "force": ("Majburiy obuna", "force"),
}


class AddChat(StatesGroup):
    wait = State()
    acct = State()


@router.callback_query(F.data == "adm")
async def adm(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    await state.clear()
    rows = [[B(t, e, cb=f"adk:{k}")] for k, (t, e) in KINDS.items()]
    rows.append([B("Statistika", "stat", cb="adstat")])
    rows.append(back())
    await ed(c.message, "⚙️ <b>Admin panel</b>", reply_markup=M(rows))


@router.callback_query(F.data == "adstat")
async def adstat(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    n = (await one("SELECT COUNT(*) n FROM users"))["n"]
    wt = (await one("SELECT COUNT(*) n FROM withdraws WHERE status='wait'"))["n"]
    await ed(c.message, f"📊 Foydalanuvchilar: <b>{n}</b>\nKutilayotgan yechishlar: <b>{wt}</b>",
                              reply_markup=M([back("adm")]))


@router.callback_query(F.data.startswith("adk:"))
async def adk(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    kind = c.data.split(":")[1]
    await state.clear()
    chats = await fetch("SELECT * FROM chats WHERE kind=?", kind)
    rows = [[B(ch["title"] + (f" [{ch['acct']}]" if ch["acct"] else ""), "del", cb=f"addel:{ch['id']}:{kind}")]
            for ch in chats]
    rows.append([B("Qo'shish", "add", cb=f"adadd:{kind}")])
    rows.append(back("adm"))
    await ed(c.message, f"<b>{KINDS[kind][0]}</b>\n\nRo'yxat (o'chirish uchun bosing):", reply_markup=M(rows))


@router.callback_query(F.data.startswith("addel:"))
async def addel(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    _, cid, kind = c.data.split(":")
    await run("DELETE FROM chats WHERE id=?", int(cid))
    await c.answer("O'chirildi")
    chats = await fetch("SELECT * FROM chats WHERE kind=?", kind)
    rows = [[B(ch["title"], "del", cb=f"addel:{ch['id']}:{kind}")] for ch in chats]
    rows += [[B("Qo'shish", "add", cb=f"adadd:{kind}")], back("adm")]
    await ed(c.message, f"<b>{KINDS[kind][0]}</b>", reply_markup=M(rows))


@router.callback_query(F.data.startswith("adadd:"))
async def adadd(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMINS:
        return await c.answer()
    await state.set_state(AddChat.wait)
    await state.update_data(kind=c.data.split(":")[1])
    await ed(c.message, "Kanal/guruhdan biror xabarni <b>forward</b> qiling yoki @username / ID yuboring.\n"
                              "⚠️ Bot o'sha yerda <b>admin</b> bo'lishi shart.", reply_markup=M([back("adm")]))


@router.message(AddChat.wait)
async def addchat_get(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return
    try:
        fo = m.forward_origin
        fchat = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None) if fo else None
        if fchat:
            chat = await bot.get_chat(fchat.id)
        else:
            t = (m.text or "").strip()
            chat = await bot.get_chat(int(t) if t.lstrip("-").isdigit() else t)
        me = await bot.get_me()
        st = (await bot.get_chat_member(chat.id, me.id)).status
        if st not in (S.ADMINISTRATOR, S.CREATOR):
            return await m.answer("❌ Bot bu yerda admin emas. Admin qilib, qayta yuboring.")
        link = f"https://t.me/{chat.username}" if chat.username else (
            chat.invite_link or await bot.export_chat_invite_link(chat.id))
    except Exception as e:
        return await m.answer(f"❌ Topilmadi yoki xato: {e}")
    d = await state.get_data()
    ctype = "channel" if chat.type == "channel" else "group"
    await state.update_data(chat_id=chat.id, title=chat.title, link=link, ctype=ctype)
    if d["kind"] == "trade":
        rows = [[B(a, "trade", cb=f"adacct:{i}")] for i, a in enumerate(ACCTS)]
        rows.append([B("Hammasi uchun", "ok", cb="adacct:all")])
        await state.set_state(AddChat.acct)
        return await m.answer("Bu qaysi akkaunt turi uchun?", reply_markup=M(rows))
    await save_chat(m, state, None)


@router.callback_query(AddChat.acct, F.data.startswith("adacct:"))
async def addchat_acct(c: CallbackQuery, state: FSMContext):
    v = c.data.split(":")[1]
    await save_chat(c.message, state, "all" if v == "all" else ACCTS[int(v)])
    await c.answer()


async def save_chat(m: Message, state: FSMContext, acct):
    d = await state.get_data()
    await run("INSERT INTO chats(kind,chat_id,title,link,acct,ctype) VALUES(?,?,?,?,?,?)",
              d["kind"], d["chat_id"], d["title"], d["link"], acct, d["ctype"])
    await state.clear()
    await m.answer(f"✅ Qo'shildi: <b>{html.escape(d['title'])}</b>", reply_markup=M([back("adm")]))


# ---- premium emoji sozlash (admin) ----
@router.message(Command("emojiid"))
async def emojiid_hint(m: Message):
    if m.from_user.id in ADMINS:
        await m.answer("Premium emoji yuboring — men uning ID sini aytaman.\n"
                       f"Keyin: <code>/setemoji kalit ID</code>\nKalitlar: {', '.join(FB)}")


@router.message(Command("setemoji"))
async def setemoji(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS or not command.args:
        return
    try:
        key, pid = command.args.split()
        assert key in FB
    except Exception:
        return await m.answer("Format: /setemoji kalit ID")
    EMO[key] = pid
    await run("INSERT INTO settings VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", f"emo:{key}", pid, pid)
    await m.answer(f"✅ {key} → {pid}")


@router.message(Command("addbal"))
async def addbal(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS or not command.args:
        return
    try:
        p = command.args.split()
        uid, amount = int(p[0]), int(p[1])
        col = "refbal" if len(p) > 2 and p[2] == "ref" else "bal"
        async with LOCK:
            await credit(uid, col, amount, "admin", f"by {m.from_user.id}")
        await m.answer("✅ Bajarildi")
    except Exception:
        await m.answer("Format: /addbal USER_ID SUMMA [ref]")


@router.message(Command("info"))
async def info(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS or not command.args or not command.args.strip().isdigit():
        return
    uid = int(command.args)
    u = await one("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return await m.answer("Topilmadi")
    g, p, d = [await progress_of(uid, k) for k in ("group", "post", "drive")]
    await m.answer(f"{html.escape(u['name'] or '')} <code>{uid}</code>\nAsosiy: {money(u['bal'])}\n"
                   f"Taklif: {money(u['refbal'])}\nGuruh {g}, Post {p}, Kanal {d}")


# ================== KUZATUV (chat_member, channel_post) ==================
@router.chat_member()
async def on_member(u: ChatMemberUpdated):
    if not (u.old_chat_member.status in (S.LEFT, S.KICKED) and
            u.new_chat_member.status in (S.MEMBER, S.RESTRICTED)):
        return
    joined = u.new_chat_member.user
    if joined.is_bot:
        return
    for ch in await fetch("SELECT * FROM chats WHERE chat_id=? AND kind IN ('group','drive')", u.chat.id):
        if ch["kind"] == "group":
            adder = u.from_user.id
            if adder == joined.id or not await one("SELECT id FROM users WHERE id=?", adder):
                continue
            cur = await run("INSERT OR IGNORE INTO invited VALUES(?,?)", u.chat.id, joined.id)
            if cur.rowcount:
                await bump(adder, "group", 1, GROUP_UNIT, GROUP_REWARD, f"{GROUP_UNIT} ta odam qo'shdingiz.")
        else:
            if not u.invite_link:
                continue
            r = await one("SELECT uid FROM links WHERE link=?", u.invite_link.invite_link)
            if not r or r["uid"] == joined.id:
                continue
            cur = await run("INSERT OR IGNORE INTO invited VALUES(?,?)", u.chat.id, joined.id)
            if cur.rowcount:
                await bump(r["uid"], "drive", 1, DRIVE_UNIT, DRIVE_REWARD, f"Kanalga {DRIVE_UNIT} ta odam qo'shildi.")


@router.channel_post()
async def on_post(m: Message):
    await run("INSERT OR IGNORE INTO posts(chat_id,msg_id,views) VALUES(?,?,0)", m.chat.id, m.message_id)


# ================== TELETHON (ko'rishlar soni) ==================
tg = None


async def start_telethon():
    global tg
    if not (API_ID and API_HASH and SESSION):
        logging.warning("Telethon sozlanmagan: post ko'rishlarini bilib bo'lmaydi.")
        return
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    tg = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await tg.start()
    await tg.get_dialogs()


LAST_SYNC: dict = {}


async def sync_views(chat_id, force=False):
    if not tg:
        return
    if not force and time.time() - LAST_SYNC.get(chat_id, 0) < 60:
        return
    LAST_SYNC[chat_id] = time.time()
    try:
        async for msg in tg.iter_messages(chat_id, limit=500):
            if msg.id and not msg.action:
                await db.execute("INSERT INTO posts(chat_id,msg_id,views) VALUES(?,?,?) "
                                 "ON CONFLICT(chat_id,msg_id) DO UPDATE SET views=?",
                                 (chat_id, msg.id, msg.views or 0, msg.views or 0))
        await db.commit()
    except Exception:
        logging.exception("sync_views")


async def views_loop():
    while True:
        for ch in await fetch("SELECT chat_id FROM chats WHERE kind='post'"):
            await sync_views(ch["chat_id"], force=True)
        await asyncio.sleep(900)


@router.message(F.entities, F.from_user.id.in_(ADMINS), F.chat.type == "private")
async def emoji_ids(m: Message):
    ids = [e.custom_emoji_id for e in m.entities if e.type == "custom_emoji"]
    if ids:
        await m.answer("\n".join(f"<code>{i}</code>" for i in ids))



# ================== ISHGA TUSHIRISH ==================
async def health(_):
    return web.Response(text="ok")


async def main():
    global db, bot
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.executescript(SCHEMA)
    for r in await fetch("SELECT k,v FROM settings WHERE k LIKE 'emo:%'"):
        EMO[r["k"][4:]] = r["v"]

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(SubMW())
    dp.callback_query.middleware(SubMW())
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await start_telethon()
    asyncio.create_task(referral_loop())
    asyncio.create_task(views_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
