import os, sqlite3, datetime as dt, threading, time
from dateutil import tz
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# === ENV ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "7539551272:AAEM1etW4CGIveFZMNpn_v29NrQe9nTFFRw")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "176867232").split(",") if x.strip().isdigit()}
TZ = tz.gettz(os.getenv("TZ", "Europe/Moscow"))
DB = os.getenv("DB_PATH", "./sportsbot.db")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in .env")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# === DB ===
def db():
    return sqlite3.connect(DB, check_same_thread=False)

def ensure_schema():
    with db() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emoji TEXT,
                title TEXT NOT NULL,
                date_start TEXT NOT NULL,         -- YYYY-MM-DD (локальная)
                date_end TEXT NOT NULL,           -- YYYY-MM-DD (локальная)
                location TEXT,
                capacity INTEGER,
                description TEXT,
                rewards TEXT,
                report_required INTEGER DEFAULT 0,
                report_schedule TEXT DEFAULT 'none',   -- none|daily|final
                report_unit TEXT,
                report_photo_required INTEGER DEFAULT 0, -- 0=не обяз., 1=обяз.
                is_active INTEGER DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                tg_user_id INTEGER NOT NULL,
                tg_username TEXT,
                tg_name TEXT,
                signed_at TEXT NOT NULL,          -- UTC ISO
                UNIQUE(event_id, tg_user_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                tg_user_id INTEGER NOT NULL,
                date TEXT NOT NULL,               -- YYYY-MM-DD (локальная дата)
                value REAL,
                text TEXT,
                photos TEXT,                      -- одиночный file_id (строка)
                created_at TEXT NOT NULL,         -- UTC ISO
                UNIQUE(event_id, tg_user_id, date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications_sent(
                event_id INTEGER NOT NULL,
                tg_user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,               -- start|start-2|report-daily|report-final
                sent_at TEXT NOT NULL,
                PRIMARY KEY (event_id, tg_user_id, kind)
            )
        """)
        # лёгкая миграция на случай старой БД
        cur.execute("PRAGMA table_info(events)")
        cols = {row[1] for row in cur.fetchall()}
        alters = []
        if "report_required" not in cols: alters.append("ALTER TABLE events ADD COLUMN report_required INTEGER DEFAULT 0")
        if "report_schedule" not in cols: alters.append("ALTER TABLE events ADD COLUMN report_schedule TEXT DEFAULT 'none'")
        if "report_unit" not in cols: alters.append("ALTER TABLE events ADD COLUMN report_unit TEXT")
        if "report_photo_required" not in cols: alters.append("ALTER TABLE events ADD COLUMN report_photo_required INTEGER DEFAULT 0")
        for sql in alters:
            cur.execute(sql)
        con.commit()

ensure_schema()

# === Utils ===
RU_MONTHS_GEN = {
    1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
    7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря"
}
def local_today() -> dt.date:
    return dt.datetime.now(TZ).date()
def today_str() -> str:
    return local_today().strftime("%Y-%m-%d")
def parse_date(s: str) -> dt.date:
    s = s.strip()
    try:
        if "-" in s:
            return dt.datetime.strptime(s, "%Y-%m-%d").date()
        return dt.datetime.strptime(s, "%d.%m.%Y").date()
    except:
        raise ValueError("Неверный формат даты. Используй YYYY-MM-DD или DD.MM.YYYY")
def is_date_like(s: str) -> bool:
    try:
        parse_date(s); return True
    except:
        return False
def ru_date(d: dt.date) -> str:
    return f"{d.day} {RU_MONTHS_GEN[d.month]} {d.year}"
def ru_range(d1: dt.date, d2: dt.date) -> str:
    if d1 == d2: return ru_date(d1)
    if d1.year == d2.year:
        if d1.month == d2.month:
            return f"{d1.day}–{d2.day} {RU_MONTHS_GEN[d1.month]} {d1.year}"
        return f"{d1.day} {RU_MONTHS_GEN[d1.month]} — {d2.day} {RU_MONTHS_GEN[d2.month]} {d1.year}"
    return f"{ru_date(d1)} — {ru_date(d2)}"
def status_for(d1: dt.date, d2: dt.date, today: dt.date) -> str:
    if d1 <= today <= d2: return "идёт сейчас"
    if today < d1: return "ещё не началось"
    return "завершено"

WELCOME_TEXT = (
    "Привет!\n\n"
    "Я бот, который расскажет тебе о <b>спортивных активностях</b>, доступных в ПравоТех 🚴‍♀️\n\n"
    "Я также помогу тебе <b>записаться</b> на понравившуюся активность. "
    "И также при помощи меня ты сможешь <b>фиксировать результаты</b>.\n\n"
    "<i>Выбирай кнопку в меню ниже 👇🏻</i>"
)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def fmt_event_row(row) -> str:
    # (id,emoji,title,date_start,date_end,location,capacity,description,rewards,
    #  rep_req,rep_sched,rep_unit,rep_photo,is_active)
    (eid, emoji, title, ds, de, loc, cap, desc, rew,
     rep_req, rep_sched, rep_unit, rep_photo, _) = row
    d1 = dt.datetime.strptime(ds, "%Y-%m-%d").date()
    d2 = dt.datetime.strptime(de, "%Y-%m-%d").date()
    emj = (emoji or "🏅").strip() or "🏅"
    extras = []
    if rep_req:
        s = "ежедневные" if rep_sched == "daily" else "в финале"
        if rep_unit: s += f" ({rep_unit})"
        extras.append(f"Отчёты: {s}")
        if rep_photo: extras.append("Фото-пруф обязателен")
    lines = [
        f"{emj} <b>{title}</b> | {ru_range(d1,d2)} | {status_for(d1,d2,local_today())}",
        f"Описание: {desc or '—'}",
        f"Награды: {rew or '—'}"
    ]
    if extras:
        lines.append(" · ".join(extras))
    return "\n".join(lines)

# === Keyboards ===
def main_menu_kb(is_admin_flag: bool):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🏅 События"), KeyboardButton("📝 Мои регистрации"))
    if is_admin_flag:
        kb.add(KeyboardButton("➕ Добавить событие"))
    return kb

def event_keyboard(event_id: int, user_id: int):
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM signups WHERE event_id=?", (event_id,))
        taken = cur.fetchone()[0]
        cur.execute("SELECT capacity, report_required FROM events WHERE id=?", (event_id,))
        cap, rep_req = cur.fetchone()
        cur.execute("SELECT 1 FROM signups WHERE event_id=? AND tg_user_id=?", (event_id, user_id))
        already = cur.fetchone() is not None
    kb = InlineKeyboardMarkup()
    if already:
        kb.add(InlineKeyboardButton("❌ Отписаться", callback_data=f"leave:{event_id}"))
        if rep_req:
            kb.add(InlineKeyboardButton("📥 Отчёт", callback_data=f"report:{event_id}"))
    else:
        if cap is None or taken < cap:
            kb.add(InlineKeyboardButton("✍️ Записаться", callback_data=f"join:{event_id}"))
        else:
            kb.add(InlineKeyboardButton("Список заполнен", callback_data="noop"))
    kb.add(InlineKeyboardButton("🏆 Рейтинг", callback_data=f"lb:{event_id}"))
    if is_admin(user_id):
        kb.add(InlineKeyboardButton("🛠 Редактировать", callback_data=f"edit:{event_id}"))
        kb.add(InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{event_id}"))
        kb.add(InlineKeyboardButton("👥 Участники", callback_data=f"plist:{event_id}"))
    return kb

# === State ===
STATE = {}  # user_id -> dict
def reset_state(uid): STATE.pop(uid, None)

# === Start & menu
@bot.message_handler(commands=["start","help"])
def start_cmd(m):
    bot.send_message(m.chat.id, WELCOME_TEXT, reply_markup=main_menu_kb(is_admin(m.from_user.id)))

@bot.message_handler(func=lambda m: m.text == "🏅 События")
def btn_events(m): list_events(m)

@bot.message_handler(func=lambda m: m.text == "📝 Мои регистрации")
def btn_my(m): my_signups(m)

# === Create wizard (admin)
@bot.message_handler(func=lambda m: m.text == "➕ Добавить событие")
def add_wizard_start(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "Эта кнопка только для админов.")
        return
    STATE[m.from_user.id] = {"mode": "addevent", "step": 1, "data": {}}
    bot.send_message(m.chat.id, "Шаг 1/11 — введи эмодзи (или «-»):")

@bot.message_handler(func=lambda m: m.from_user.id in STATE and STATE[m.from_user.id].get("mode")=="addevent")
def add_wizard_flow(m):
    uid=m.from_user.id; st=STATE[uid]; step=st["step"]; d=st["data"]; txt=(m.text or "").strip()
    if txt.lower() in {"отмена","/cancel","cancel"}:
        reset_state(uid); bot.reply_to(m,"Ок, отменяю."); return
    try:
        if step==1:
            d["emoji"] = "" if txt in {"-","—",""} else txt
            st["step"]=2; bot.reply_to(m,"Шаг 2/11 — введи <b>название</b>:")
        elif step==2:
            d["title"]=txt
            st["step"]=3; bot.reply_to(m,"Шаг 3/11 — введи <b>дату начала</b> (YYYY-MM-DD или DD.MM.YYYY):")
        elif step==3:
            d["date_start"]=parse_date(txt).strftime("%Y-%m-%d")
            st["step"]=4; bot.reply_to(m,"Шаг 4/11 — введи <b>дату окончания</b>:")
        elif step==4:
            d_end=parse_date(txt)
            d_start=dt.datetime.strptime(d["date_start"], "%Y-%m-%d").date()
            if d_end < d_start:
                bot.reply_to(m,"Окончание раньше начала. Попробуй ещё раз."); return
            d["date_end"]=d_end.strftime("%Y-%m-%d")
            st["step"]=5; bot.reply_to(m,"Шаг 5/11 — введи <b>лимит мест</b> или «без лимита»:")
        elif step==5:
            low=txt.lower()
            d["capacity"] = None if low in {"без лимита","безлимита","нет","-",""} else int(txt)
            st["step"]=6; bot.reply_to(m,"Шаг 6/11 — введи <b>локацию</b> (или «-»):")
        elif step==6:
            d["location"] = "" if txt in {"-","—",""} else txt
            st["step"]=7; bot.reply_to(m,"Шаг 7/11 — введи <b>описание</b> (или «-»):")
        elif step==7:
            d["description"] = "" if txt in {"-","—",""} else txt
            st["step"]=8; bot.reply_to(m,"Шаг 8/11 — введи <b>награды</b> (или «-»):")
        elif step==8:
            d["rewards"] = "" if txt in {"-","—",""} else txt
            st["step"]=9; bot.reply_to(m,"Шаг 9/11 — нужны отчёты? Напиши «да» или «нет».")
        elif step==9:
            d["report_required"] = 1 if txt.lower() in {"да","+","yes","нужны","нужен"} else 0
            if d["report_required"] == 0:
                d["report_schedule"]="none"; d["report_unit"]=""; d["report_photo_required"]=0
                st["step"]=11; bot.reply_to(m,"Шаг 11/11 — напиши «готово», чтобы сохранить.")
            else:
                st["step"]=10; bot.reply_to(m,"Шаг 10/11 — тип отчётов: «ежедневный» или «финальный». Затем укажу единицу и фото-пруф.")
        elif step==10:
            low=txt.lower()
            if low.startswith("ежед") or low=="daily":
                d["report_schedule"]="daily"
            elif low.startswith("фин") or low=="final":
                d["report_schedule"]="final"
            else:
                bot.reply_to(m,"Напиши «ежедневный» или «финальный»."); return
            st["step"]=100; bot.reply_to(m,"Укажи единицу измерения (например: шагов, км) или «-».")
        elif step==100:
            d["report_unit"] = "" if txt in {"-","—",""} else txt
            st["step"]=101; bot.reply_to(m,"Фото-пруф обязателен? Напиши «да» или «нет».")
        elif step==101:
            d["report_photo_required"] = 1 if txt.lower() in {"да","+","yes"} else 0
            st["step"]=11; bot.reply_to(m,"Шаг 11/11 — напиши «готово», чтобы сохранить.")
        elif step==11:
            if txt.lower() not in {"готово","ok","да","save"}:
                bot.reply_to(m,"Если всё верно — напиши «готово»."); return
            with db() as con:
                cur=con.cursor()
                cur.execute("""INSERT INTO events(emoji,title,date_start,date_end,location,capacity,description,rewards,
                              report_required,report_schedule,report_unit,report_photo_required,is_active)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                            (d.get("emoji",""), d["title"], d["date_start"], d["date_end"],
                             d.get("location",""), d.get("capacity"), d.get("description",""), d.get("rewards",""),
                             d.get("report_required",0), d.get("report_schedule","none"),
                             d.get("report_unit",""), d.get("report_photo_required",0)))
                con.commit()
            reset_state(uid)
            bot.reply_to(m,"Событие добавлено ✅ Нажми «🏅 События», чтобы посмотреть.", reply_markup=main_menu_kb(is_admin(uid)))
    except ValueError as e:
        bot.reply_to(m, f"⚠️ {e}")
    except Exception:
        bot.reply_to(m, "Что-то пошло не так. Напиши «Отмена» и начни заново.")

# === Lists
@bot.message_handler(commands=["events"])
def list_events(m):
    with db() as con:
        cur=con.cursor()
        cur.execute("""SELECT id,emoji,title,date_start,date_end,location,capacity,description,rewards,
                              report_required,report_schedule,report_unit,report_photo_required,is_active
                       FROM events
                       WHERE is_active=1 AND date_end>=?
                       ORDER BY date_start ASC
                       LIMIT 50""", (today_str(),))
        rows=cur.fetchall()
    if not rows:
        bot.send_message(m.chat.id, "Сейчас нет активных событий.", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return
    for row in rows:
        bot.send_message(m.chat.id, fmt_event_row(row), reply_markup=event_keyboard(row[0], m.from_user.id))
    try:
        bot.send_message(m.chat.id, "Выбирай действие ниже ⤵️", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
    except:
        pass

@bot.message_handler(commands=["my"])
def my_signups(m):
    with db() as con:
        cur=con.cursor()
        cur.execute("""SELECT e.id,e.emoji,e.title,e.date_start,e.date_end,e.location,e.capacity,e.description,e.rewards,
                              e.report_required,e.report_schedule,e.report_unit,e.report_photo_required,e.is_active
                       FROM signups s JOIN events e ON e.id=s.event_id
                       WHERE s.tg_user_id=? AND e.date_end>=?
                       ORDER BY e.date_start ASC""", (m.from_user.id, today_str()))
        rows=cur.fetchall()
    if not rows:
        bot.send_message(m.chat.id, "У тебя пока нет активных регистраций.", reply_markup=main_menu_kb(is_admin(m.from_user.id)))
        return
    for row in rows:
        bot.send_message(m.chat.id, "🧾 Твоя регистрация\n"+fmt_event_row(row), reply_markup=event_keyboard(row[0], m.from_user.id))

# === One-line add (без отчётных настроек — удобнее мастером)
@bot.message_handler(commands=["addevent"])
def add_event_one_line(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m,"Команда только для админов.")
        return
    try:
        _, payload = m.text.split(" ",1)
        parts = [p.strip() for p in payload.split("|")]
        def extract(ps):
            if len(ps) < 5: raise ValueError("Мало полей.")
            if is_date_like(ps[1]) and is_date_like(ps[2]):
                emoji=""; title=ps[0]; d1=parse_date(ps[1]); d2=parse_date(ps[2]); tail=ps[3:]
            elif (not is_date_like(ps[1])) and is_date_like(ps[2]) and is_date_like(ps[3]):
                emoji=ps[0]; title=ps[1]; d1=parse_date(ps[2]); d2=parse_date(ps[3]); tail=ps[4:]
            elif is_date_like(ps[1]):
                emoji=""; title=ps[0]; d1=parse_date(ps[1]); d2=d1; tail=ps[2:]
            elif is_date_like(ps[2]):
                emoji=ps[0]; title=ps[1]; d1=parse_date(ps[2]); d2=d1; tail=ps[3:]
            else:
                raise ValueError("Не вижу дат.")
            if d2 < d1: raise ValueError("Окончание раньше начала.")
            cap=None; loc=""; desc=""; rew=""
            if len(tail)>=1 and tail[0]!="":
                cap = None if tail[0].lower() in {"без лимита","безлимита","нет","-"} else int(tail[0])
            if len(tail)>=2: loc=tail[1]
            if len(tail)>=3: desc=tail[2]
            if len(tail)>=4: rew=tail[3]
            return (emoji,title,d1.strftime("%Y-%m-%d"),d2.strftime("%Y-%m-%d"),cap,loc,desc,rew)
        emoji,title,ds,de,cap,loc,desc,rew = extract(parts)
    except Exception:
        bot.reply_to(m,"Неверный формат. Лучше пользуйся кнопкой «➕ Добавить событие».")
        return
    with db() as con:
        cur=con.cursor()
        cur.execute("""INSERT INTO events(emoji,title,date_start,date_end,location,capacity,description,rewards,is_active)
                       VALUES(?,?,?,?,?,?,?,?,1)""", (emoji,title,ds,de,loc,cap,desc,rew))
        con.commit()
    bot.reply_to(m,"Готово, добавила событие ✅")

# === Participants (admin)
@bot.message_handler(commands=["participants"])
def participants(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m,"Только для админов."); return
    try:
        _, eid = m.text.split(" ",1); eid=int(eid.strip())
    except:
        bot.reply_to(m,"Укажи ID: /participants 2"); return
    with db() as con:
        cur=con.cursor()
        cur.execute("SELECT title FROM events WHERE id=?", (eid,))
        ev=cur.fetchone()
        if not ev:
            bot.reply_to(m,"Не нашла событие."); return
        title=ev[0]
        cur.execute("SELECT tg_name,tg_username FROM signups WHERE event_id=? ORDER BY signed_at ASC",(eid,))
        rows=cur.fetchall()
    if not rows:
        bot.reply_to(m,f"На «{title}» пока никто не записан."); return
    lines=[f"{i}. {name} {'@'+username if username else ''}".strip() for i,(name,username) in enumerate(rows,1)]
    bot.reply_to(m, f"Участники «{title}»:\n" + "\n".join(lines) + f"\n\nВсего: {len(rows)}")

# === Edit/Delete (admin)
@bot.callback_query_handler(func=lambda c: c.data.startswith("del:"))
def cb_delete_confirm(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id,"Только админ."); return
    _, eid = c.data.split(":"); eid=int(eid)
    kb=InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Удалить", callback_data=f"delok:{eid}"),
           InlineKeyboardButton("↩️ Отмена", callback_data="noop"))
    bot.answer_callback_query(c.id,"Подтверди удаление")
    bot.send_message(c.message.chat.id, f"Точно удалить событие #{eid}? Будут удалены записи и отчёты.", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("delok:"))
def cb_delete_do(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id,"Только админ."); return
    _, eid = c.data.split(":"); eid=int(eid)
    with db() as con:
        cur=con.cursor()
        cur.execute("DELETE FROM reports WHERE event_id=?", (eid,))
        cur.execute("DELETE FROM signups WHERE event_id=?", (eid,))
        cur.execute("DELETE FROM events WHERE id=?", (eid,))
        con.commit()
    bot.answer_callback_query(c.id,"Удалено")
    bot.send_message(c.message.chat.id, f"Событие #{eid} удалено 🗑")

@bot.callback_query_handler(func=lambda c: c.data.startswith("edit:"))
def cb_edit_start(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id,"Только админ."); return
    _, eid = c.data.split(":"); eid=int(eid)
    STATE[c.from_user.id] = {"mode":"edit","event_id":eid,"step":1,"data":{}}
    bot.answer_callback_query(c.id,"Редактирование")
    bot.send_message(c.message.chat.id, "Редактирование: отправь новое <b>название</b> или «-», чтобы оставить прежнее.")

@bot.message_handler(func=lambda m: m.from_user.id in STATE and STATE[m.from_user.id].get("mode")=="edit")
def edit_flow(m):
    uid=m.from_user.id; st=STATE[uid]; step=st["step"]; eid=st["event_id"]; txt=(m.text or "").strip()
    if txt.lower() in {"отмена","/cancel","cancel"}:
        reset_state(uid); bot.reply_to(m,"Ок, отменяю."); return
    with db() as con:
        cur=con.cursor()
        cur.execute("""SELECT title,date_start,date_end,location,capacity,description,rewards,
                              report_required,report_schedule,report_unit,report_photo_required
                       FROM events WHERE id=?""", (eid,))
        row=cur.fetchone()
        if not row:
            reset_state(uid); bot.reply_to(m,"Не нашла событие."); return
        title, ds, de, loc, cap, desc, rew, rep_req, rep_sched, rep_unit, rep_photo = row
    try:
        if step==1:
            st["data"]["title"] = title if txt in {"-","—",""} else txt
            st["step"]=2; bot.reply_to(m,f"Текущая дата начала: {ds}. Введи новую или «-».")
        elif step==2:
            new_ds = ds if txt in {"-","—",""} else parse_date(txt).strftime("%Y-%m-%d")
            st["data"]["date_start"]=new_ds
            st["step"]=3; bot.reply_to(m,f"Текущая дата окончания: {de}. Введи новую или «-».")
        elif step==3:
            new_de = de if txt in {"-","—",""} else parse_date(txt).strftime("%Y-%m-%d")
            if new_de < st["data"]["date_start"]:
                bot.reply_to(m,"Окончание раньше начала. Введи корректную."); return
            st["data"]["date_end"]=new_de
            st["step"]=4; bot.reply_to(m,f"Текущая локация: {loc or '—'}. Введи новую или «-».")
        elif step==4:
            st["data"]["location"] = loc if txt in {"-","—",""} else ("" if txt.lower()=="пусто" else txt)
            st["step"]=5; bot.reply_to(m,f"Текущий лимит: {cap if cap is not None else 'без лимита'}. Введи число, «без лимита» или «-».")
        elif step==5:
            if txt in {"-","—",""}: new_cap = cap
            else:
                low=txt.lower()
                new_cap = None if low in {"без лимита","безлимита","нет"} else int(txt)
            st["data"]["capacity"]=new_cap
            st["step"]=6; bot.reply_to(m,"Введи новое описание (или «-»):")
        elif step==6:
            st["data"]["description"] = desc if txt in {"-","—",""} else ("" if txt.lower()=="пусто" else txt)
            st["step"]=7; bot.reply_to(m,"Введи новые награды (или «-»):")
        elif step==7:
            st["data"]["rewards"] = rew if txt in {"-","—",""} else ("" if txt.lower()=="пусто" else txt)
            st["step"]=8; bot.reply_to(m,f"Отчёты сейчас: {'вкл' if rep_req else 'выкл'}. Напиши «вкл» или «выкл».")
        elif step==8:
            if txt in {"-","—",""}: new_req = rep_req
            else: new_req = 1 if txt.lower() in {"вкл","да","+","on"} else 0
            st["data"]["report_required"]=new_req
            if new_req==0:
                st["data"]["report_schedule"]="none"; st["data"]["report_unit"]=""; st["data"]["report_photo_required"]=0
                st["step"]=12; bot.reply_to(m,"Отчёты выключены. Напиши «сохранить», чтобы применить.")
            else:
                st["step"]=9; bot.reply_to(m,f"Тип отчётов сейчас: {rep_sched or 'none'}. Напиши «ежедневный» или «финальный».")
        elif step==9:
            low=txt.lower()
            if txt in {"-","—"}: new_sched = rep_sched
            elif low.startswith("ежед") or low=="daily": new_sched="daily"
            elif low.startswith("фин") or low=="final": new_sched="final"
            else: bot.reply_to(m,"Напиши «ежедневный» или «финальный», либо «-»."); return
            st["data"]["report_schedule"]=new_sched
            st["step"]=10; bot.reply_to(m,f"Единица измерения сейчас: {rep_unit or '—'}. Введи новую или «-».")
        elif step==10:
            st["data"]["report_unit"] = rep_unit if txt in {"-","—"} else ("" if txt.lower()=="пусто" else txt)
            st["step"]=11; bot.reply_to(m,f"Фото-пруф сейчас {'обязателен' if rep_photo else 'не обязателен'}. Напиши «да» или «нет» (или «-»):")
        elif step==11:
            if txt in {"-","—"}: new_photo = rep_photo
            else: new_photo = 1 if txt.lower() in {"да","+","yes"} else 0
            st["data"]["report_photo_required"]=new_photo
            st["step"]=12; bot.reply_to(m,"Напиши «сохранить», чтобы применить изменения.")
        elif step==12:
            if txt.lower() not in {"сохранить","save","ok","да"}:
                bot.reply_to(m,"Напиши «сохранить», чтобы применить."); return
            vals = st["data"]
            with db() as con:
                cur=con.cursor()
                cur.execute("""UPDATE events
                               SET title=?, date_start=?, date_end=?, location=?, capacity=?, description=?, rewards=?,
                                   report_required=?, report_schedule=?, report_unit=?, report_photo_required=?
                               WHERE id=?""",
                            (vals.get("title",title), vals.get("date_start",ds), vals.get("date_end",de),
                             vals.get("location",loc), vals.get("capacity",cap), vals.get("description",desc),
                             vals.get("rewards",rew), vals.get("report_required",rep_req),
                             vals.get("report_schedule",rep_sched), vals.get("report_unit",rep_unit),
                             vals.get("report_photo_required",rep_photo), eid))
                con.commit()
            reset_state(uid); bot.reply_to(m,"Сохранила ✅")
    except ValueError as e:
        bot.reply_to(m, f"⚠️ {e}")
    except Exception:
        bot.reply_to(m,"Что-то пошло не так, попробуй ещё раз.")

# === Reports (ОДНО фото)
def upsert_report(event_id: int, user, date_str: str, value, text, photo_id: str | None):
    with db() as con:
        cur=con.cursor()
        cur.execute("INSERT OR REPLACE INTO reports(event_id,tg_user_id,date,value,text,photos,created_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, user.id, date_str, value, text, photo_id or "", dt.datetime.utcnow().isoformat()))
        con.commit()

@bot.callback_query_handler(func=lambda c: c.data.startswith("report:"))
def cb_report_start(c):
    user=c.from_user
    try:
        _, eid = c.data.split(":"); eid=int(eid)
    except:
        bot.answer_callback_query(c.id,"Ошибка."); return
    with db() as con:
        cur=con.cursor()
        cur.execute("SELECT report_required,report_schedule,report_unit,report_photo_required,date_start,date_end FROM events WHERE id=?", (eid,))
        row=cur.fetchone()
        if not row:
            bot.answer_callback_query(c.id,"Событие не найдено."); return
        req,sched,unit,photo_req,ds,de = row
        cur.execute("SELECT 1 FROM signups WHERE event_id=? AND tg_user_id=?", (eid,user.id))
        if not cur.fetchone():
            bot.answer_callback_query(c.id,"Сначала запишись на событие."); return
    today = today_str()
    if sched=="final" and today != de:
        bot.answer_callback_query(c.id,"Финальный отчёт — в день окончания."); return
    STATE[user.id] = {"mode":"report","event_id":eid,"step":1,"photo_req":photo_req}
    bot.answer_callback_query(c.id,"Ок!")
    if photo_req:
        bot.send_message(c.message.chat.id,"Пришли одно фото-пруф (обязательно).")
    else:
        bot.send_message(c.message.chat.id,"Пришли одно фото-пруф или напиши «-», если без фото.")

@bot.message_handler(
    func=lambda m: m.from_user.id in STATE and STATE[m.from_user.id].get("mode")=="report",
    content_types=['text','photo','document']
)
def report_flow(m):
    st=STATE[m.from_user.id]; step=st["step"]; eid=st["event_id"]; txt=(m.text or "").strip()
    if txt.lower() in {"отмена","/cancel","cancel"}:
        reset_state(m.from_user.id); bot.reply_to(m,"Ок, отменяю отчёт."); return

    if step==1:
        photo_id=None
        if m.content_type=="photo" and m.photo:
            photo_id=m.photo[-1].file_id
        elif m.content_type=="document" and getattr(m.document,"mime_type","").startswith("image/"):
            photo_id=m.document.file_id

        if st.get("photo_req") and not photo_id:
            bot.reply_to(m,"Нужно одно фото-пруф. Пришли фото.")
            return
        if not photo_id and txt not in {"-","—"}:
            bot.reply_to(m,"Пришли фото или «-».")
            return

        st["photo_id"]=photo_id
        st["step"]=2
        with db() as con:
            cur=con.cursor(); cur.execute("SELECT report_unit FROM events WHERE id=?", (eid,))
            unit=(cur.fetchone() or [""])[0] or ""
        bot.reply_to(m, f"Введи числовой результат{(' ('+unit+')') if unit else ''}. Пример: 12345")
        return

    if step==2:
        try:
            value=float(txt.replace(",","."))
        except:
            bot.reply_to(m,"Похоже, это не число. Введи число, например 12345")
            return
        st["value"]=value
        st["step"]=3
        bot.reply_to(m,"Напиши комментарий (или «-»):")
        return

    if step==3:
        comment = "" if txt in {"-","—"} else txt
        upsert_report(eid, m.from_user, today_str(), st.get("value"), comment, st.get("photo_id"))
        reset_state(m.from_user.id)
        bot.reply_to(m,"Отчёт сохранён ✅")
        return

    reset_state(m.from_user.id)
    bot.reply_to(m,"Давай начнём заново: нажми «📥 Отчёт» под событием.")

# === Leaderboard
def leaderboard_text(eid: int) -> str:
    with db() as con:
        cur=con.cursor()
        cur.execute("""SELECT s.tg_name,s.tg_username,SUM(r.value)
                       FROM reports r
                       JOIN signups s ON s.event_id=r.event_id AND s.tg_user_id=r.tg_user_id
                       WHERE r.event_id=?
                       GROUP BY r.tg_user_id
                       ORDER BY SUM(r.value) DESC""", (eid,))
        rows=cur.fetchall()
    if not rows:
        return "Пока нет данных для рейтинга."
    lines=[]
    for i,(name,username,total) in enumerate(rows,1):
        uname=f" @{username}" if username else ""
        if total is None: total = 0
        total_str = f"{int(total) if abs(total-int(total))<1e-9 else round(total,2)}"
        lines.append(f"{i}. {name}{uname} — {total_str}")
    return "🏆 Рейтинг\n" + "\n".join(lines[:20])

@bot.callback_query_handler(func=lambda c: c.data.startswith("lb:"))
def cb_leaderboard(c):
    try:
        _, eid = c.data.split(":"); eid=int(eid)
    except:
        bot.answer_callback_query(c.id,"Ошибка.")
        return
    bot.answer_callback_query(c.id,"Показываю рейтинг")
    bot.send_message(c.message.chat.id, leaderboard_text(eid))

# === Participants list (admin)
@bot.callback_query_handler(func=lambda c: c.data.startswith("plist:"))
def cb_participants(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id,"Только админ."); return
    try:
        _, eid=c.data.split(":"); eid=int(eid)
    except:
        bot.answer_callback_query(c.id,"Некорректно."); return
    with db() as con:
        cur=con.cursor()
        cur.execute("SELECT title FROM events WHERE id=?", (eid,))
        ev=cur.fetchone()
        if not ev:
            bot.answer_callback_query(c.id,"Не нашла событие."); return
        title=ev[0]
        cur.execute("SELECT tg_name,tg_username FROM signups WHERE event_id=? ORDER BY signed_at",(eid,))
        rows=cur.fetchall()
    if not rows:
        bot.send_message(c.message.chat.id, f"На «{title}» пока никто не записан.")
        bot.answer_callback_query(c.id,"Пусто.")
        return
    lines=[f"{i}. {n} {'@'+u if u else ''}".strip() for i,(n,u) in enumerate(rows,1)]
    bot.send_message(c.message.chat.id, f"Участники «{title}»:\n" + "\n".join(lines) + f"\n\nВсего: {len(rows)}")
    bot.answer_callback_query(c.id,"Готово ✅")

# === Join/Leave
@bot.callback_query_handler(func=lambda c: c.data.startswith(("join:","leave:")))
def cb_join_leave(c):
    user=c.from_user; cmd,eid=c.data.split(":"); eid=int(eid)
    with db() as con:
        cur=con.cursor()
        cur.execute("SELECT capacity,date_end,is_active FROM events WHERE id=?", (eid,))
        row=cur.fetchone()
        if not row:
            bot.answer_callback_query(c.id,"Событие не найдено."); return
        cap,de,active=row
        if not active:
            bot.answer_callback_query(c.id,"Событие выключено."); return
        if de < today_str():
            bot.answer_callback_query(c.id,"Событие уже завершено."); return
        if cmd=="join":
            if cap is not None:
                cur.execute("SELECT COUNT(*) FROM signups WHERE event_id=?", (eid,))
                taken=cur.fetchone()[0]
                if taken>=cap:
                    bot.answer_callback_query(c.id,"Мест нет 😕"); return
            cur.execute("SELECT 1 FROM signups WHERE event_id=? AND tg_user_id=?", (eid,user.id))
            if cur.fetchone():
                bot.answer_callback_query(c.id,"Ты уже записан(а)."); return
            cur.execute("INSERT INTO signups(event_id,tg_user_id,tg_username,tg_name,signed_at) VALUES(?,?,?,?,?)",
                        (eid,user.id,user.username or "", f"{user.first_name or ''} {user.last_name or ''}".strip(), dt.datetime.utcnow().isoformat()))
            con.commit()
            bot.answer_callback_query(c.id,"Готово! Ты записан(а) ✍️")
        else:
            cur.execute("DELETE FROM signups WHERE event_id=? AND tg_user_id=?", (eid,user.id))
            con.commit()
            bot.answer_callback_query(c.id,"Ты отписался(ась).")
    try:
        bot.edit_message_reply_markup(chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=event_keyboard(eid,user.id))
    except:
        pass

# === Reminders loop (старт и отчёты)
def reminders_loop():
    while True:
        try:
            today = local_today()
            plus2 = today + dt.timedelta(days=2)
            with db() as con:
                cur=con.cursor()
                # стартовые напоминания: сегодня и через 2 дня
                cur.execute("SELECT id,title,emoji,date_start FROM events WHERE is_active=1 AND date_start IN (?,?)",
                            (today.strftime("%Y-%m-%d"), plus2.strftime("%Y-%m-%d")))
                for eid,title,emoji,ds in cur.fetchall():
                    d1 = dt.datetime.strptime(ds,"%Y-%m-%d").date()
                    kind = "start" if d1==today else "start-2"
                    cur.execute("SELECT tg_user_id FROM signups WHERE event_id=?", (eid,))
                    for (uid,) in cur.fetchall():
                        cur.execute("SELECT 1 FROM notifications_sent WHERE event_id=? AND tg_user_id=? AND kind=?", (eid,uid,kind))
                        if cur.fetchone(): continue
                        emj = (emoji or "🏅").strip() or "🏅"
                        try:
                            bot.send_message(uid, f"{emj} Напоминание: «<b>{title}</b>» стартует {'сегодня' if kind=='start' else 'через 2 дня'}.")
                        except:
                            pass
                        cur.execute("INSERT OR IGNORE INTO notifications_sent(event_id,tg_user_id,kind,sent_at) VALUES(?,?,?,?)",
                                    (eid,uid,kind,dt.datetime.utcnow().isoformat()))
                # отчёты: daily — каждый день в диапазоне; final — в день окончания
                cur.execute("SELECT id,title,emoji,date_start,date_end,report_required,report_schedule FROM events WHERE is_active=1")
                for eid,title,emoji,ds,de,req,sched in cur.fetchall():
                    if not req: continue
                    d1 = dt.datetime.strptime(ds,"%Y-%m-%d").date()
                    d2 = dt.datetime.strptime(de,"%Y-%m-%d").date()
                    if sched=="daily" and d1 <= today <= d2:
                        kind="report-daily"
                    elif sched=="final" and today == d2:
                        kind="report-final"
                    else:
                        continue
                    cur.execute("SELECT tg_user_id FROM signups WHERE event_id=?", (eid,))
                    users = [u[0] for u in cur.fetchall()]
                    for uid in users:
                        if kind=="report-daily":
                            cur.execute("SELECT 1 FROM reports WHERE event_id=? AND tg_user_id=? AND date=?",
                                        (eid,uid,today.strftime("%Y-%m-%d")))
                            if cur.fetchone(): continue
                        cur.execute("SELECT 1 FROM notifications_sent WHERE event_id=? AND tg_user_id=? AND kind=?", (eid,uid,kind))
                        if cur.fetchone(): continue
                        emj = (emoji or "🏅").strip() or "🏅"
                        try:
                            bot.send_message(uid, f"{emj} Напомню: пришли отчёт по «<b>{title}</b>» — нажми «📥 Отчёт» в карточке.")
                        except:
                            pass
                        cur.execute("INSERT OR IGNORE INTO notifications_sent(event_id,tg_user_id,kind,sent_at) VALUES(?,?,?,?)",
                                    (eid,uid,kind,dt.datetime.utcnow().isoformat()))
                con.commit()
        except:
            pass
        time.sleep(1800)  # каждые ~30 минут

threading.Thread(target=reminders_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling(timeout=30, long_polling_timeout=30)
