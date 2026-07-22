from dotenv import load_dotenv
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command, StateFilter
from aiogram import Bot, Dispatcher, types, F
from typing import Optional, List, Tuple, Set, Literal
from datetime import datetime, date, timedelta
from contextlib import contextmanager
import calendar
import logging
import asyncio
import sqlite3
import threading
import os
import re


load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

if not API_TOKEN:
    raise ValueError("BOT_TOKEN not set in .env")
if not ADMIN_CHAT_ID:
    raise ValueError("ADMIN_CHAT_ID not set in .env")

try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
except ValueError:
    raise ValueError("ADMIN_CHAT_ID must be a number")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DB_NAME = 'bookings.db'

MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

DEFAULT_SLOT_DURATION = 30
TELEGRAM_MAX_LENGTH = 4000

_db_lock = threading.Lock()


# Database helpers

@contextmanager
def db_cursor():
    conn = None
    with _db_lock:
        try:
            conn = sqlite3.connect(DB_NAME)
            conn.execute('PRAGMA journal_mode=WAL')
            yield conn.cursor()
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Database error: {e}")
            raise
            raise
        finally:
            if conn:
                conn.close()


def db_fetchall(query: str, params: tuple = ()) -> List[Tuple]:
    with db_cursor() as c:
        c.execute(query, params)
        return c.fetchall()


def db_fetchone(query: str, params: tuple = ()) -> Optional[Tuple]:
    with db_cursor() as c:
        c.execute(query, params)
        return c.fetchone()


def db_execute(query: str, params: tuple = ()) -> int:
    with db_cursor() as c:
        c.execute(query, params)
        return c.rowcount


def init_db():
    with db_cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                gender TEXT,
                services TEXT NOT NULL,
                master TEXT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                comment TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'active',
                duration INTEGER DEFAULT 0
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                consent_given INTEGER DEFAULT 0,
                consent_date TEXT
            )
        ''')
    logger.info("Database initialized")


def _booking_columns(c) -> List[str]:
    c.execute("PRAGMA table_info(bookings)")
    return [col[1] for col in c.fetchall()]


def migrate_db():
    with db_cursor() as c:
        columns = _booking_columns(c)

        if 'services' not in columns and 'service' in columns:
            c.execute('ALTER TABLE bookings RENAME COLUMN service TO services')
            columns = _booking_columns(c)
            logger.info("Migration: renamed service to services")

        if 'service' in columns and 'services' in columns:
            c.execute(
                'UPDATE bookings SET services = COALESCE(NULLIF(services, ""), service)'
            )
            logger.info("Migration: copied service data to services")

        migrations = [
            ('user_id', 'ALTER TABLE bookings ADD COLUMN user_id INTEGER'),
            ('gender', 'ALTER TABLE bookings ADD COLUMN gender TEXT'),
            ('services', 'ALTER TABLE bookings ADD COLUMN services TEXT'),
            ('master', 'ALTER TABLE bookings ADD COLUMN master TEXT'),
            ('comment', 'ALTER TABLE bookings ADD COLUMN comment TEXT'),
            ('status', 'ALTER TABLE bookings ADD COLUMN status TEXT DEFAULT "active"'),
            ('created_at', 'ALTER TABLE bookings ADD COLUMN created_at TEXT'),
            ('duration', 'ALTER TABLE bookings ADD COLUMN duration INTEGER DEFAULT 0'),
        ]
        for col, sql in migrations:
            if col not in _booking_columns(c):
                c.execute(sql)
                logger.info(f"Migration: added {col}")

        for col in ['reminder_24h_sent', 'reminder_3h_sent', 'reminder_1_5h_sent']:
            if col not in _booking_columns(c):
                c.execute(
                    f'ALTER TABLE bookings ADD COLUMN {col} INTEGER DEFAULT 0')
                logger.info(f"Migration: added {col}")

        if 'created_at' in _booking_columns(c):
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute(
                'UPDATE bookings SET created_at = ? WHERE created_at IS NULL', (now,))

        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if not c.fetchone():
            c.execute('''
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    consent_given INTEGER DEFAULT 0,
                    consent_date TEXT
                )
            ''')
            logger.info("Migration: added users table")


init_db()
migrate_db()


# Users / Consent

def ensure_user(user_id: int):
    db_execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))


def has_consent(user_id: int) -> bool:
    row = db_fetchone(
        'SELECT consent_given FROM users WHERE user_id = ?', (user_id,))
    return row is not None and row[0] == 1


def give_consent(user_id: int):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db_execute(
        'INSERT INTO users (user_id, consent_given, consent_date) VALUES (?, 1, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET consent_given = 1, consent_date = ?',
        (user_id, now, now)
    )


def delete_user_data(user_id: int):
    db_execute('DELETE FROM bookings WHERE user_id = ?', (user_id,))
    db_execute('DELETE FROM users WHERE user_id = ?', (user_id,))


def format_gender(gender: Optional[str]) -> str:
    if gender == 'male':
        return 'Мужчина'
    if gender == 'female':
        return 'Женщина'
    return gender or '—'


def to_services_set(selected) -> Set[str]:
    if isinstance(selected, list):
        return set(selected)
    if isinstance(selected, set):
        return selected
    return set()


def to_services_list(selected) -> List[str]:
    return list(to_services_set(selected))


def format_personal_row(row: Tuple) -> str:
    name, phone, gender, services, master, date_val, time_val, comment, status = row
    lines = [
        f"🆔 Запись",
        f"   👤 Имя: {name}",
        f"   📞 Телефон: {phone}",
        f"   {'👨' if gender == 'male' else '👩'} Пол: {format_gender(gender)}",
        f"   💼 Услуги: {services}",
        f"   👤 Мастер: {master}",
        f"   📅 Дата: {date_val}",
        f"   🕐 Время: {time_val}",
        f"   📊 Статус: {status}",
    ]
    if comment:
        lines.append(f"   💬 Комментарий: {comment}")
    return "\n".join(lines) + "\n"


def get_user_personal_data(user_id: int) -> str:
    rows = db_fetchall(
        'SELECT name, phone, gender, services, master, date, time, comment, status '
        'FROM bookings WHERE user_id = ? ORDER BY date DESC, time DESC',
        (user_id,)
    )
    if not rows:
        return "📭 У вас нет записей в системе."
    return "📋 <b>Ваши персональные данные:</b>\n\n" + "\n".join(format_personal_row(r) for r in rows)


# Bookings

def add_booking(user_id: int, gender: str, services: str, master: str,
                date: str, time: str, name: str, phone: str,
                comment: str = "", duration: int = 0) -> int:
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with db_cursor() as c:
        c.execute(
            'INSERT INTO bookings (user_id, gender, services, master, date, time, name, phone, comment, duration, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (user_id, gender, services, master, date,
             time, name, phone, comment, duration, created_at)
        )
        return c.lastrowid


def time_to_minutes(t: str) -> int:
    try:
        h, m = map(int, t.split(':'))
        return h * 60 + m
    except (ValueError, AttributeError):
        return 0


def is_slot_taken(date: str, time: str, duration: int = 0,
                  exclude_id: Optional[int] = None) -> bool:
    query = 'SELECT time, COALESCE(duration, 0) FROM bookings WHERE date = ? AND status = "active"'
    params = [date]
    if exclude_id:
        query += ' AND id != ?'
        params.append(exclude_id)

    start_new = time_to_minutes(time)
    end_new = start_new + duration

    for t, d in db_fetchall(query, params):
        start_existing = time_to_minutes(t)
        effective_duration = d if d > 0 else DEFAULT_SLOT_DURATION
        end_existing = start_existing + effective_duration
        if start_new < end_existing and start_existing < end_new:
            return True
    return False


def get_user_bookings(user_id: int) -> List[Tuple]:
    return db_fetchall(
        'SELECT id, services, master, date, time, name, phone, status, comment '
        'FROM bookings WHERE user_id = ? ORDER BY date DESC, time DESC',
        (user_id,)
    )


def get_all_bookings(limit: int = 50) -> List[Tuple]:
    return db_fetchall(
        'SELECT id, user_id, gender, services, master, date, time, name, phone, status, comment '
        'FROM bookings ORDER BY date DESC, time DESC LIMIT ?',
        (limit,)
    )


def get_today_bookings() -> List[Tuple]:
    today = datetime.now().strftime('%d.%m.%Y')
    return db_fetchall(
        'SELECT id, user_id, gender, services, master, date, time, name, phone, status, comment '
        'FROM bookings WHERE date = ? ORDER BY time',
        (today,)
    )


def cancel_booking(booking_id: int) -> bool:
    return db_execute('UPDATE bookings SET status = "cancelled" WHERE id = ?', (booking_id,)) > 0


def get_active_bookings_for_reminders() -> List[Tuple]:
    return db_fetchall(
        'SELECT id, user_id, services, master, date, time, name, '
        'reminder_24h_sent, reminder_3h_sent, reminder_1_5h_sent '
        'FROM bookings WHERE status = "active"'
    )


def mark_reminder_sent(booking_id: int, column: str):
    allowed_columns = {'reminder_24h_sent',
                       'reminder_3h_sent', 'reminder_1_5h_sent'}
    if column not in allowed_columns:
        raise ValueError("Invalid column name")
    db_execute(f'UPDATE bookings SET {column} = 1 WHERE id = ?', (booking_id,))


# Services

MALE_SERVICES = {
    "💇‍♂️ Стрижка (30 мин)": ("Стрижка", 30),
    "🧔 Стрижка бороды (20 мин)": ("Стрижка бороды", 20),
    "🪒 Бритьё опасной бритвой (30 мин)": ("Бритьё опасной бритвой", 30),
    "💆‍♂️ Комплекс (60 мин)": ("Комплекс (стрижка+борода)", 60),
    "🎨 Тонирование бороды (40 мин)": ("Тонирование бороды", 40),
    "✨ Укладка (20 мин)": ("Укладка", 20),
    "💅 Маникюр (45 мин)": ("Маникюр", 45),
    "🦶 Педикюр (60 мин)": ("Педикюр", 60),
    "🧖 Коррекция бровей (15 мин)": ("Коррекция бровей", 15),
    "💆 Массаж головы (30 мин)": ("Массаж головы", 30),
    "🧴 Уход за лицом (45 мин)": ("Уход за лицом", 45)
}

FEMALE_SERVICES = {
    "💇‍♀️ Стрижка (45 мин)": ("Стрижка", 45),
    "✨ Укладка (30 мин)": ("Укладка", 30),
    "💅 Маникюр (60 мин)": ("Маникюр", 60),
    "🦶 Педикюр (75 мин)": ("Педикюр", 75),
    "🎨 Окрашивание (120 мин)": ("Окрашивание", 120),
    "🧖 Коррекция бровей (20 мин)": ("Коррекция бровей", 20),
    "💆 Массаж головы (30 мин)": ("Массаж головы", 30),
    "🧴 Уход за лицом (60 мин)": ("Уход за лицом", 60),
    "💄 Макияж (45 мин)": ("Макияж", 45),
    "🌸 Наращивание ресниц (90 мин)": ("Наращивание ресниц", 90)
}

WORKING_HOURS_START = 9
WORKING_HOURS_END = 21


# Keyboards

def get_consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Согласен на обработку ПД",
                callback_data="consent:yes"
            )],
            [InlineKeyboardButton(
                text="📄 Политика конфиденциальности",
                callback_data="consent:privacy"
            )]
        ]
    )


def get_gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨 Мужчина"),
             KeyboardButton(text="👩 Женщина")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_services_keyboard(gender: str, selected=None) -> ReplyKeyboardMarkup:
    services = MALE_SERVICES if gender == "male" else FEMALE_SERVICES
    selected = to_services_set(selected)
    buttons = []
    row = []
    for i, (display, (name, _)) in enumerate(services.items()):
        prefix = "✅ " if name in selected else ""
        row.append(KeyboardButton(text=f"{prefix}{display}"))
        if len(row) == 2 or i == len(services) - 1:
            buttons.append(row)
            row = []
    buttons.append([KeyboardButton(text="✅ Готово")])
    buttons.append([KeyboardButton(text="❌ Отменить запись")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)


def get_master_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Любой мастер")],
            [KeyboardButton(text="❌ Отменить запись")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_edit_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить пол"),
             KeyboardButton(text="✏️ Изменить услуги")],
            [KeyboardButton(text="✏️ Изменить мастера"),
             KeyboardButton(text="✏️ Изменить дату")],
            [KeyboardButton(text="✏️ Изменить время"),
             KeyboardButton(text="✏️ Изменить контакты")],
            [KeyboardButton(text="✏️ Изменить комментарий")],
            [KeyboardButton(text="✅ Всё верно, подтвердить")],
            [KeyboardButton(text="❌ Отменить запись")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_comment_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Добавить комментарий")],
            [KeyboardButton(text="⏩ Пропустить")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить")],
            [KeyboardButton(text="❌ Отменить")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Записаться")],
            [KeyboardButton(text="📋 Мои записи"),
             KeyboardButton(text="❌ Отменить запись")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить запись")]],
        resize_keyboard=True
    )


def get_reminder_inline_kb(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="❌ Отменить запись",
                callback_data=f"cancel:{booking_id}"
            )]
        ]
    )


def get_time_slider_keyboard(hour: int, minute: int, date_str: str, duration: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"🕐 Текущее время: {hour:02d}:{minute:02d}", callback_data="time_slider:ignore"
        )
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"⬅️ {hour:02d} ➡️", callback_data="time_slider:hour_nav"
        )
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"⬆️ {minute:02d} ⬇️", callback_data="time_slider:minute_nav"
        )
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text="✅ Всё верно, выбрать", callback_data=f"time_slider:confirm:{hour:02d}:{minute:02d}"
        )
    ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Назад к списку", callback_data="time_slider:back_to_list"
        )
    ])

    return kb


def get_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"{MONTHS[month]} {year}", callback_data="cal:ignore")
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text=d, callback_data="cal:ignore") for d in DAYS
    ])

    cal = calendar.Calendar(firstweekday=0)
    today = date.today()
    max_date = today + timedelta(days=90)

    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(
                    text=" ", callback_data="cal:ignore"))
                continue
            d = date(year, month, day)
            if d < today or d > max_date:
                row.append(InlineKeyboardButton(
                    text=" ", callback_data="cal:ignore"))
            else:
                row.append(InlineKeyboardButton(
                    text=str(day),
                    callback_data=f"cal:select:{d.strftime('%Y-%m-%d')}"
                ))
        kb.inline_keyboard.append(row)

    nav = []
    prev_m = month - 1
    prev_y = year
    if prev_m < 1:
        prev_m = 12
        prev_y -= 1
    next_m = month + 1
    next_y = year
    if next_m > 12:
        next_m = 1
        next_y += 1

    nav.append(InlineKeyboardButton(
        text="<", callback_data=f"cal:nav:{prev_y}:{prev_m}"))
    nav.append(InlineKeyboardButton(
        text=">", callback_data=f"cal:nav:{next_y}:{next_m}"))
    kb.inline_keyboard.append(nav)
    return kb


# FSM

class Booking(StatesGroup):
    choose_gender = State()
    choose_services = State()
    choose_master = State()
    choose_date = State()
    choose_time = State()
    enter_contacts = State()
    enter_comment = State()
    edit = State()
    confirm = State()


# Bot init

bot = Bot(token=API_TOKEN)

try:
    import redis.asyncio as redis
    from aiogram.fsm.storage.redis import RedisStorage
    redis_client = redis.Redis(
        host='localhost', port=6379, db=0, decode_responses=False)
    storage = RedisStorage(redis=redis_client)
    logger.info("FSM storage: Redis")
except Exception as e:
    logger.warning("FSM Redis unavailable, using MemoryStorage")
    storage = MemoryStorage()

dp = Dispatcher(storage=storage)


# Helpers

def get_services_list(gender: str, selected_names: Set[str]) -> Tuple[str, int]:
    services = MALE_SERVICES if gender == "male" else FEMALE_SERVICES if gender == "female" else {}
    names = []
    total_time = 0
    for display, (name, duration) in services.items():
        if name in selected_names:
            names.append(name)
            total_time += duration
    return ", ".join(names), total_time


def build_booking_summary(data: dict, header: str = "📋 <b>Ваши данные:</b>\n") -> str:
    gender = data.get('gender', 'male')
    selected = to_services_set(data.get('selected_services', []))
    services_str, total_time = get_services_list(gender, selected)
    master = data.get('master', 'Любой')
    date_val = data.get('date', 'Не указана')
    time_val = data.get('time', 'Не указано')
    name = data.get('name', 'Не указано')
    phone = data.get('phone', 'Не указан')
    comment = data.get('comment', '')

    gender_emoji = "👨" if gender == "male" else "👩"
    lines = [
        header,
        f"{gender_emoji} Пол: {'Мужчина' if gender == 'male' else 'Женщина'}",
        f"💼 Услуги: <b>{services_str}</b>",
        f"⏱ Примерное время: <b>{total_time} мин</b>",
        f"👤 Мастер: <b>{master}</b>",
        f"📅 Дата: <b>{date_val}</b>",
        f"🕐 Время: <b>{time_val}</b>",
        f"👤 Имя: <b>{name}</b>",
        f"📞 Телефон: <b>{phone}</b>",
    ]
    if comment:
        lines.append(f"💬 Комментарий: <b>{comment}</b>")
    return "\n".join(lines)


def format_booking(row: Tuple, admin: bool = False) -> str:
    if admin:
        bid, uid, gender, services, master, date, time_val, name, phone, status, comment = row
        gender_emoji = "👨" if gender == "male" else "👩" if gender == "female" else "❓"
        header = f"{'✅' if status == 'active' else '🚫'} <b>Запись #{bid}</b> {gender_emoji} (User: {uid})\n"
    else:
        bid, services, master, date, time_val, name, phone, status, comment = row
        header = f"{'✅' if status == 'active' else '🚫'} <b>Запись #{bid}</b>\n"

    lines = [
        f"   💼 Услуги: {services}",
        f"   👤 Мастер: {master}",
        f"   📅 Дата: {date}",
        f"   🕐 Время: {time_val}",
        f"   👤 Имя: {name}",
        f"   📞 Телефон: {phone}",
    ]
    if comment:
        lines.append(f"   💬 Комментарий: {comment}")
    return header + "\n".join(lines) + "\n"


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


def split_message(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> List[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return parts


async def answer_long(message: types.Message, text: str, **kwargs):
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        send_kwargs = dict(kwargs)
        if i < len(chunks) - 1:
            send_kwargs.pop('reply_markup', None)
        await message.answer(chunk, **send_kwargs)


def get_reminder_time(appointment_dt: datetime, delta_hours: float) -> datetime:
    reminder = appointment_dt - timedelta(hours=delta_hours)
    if reminder.hour < 9:
        reminder = (reminder - timedelta(days=1)).replace(
            hour=21, minute=0, second=0, microsecond=0
        )
    elif reminder.hour >= 21:
        reminder = reminder.replace(
            hour=21, minute=0, second=0, microsecond=0
        )
    return reminder


REMINDER_TEMPLATES = {
    24: lambda name, date, time, services, master: (
        f"👋 <b>Здравствуйте, {name}!</b>\n\n"
        f"Напоминаем, что <b>завтра</b> ({date}) в {time} "
        f"у вас запись в наш салон.\n"
        f"💼 Услуги: {services}\n"
        f"👤 Мастер: {master}\n\n"
        f"Если планы изменились — свяжитесь с нами, чтобы освободить место."
    ),
    3: lambda name, date, time, services, master: (
        f"⏰ <b>Напоминание о записи</b>\n\n"
        f"{name}, через 3 часа ({time}) ждём вас в салоне!\n"
        f"💼 Услуги: {services}\n"
        f"👤 Мастер: {master}\n\n"
        f"До встречи! ✨"
    ),
    1.5: lambda name, date, time, services, master: (
        f"⏰ <b>Скоро встреча!</b>\n\n"
        f"{name}, через полтора часа ({time}) начинается ваша запись.\n"
        f"💼 Услуги: {services}\n"
        f"👤 Мастер: {master}\n\n"
        f"Уже готовимся к вашему визиту! 🌟"
    ),
}

REMINDER_CFG = [
    (24, 'reminder_24h_sent', 0),
    (3, 'reminder_3h_sent', 1),
    (1.5, 'reminder_1_5h_sent', 2),
]


async def send_reminder(user_id: int, booking_id: int, services: str,
                        master: str, date_str: str, time_str: str,
                        name: str, hours_before: float):
    try:
        text = REMINDER_TEMPLATES[hours_before](
            name, date_str, time_str, services, master)
        kb = get_reminder_inline_kb(
            booking_id) if hours_before == 1.5 else None
        await bot.send_message(
            chat_id=user_id, text=text, reply_markup=kb, parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")


async def check_and_send_reminders():
    now = datetime.now()
    bookings = get_active_bookings_for_reminders()

    for row in bookings:
        bid, uid, services, master, date_str, time_str, name, *flags = row
        try:
            appt_dt = datetime.strptime(
                f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        except ValueError:
            continue

        if appt_dt <= now:
            continue

        for hours, col, idx in REMINDER_CFG:
            if not flags[idx]:
                rt = get_reminder_time(appt_dt, hours)
                if now >= rt:
                    await send_reminder(
                        uid, bid, services, master, date_str, time_str, name, hours
                    )
                    mark_reminder_sent(bid, col)


async def reminder_loop():
    while True:
        try:
            await check_and_send_reminders()
        except Exception as e:
            logger.error(f"Reminder loop error: {e}")
        await asyncio.sleep(60)


async def require_consent(message: types.Message, state: FSMContext = None) -> bool:
    if not has_consent(message.from_user.id):
        await message.answer(
            "⚠️ Для записи необходимо согласие на обработку персональных данных.",
            reply_markup=get_consent_keyboard()
        )
        if state:
            await state.clear()
        return False
    return True


async def require_admin(message: types.Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return False
    return True


async def show_summary(message: types.Message, state: FSMContext,
                       edit_mode: Optional[bool] = None):
    user_data = await state.get_data()
    if edit_mode is None:
        edit_mode = user_data.get('is_edit', False)
    text = build_booking_summary(user_data)

    if edit_mode:
        text += "\n\nЧто хотите изменить?"
        await message.answer(text, reply_markup=get_edit_keyboard(), parse_mode="HTML")
        await state.set_state(Booking.edit)
    else:
        text += "\n\nВсё верно? Нажмите <b>Подтвердить</b> ✅"
        await message.answer(text, reply_markup=get_confirm_keyboard(), parse_mode="HTML")
        await state.set_state(Booking.confirm)


async def finalize_booking(message: types.Message, state: FSMContext):
    user_data = await state.get_data()

    if user_data.get('submitting'):
        await state.update_data(submitting=False)
        return

    if not await require_consent(message, state):
        return

    await state.update_data(submitting=True)
    user_data = await state.get_data()
    user_id = message.from_user.id

    gender = user_data.get('gender', 'male')
    selected = to_services_set(user_data.get('selected_services', []))
    services_str, total_time = get_services_list(gender, selected)
    master = user_data.get('master', 'Любой')
    date_val = user_data.get('date')
    time_val = user_data.get('time')
    name = user_data.get('name')
    phone = user_data.get('phone')
    comment = user_data.get('comment', '')

    if not selected or not date_val or not time_val or not name or not phone:
        await state.update_data(submitting=False)
        await message.answer(
            "❌ <b>Не все данные заполнены.</b> Пожалуйста, начните запись заново.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if is_slot_taken(date_val, time_val, total_time):
        await state.update_data(submitting=False, is_edit=True)
        await message.answer(
            f"😔 <b>К сожалению, время {time_val} на {date_val} уже занято.</b>\n\n"
            f"Пожалуйста, выберите другое время:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(Booking.choose_time)
        return

    try:
        booking_id = add_booking(
            user_id=user_id, gender=gender, services=services_str, master=master,
            date=date_val, time=time_val, name=name, phone=phone,
            comment=comment, duration=total_time
        )

        admin_lines = [
            "🔔 <b>НОВАЯ ЗАПИСЬ!</b>\n",
            f"🆔 ID записи: <code>{booking_id}</code>",
            f"👤 Клиент: <b>{name}</b>",
            f"{'👨' if gender == 'male' else '👩'} Пол: {'Мужчина' if gender == 'male' else 'Женщина'}",
            f"💼 Услуги: <b>{services_str}</b>",
            f"⏱ Время: <b>{total_time} мин</b>",
            f"👤 Мастер: <b>{master}</b>",
            f"📅 Дата: <b>{date_val}</b>",
            f"🕐 Время: <b>{time_val}</b>",
            f"📞 Телефон: <b>{phone}</b>",
        ]
        if comment:
            admin_lines.append(f"💬 Комментарий: <b>{comment}</b>")
        admin_lines.append(f"🆔 User ID: <code>{user_id}</code>")

        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text="\n".join(admin_lines),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")

        client_lines = [
            "🎉 <b>Запись подтверждена!</b>\n",
            f"💼 Услуги: {services_str}",
            f"⏱ Примерное время: {total_time} мин",
            f"👤 Мастер: {master}",
            f"📅 Дата: {date_val}",
            f"🕐 Время: {time_val}",
        ]
        if comment:
            client_lines.append(f"💬 Комментарий: {comment}")
        client_lines.append(
            "\n✨ Спасибо, что выбрали наш салон! Желаем вам прекрасного дня и отличного настроения! 🌸"
        )

        await message.answer(
            "\n".join(client_lines),
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Database error during booking: {e}")
        await state.update_data(submitting=False)
        await message.answer(
            "❌ <b>Произошла ошибка при сохранении записи.</b>\n"
            "Пожалуйста, попробуйте позже или свяжитесь с администратором.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.clear()


# Handlers

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    ensure_user(user_id)

    if not has_consent(user_id):
        await message.answer(
            f"👋 <b>Добро пожаловать, {message.from_user.first_name}!</b>\n\n"
            "Я бот для записи в барбершоп / салон красоты. ✨\n\n"
            "Для продолжения необходимо ваше согласие на обработку персональных данных "
            "(имя и телефон), которые нужны для создания записи.\n\n"
            "Вы можете ознакомиться с политикой конфиденциальности перед согласием.",
            reply_markup=get_consent_keyboard(),
            parse_mode="HTML"
        )
        return

    welcome_text = (
        f"👋 <b>Добро пожаловать, {message.from_user.first_name}!</b>\n\n"
        f"Я бот для записи в барбершоп / салон красоты. ✨\n\n"
        f"📝 Чтобы записаться — нажмите кнопку ниже\n"
        f"📋 Чтобы посмотреть свои записи — /mybookings\n"
        f"ℹ️ Чтобы узнать команды — /help\n\n"
        f"👨‍💻 <b>Разработчик:</b> @physwimath\n"
        f"📩 По вопросам и для сотрудничества — пишите в личные сообщения\n\n"
        f"Выберите действие:"
    )
    await message.answer(
        welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "consent:yes")
async def process_consent_yes(callback: types.CallbackQuery):
    give_consent(callback.from_user.id)
    await callback.answer("Спасибо за согласие!")
    try:
        await callback.message.edit_text(
            "✅ <b>Согласие получено.</b>\n\nТеперь вы можете пользоваться всеми функциями бота.",
            parse_mode="HTML"
        )
    except Exception:
        pass
    welcome_text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "📝 Чтобы записаться — нажмите кнопку ниже\n"
        "📋 Чтобы посмотреть свои записи — /mybookings\n"
        "ℹ️ Чтобы узнать команды — /help\n\n"
        "Выберите действие:"
    )
    await callback.message.answer(
        welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "consent:privacy")
async def process_consent_privacy(callback: types.CallbackQuery):
    privacy_text = (
        "📄 <b>Политика конфиденциальности</b>\n\n"
        "1. Мы обрабатываем только имя и номер телефона, необходимые для создания записи.\n"
        "2. Данные хранятся в защищённой базе и не передаются третьим лицам.\n"
        "3. Вы можете в любой момент запросить удаление данных через /delete_my_data.\n"
        "4. Срок хранения: 3 года с момента последней записи.\n"
        "5. Используя бота, вы соглашаетесь с обработкой указанных данных."
    )
    await callback.answer()
    await callback.message.answer(privacy_text, parse_mode="HTML")


@dp.message(Command('help'))
@dp.message(F.text.lower().in_(['ℹ️ помощь', 'помощь']))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 <b>Команды бота:</b>\n\n"
        "📝 <b>/book</b> или <b>📝 Записаться</b> — записаться на услугу\n"
        "📋 <b>/mybookings</b> или <b>📋 Мои записи</b> — посмотреть свои записи\n"
        "❌ <b>/cancel</b> — отменить текущую операцию\n"
        "👤 <b>/my_data</b> — ваши персональные данные\n"
        "🗑 <b>/delete_my_data</b> — удалить все данные\n"
        "🚫 <b>/withdraw_consent</b> — отозвать согласие\n\n"
        "👨‍💼 <b>Админ-команды:</b>\n"
        "/admin — все записи\n"
        "/today — записи на сегодня\n\n"
        "👨‍💻 <b>Разработчик:</b> @physwimath\n"
        "💡 <b>Совет:</b> Записывайтесь заранее, чтобы выбрать удобное время!"
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command('cancel'))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        await message.answer(
            "🚫 Операция отменена. Возвращаюсь в главное меню.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "ℹ️ Нет активной операции для отмены.",
            reply_markup=get_main_keyboard()
        )


@dp.message(
    StateFilter(Booking),
    F.text.lower().in_(['❌ отменить запись', 'отменить запись',
                        'отмена', 'отменить'])
)
async def cancel_any_state(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚫 Операция отменена. Возвращаюсь в главное меню.",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text.lower().in_(['📝 записаться', 'записаться', '/book']))
async def start_booking(message: types.Message, state: FSMContext):
    if not await require_consent(message, state):
        return
    await state.clear()
    await message.answer(
        "👤 <b>Кто будет записываться?</b>\n\n"
        "Выберите, пожалуйста:",
        reply_markup=get_gender_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(Booking.choose_gender)


@dp.message(Booking.choose_gender)
async def process_gender(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if "мужчина" in text.lower():
        gender = "male"
    elif "женщина" in text.lower():
        gender = "female"
    else:
        await message.answer(
            "⚠️ Пожалуйста, выберите <b>👨 Мужчина</b> или "
            "<b>👩 Женщина</b> с помощью кнопок ниже 👇",
            reply_markup=get_gender_keyboard(),
            parse_mode="HTML"
        )
        return

    user_data = await state.get_data()
    is_edit = user_data.get('is_edit', False)

    if is_edit:
        old_gender = user_data.get('gender', 'male')
        if old_gender != gender:
            await state.update_data(gender=gender, selected_services=[])
            await message.answer(
                "⚠️ <b>Пол изменён — выбор услуг сброшен.</b>\n"
                "💈 Выберите услуги заново:",
                reply_markup=get_services_keyboard(gender),
                parse_mode="HTML"
            )
            await state.set_state(Booking.choose_services)
        else:
            await state.update_data(gender=gender)
            await show_summary(message, state)
    else:
        await state.update_data(gender=gender, selected_services=[])
        await message.answer(
            "💈 <b>Выберите услуги</b> (можно несколько):\n\n"
            "Нажимайте на услуги, чтобы выбрать/убрать. "
            "Когда закончите — нажмите <b>✅ Готово</b> 👇",
            reply_markup=get_services_keyboard(gender),
            parse_mode="HTML"
        )
        await state.set_state(Booking.choose_services)


@dp.message(Booking.choose_services)
async def process_services(message: types.Message, state: FSMContext):
    text = message.text.strip()
    user_data = await state.get_data()
    gender = user_data.get('gender', 'male')
    selected = to_services_set(user_data.get('selected_services', []))
    services = MALE_SERVICES if gender == "male" else FEMALE_SERVICES
    is_edit = user_data.get('is_edit', False)

    if text == "✅ Готово":
        if not selected:
            await message.answer(
                "⚠️ Выберите хотя бы одну услугу!",
                reply_markup=get_services_keyboard(gender, selected),
                parse_mode="HTML"
            )
            await state.set_state(Booking.choose_services)
            return
        await state.update_data(selected_services=to_services_list(selected))
        if is_edit:
            await show_summary(message, state)
        else:
            await message.answer(
                "👤 <b>Выберите мастера:</b>\n\n"
                "Напишите фамилию и имя мастера, к которому хотите записаться, "
                "или нажмите <b>👤 Любой мастер</b> 👇",
                reply_markup=get_master_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(Booking.choose_master)
        return

    display_text = text.lstrip("✅ ").strip()
    service_name = None
    for display, (name, _) in services.items():
        if display_text == display or text == display:
            service_name = name
            break

    if service_name:
        if service_name in selected:
            selected.remove(service_name)
        else:
            selected.add(service_name)
        await state.update_data(selected_services=to_services_list(selected))
        await message.answer(
            "💈 <b>Выберите услуги</b> (можно несколько):\n\n"
            f"Выбрано: <b>{len(selected)}</b>",
            reply_markup=get_services_keyboard(gender, selected),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "⚠️ Пожалуйста, используйте кнопки ниже 👇",
            reply_markup=get_services_keyboard(gender, selected)
        )


@dp.message(Booking.choose_master)
async def process_master(message: types.Message, state: FSMContext):
    text = message.text.strip()
    user_data = await state.get_data()
    is_edit = user_data.get('is_edit', False)

    if text == "👤 Любой мастер":
        master = "Любой"
    elif not text:
        await message.answer(
            "⚠️ Пожалуйста, введите фамилию и имя мастера "
            "или нажмите <b>👤 Любой мастер</b> 👇",
            reply_markup=get_master_keyboard(),
            parse_mode="HTML"
        )
        return
    else:
        master = text

    await state.update_data(master=master)

    if is_edit:
        await show_summary(message, state)
    else:
        today = date.today()
        await message.answer(
            "📅 <b>Выберите дату записи:</b>",
            reply_markup=get_calendar_keyboard(today.year, today.month),
            parse_mode="HTML"
        )
        await state.set_state(Booking.choose_date)


@dp.callback_query(Booking.choose_date, F.data.startswith("cal:"))
async def process_calendar_callback(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data.split(":")
    if len(data) < 2:
        await callback.answer()
        return

    action = data[1]

    if action == "ignore":
        await callback.answer()
        return

    if action == "nav":
        if len(data) >= 4:
            year, month = int(data[2]), int(data[3])
            await callback.message.edit_reply_markup(
                reply_markup=get_calendar_keyboard(year, month)
            )
        await callback.answer()
        return

    if action == "select":
        raw_date = data[2]
        selected = datetime.strptime(raw_date, "%Y-%m-%d").date()
        today = date.today()

        if selected < today:
            await callback.answer("Нельзя выбрать прошедшую дату", show_alert=True)
            return
        if selected > today + timedelta(days=90):
            await callback.answer("Запись возможна не более чем на 3 месяца", show_alert=True)
            return

        formatted = selected.strftime("%d.%m.%Y")
        user_data = await state.get_data()
        is_edit = user_data.get('is_edit', False)
        update = {'date': formatted}
        if is_edit:
            update['old_date'] = user_data.get(
                'old_date', user_data.get('date'))
        await state.update_data(**update)
        user_data = await state.get_data()

        if is_edit:
            old_date = user_data.get('old_date')
            old_time = user_data.get('time')
            if old_date != formatted and old_time:
                gender = user_data.get('gender', 'male')
                selected_set = to_services_set(
                    user_data.get('selected_services', []))
                _, duration = get_services_list(gender, selected_set)
                if is_slot_taken(formatted, old_time, duration):
                    await callback.message.answer(
                        f"😔 <b>Время {old_time} на {formatted} уже занято.</b>\n"
                        f"Пожалуйста, выберите другое время:",
                        reply_markup=get_cancel_keyboard(),
                        parse_mode="HTML"
                    )
                    await state.set_state(Booking.choose_time)
                    await callback.answer()
                    return
            await show_summary(callback.message, state)
        else:
            await callback.message.answer(
                f"📅 Дата: <b>{formatted}</b>\n\n"
                f"🕐 Введите время в формате ЧЧ:ММ:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(Booking.choose_time)
    await callback.answer()






@dp.message(Booking.choose_time)
async def process_time(message: types.Message, state: FSMContext):
    time_input = message.text.strip()
    try:
        booking_time = datetime.strptime(time_input, '%H:%M').time()
        formatted_time = booking_time.strftime('%H:%M')

        user_data = await state.get_data()
        gender = user_data.get('gender', 'male')
        selected_set = to_services_set(user_data.get('selected_services', []))
        _, duration = get_services_list(gender, selected_set)

        start_min = booking_time.hour * 60 + booking_time.minute
        end_min = start_min + duration

        end_hour = end_min // 60
        end_minute = end_min % 60

        if start_min < WORKING_HOURS_START * 60 or end_min > WORKING_HOURS_END * 60:
            await message.answer(
                f"⚠️ <b>Абонент не укладывается в сроки!</b>\n"
                f"Время окончания {end_hour:02d}:{end_minute:02d} выходит за пределы рабочих часов "
                f"(с {WORKING_HOURS_START}:00 по {WORKING_HOURS_END}:00).\n\n"
                f"Пожалуйста, запишитесь раньше:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="HTML"
            )
            return

    except ValueError:
        await message.answer(
            "❌ <b>Неверный формат времени!</b>\n\n"
            "Используйте формат: <b>ЧЧ:ММ</b>\n"
            "Например: <code>15:30</code>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    user_data = await state.get_data()
    booking_date = user_data.get('date')
    is_edit = user_data.get('is_edit', False)

    if is_slot_taken(booking_date, formatted_time, duration):
        await message.answer(
            f"😔 <b>К сожалению, время {formatted_time} на {booking_date} "
            f"уже занято.</b>\n\n"
            f"Пожалуйста, выберите другое время:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(time=formatted_time)

    if is_edit:
        await show_summary(message, state)
    else:
        await message.answer(
            f"🕐 Время: <b>{formatted_time}</b>\n\n"
            f"👤 Введите ваше <b>имя</b> и <b>номер телефона</b> через пробел:\n"
            f"Например: <code>Иван 89123456789</code>\n\n"
            f"💡 Телефон нужен для подтверждения записи",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(Booking.enter_contacts)


@dp.message(Booking.enter_contacts)
async def process_contacts(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer(
            "⚠️ Пожалуйста, введите имя и номер телефона:",
            reply_markup=get_cancel_keyboard()
        )
        return

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "❌ <b>Неверный формат!</b>\n\n"
            "Введите: <b>Имя НомерТелефона</b>\n"
            "Например: <code>Иван 89123456789</code>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    name = parts[0]
    phone = parts[1].strip()

    if not re.fullmatch(r'8[0-9]{10}', phone):
        await message.answer(
            "❌ <b>Неверный формат телефона!</b>\n"
            "Введите 11 цифр, начиная с 8. Пример: <code>89001234567</code>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(name=name, phone=phone)
    user_data = await state.get_data()
    is_edit = user_data.get('is_edit', False)

    if is_edit:
        await show_summary(message, state)
    else:
        await message.answer(
            "💬 <b>Хотите добавить комментарий к записи?</b>\n\n"
            "Например: «аллергия на краску», «предпочитаю мастера Анну» "
            "или «первый раз, нужна консультация»\n\n"
            "Выберите действие:",
            reply_markup=get_comment_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(Booking.enter_comment)


@dp.message(Booking.enter_comment, F.text == "⏩ Пропустить")
async def skip_comment(message: types.Message, state: FSMContext):
    await state.update_data(comment="")
    await show_summary(message, state)


@dp.message(Booking.enter_comment, F.text == "💬 Добавить комментарий")
async def ask_comment_text(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 Напишите ваш комментарий:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )


@dp.message(Booking.enter_comment)
async def process_comment(message: types.Message, state: FSMContext):
    comment = message.text.strip()
    await state.update_data(comment=comment)
    await show_summary(message, state)


EDIT_ACTIONS = {
    "✏️ Изменить пол": lambda ud: (
        Booking.choose_gender,
        "👤 <b>Выберите пол:</b>",
        get_gender_keyboard()
    ),
    "✏️ Изменить услуги": lambda ud: (
        Booking.choose_services,
        "💈 <b>Выберите услуги</b> (можно несколько):\n\n"
        f"Выбрано: <b>{len(to_services_set(ud.get('selected_services', [])))}</b>",
        get_services_keyboard(ud.get('gender', 'male'),
                              ud.get('selected_services', []))
    ),
    "✏️ Изменить мастера": lambda ud: (
        Booking.choose_master,
        "👤 <b>Выберите мастера:</b>\n\n"
        "Напишите фамилию и имя мастера, или нажмите <b>👤 Любой мастер</b> 👇",
        get_master_keyboard()
    ),
    "✏️ Изменить дату": lambda ud: (
        Booking.choose_date,
        "📅 <b>Выберите новую дату:</b>",
        get_calendar_keyboard(date.today().year, date.today().month)
    ),
    "✏️ Изменить время": lambda ud: (
        Booking.choose_time,
        "🕐 Укажите новое время в формате <b>ЧЧ:ММ</b>\n"
        "Например: <code>15:30</code>",
        get_cancel_keyboard()
    ),
    "✏️ Изменить контакты": lambda ud: (
        Booking.enter_contacts,
        "👤 Введите имя и телефон через пробел:\n"
        "Например: <code>Иван 89123456789</code>",
        get_cancel_keyboard()
    ),
    "✏️ Изменить комментарий": lambda ud: (
        Booking.enter_comment,
        "📝 Напишите новый комментарий:",
        get_cancel_keyboard()
    ),
}


@dp.message(Booking.edit)
async def process_edit(message: types.Message, state: FSMContext):
    text = message.text.strip()

    if text == "✅ Всё верно, подтвердить":
        await state.update_data(is_edit=False)
        await finalize_booking(message, state)
        return

    user_data = await state.get_data()
    action = EDIT_ACTIONS.get(text)

    if action:
        new_state, msg, kb = action(user_data)
        update = {'is_edit': True}
        if text == "✏️ Изменить дату":
            update['old_date'] = user_data.get('date')
        await state.update_data(**update)
        await message.answer(msg, reply_markup=kb, parse_mode="HTML")
        await state.set_state(new_state)
    else:
        await message.answer(
            "⚠️ Пожалуйста, используйте кнопки ниже 👇",
            reply_markup=get_edit_keyboard()
        )


@dp.message(Booking.confirm, F.text == "✅ Подтвердить")
async def process_confirm(message: types.Message, state: FSMContext):
    await finalize_booking(message, state)


@dp.message(Booking.confirm, F.text == "❌ Отменить")
async def process_cancel_booking(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🚫 Запись отменена.\n\n"
        "Если передумаете — просто нажмите 📝 Записаться!",
        reply_markup=get_main_keyboard()
    )


@dp.message(Command('mybookings'))
@dp.message(F.text.lower().in_(['📋 мои записи', 'мои записи']))
async def cmd_mybookings(message: types.Message):
    if not await require_consent(message):
        return

    bookings = get_user_bookings(message.from_user.id)

    if not bookings:
        await message.answer(
            "📭 <b>У вас пока нет записей.</b>\n\n"
            "Нажмите 📝 Записаться, чтобы создать первую запись!",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        return

    text = "📋 <b>Ваши записи:</b>\n\n"
    for row in bookings:
        text += format_booking(row) + "\n"

    text += "\n💡 Чтобы отменить запись — свяжитесь с администратором"
    await answer_long(
        message, text, reply_markup=get_main_keyboard(), parse_mode="HTML"
    )


@dp.message(Command('my_data'))
async def cmd_my_data(message: types.Message):
    if not await require_consent(message):
        return
    text = get_user_personal_data(message.from_user.id)
    await answer_long(message, text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command('delete_my_data'))
async def cmd_delete_my_data(message: types.Message):
    delete_user_data(message.from_user.id)
    await message.answer(
        "🗑 <b>Ваши данные удалены.</b>\n\n"
        "Все записи и персональная информация полностью стёрты из системы.\n"
        "Если захотите вернуться — просто нажмите /start.",
        parse_mode="HTML"
    )


@dp.message(Command('withdraw_consent'))
async def cmd_withdraw_consent(message: types.Message):
    delete_user_data(message.from_user.id)
    await message.answer(
        "🚫 <b>Согласие отозвано.</b>\n\n"
        "Все ваши данные удалены. Для повторного использования бота "
        "необходимо будет дать согласие заново через /start.",
        parse_mode="HTML"
    )


@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if not await require_admin(message):
        return

    bookings = get_all_bookings(limit=20)

    if not bookings:
        await message.answer("📭 Записей пока нет.")
        return

    text = "📊 <b>Последние записи:</b>\n\n"
    for row in bookings:
        text += format_booking(row, admin=True) + "\n"

    await answer_long(message, text, parse_mode="HTML")


@dp.message(Command('today'))
async def cmd_today(message: types.Message):
    if not await require_admin(message):
        return

    bookings = get_today_bookings()

    if not bookings:
        await message.answer("📭 На сегодня записей нет.")
        return

    text = (
        f"📅 <b>Записи на сегодня "
        f"({datetime.now().strftime('%d.%m.%Y')}):</b>\n\n"
    )
    for row in bookings:
        text += format_booking(row, admin=True) + "\n"

    await answer_long(message, text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("cancel:"))
async def process_cancel_callback(callback: types.CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    row = db_fetchone(
        'SELECT user_id, status FROM bookings WHERE id = ?', (booking_id,)
    )

    if not row:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    user_id, status = row
    if user_id != callback.from_user.id:
        await callback.answer("Это не ваша запись", show_alert=True)
        return

    if status != "active":
        await callback.answer("Запись уже отменена или выполнена", show_alert=True)
        return

    if cancel_booking(booking_id):
        await callback.message.edit_text("🚫 <b>Запись отменена.</b>")
        await callback.answer("Запись отменена")
    else:
        await callback.answer("Не удалось отменить запись", show_alert=True)


@dp.message()
async def unknown_message(message: types.Message, state: FSMContext):
    await message.answer(
        "🤔 Я не понял команду.\n\n"
        "Используйте кнопки меню или отправьте /help для списка команд.",
        reply_markup=get_main_keyboard()
    )


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started")
    asyncio.create_task(reminder_loop())
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Главное меню"),
        types.BotCommand(command="book", description="Записаться"),
        types.BotCommand(command="mybookings", description="Мои записи"),
        types.BotCommand(command="help", description="Помощь"),
        types.BotCommand(command="cancel", description="Отменить действие"),
        types.BotCommand(command="my_data", description="Ваши данные"),
        types.BotCommand(command="delete_my_data",
                         description="Удалить данные"),
        types.BotCommand(command="withdraw_consent",
                         description="Отозвать согласие"),
        types.BotCommand(command="admin", description="Админка"),
        types.BotCommand(command="today", description="Записи на сегодня"),
    ])
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
