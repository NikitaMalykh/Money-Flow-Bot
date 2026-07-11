import os
import sqlite3
import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

import os
print(f"📁 Текущая рабочая папка: {os.getcwd()}")
print(f"📄 Путь к БД: {os.path.abspath('bookings.db')}")
print(f"📄 БД существует? {os.path.exists(os.path.abspath('bookings.db'))}")

load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

if not API_TOKEN:
    raise ValueError("❌ Не задан BOT_TOKEN в .env")
if not ADMIN_CHAT_ID:
    raise ValueError("❌ Не задан ADMIN_CHAT_ID в .env")

try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
except ValueError:
    raise ValueError(
        "❌ ADMIN_CHAT_ID должен быть числом (ID чата администратора)")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ==================== БАЗА ДАННЫХ ====================

DB_NAME = 'bookings.db'


def get_db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            gender TEXT,
            service TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            comment TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'active'
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")


def migrate_db():
    """Добавляет недостающие колонки в старую БД"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(bookings)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'user_id' not in columns:
        cursor.execute('ALTER TABLE bookings ADD COLUMN user_id INTEGER')
        conn.commit()
        logger.info("🔧 Миграция: добавлена колонка user_id")

    if 'gender' not in columns:
        cursor.execute('ALTER TABLE bookings ADD COLUMN gender TEXT')
        conn.commit()
        logger.info("🔧 Миграция: добавлена колонка gender")

    if 'comment' not in columns:
        cursor.execute('ALTER TABLE bookings ADD COLUMN comment TEXT')
        conn.commit()
        logger.info("🔧 Миграция: добавлена колонка comment")

    if 'status' not in columns:
        cursor.execute(
            'ALTER TABLE bookings ADD COLUMN status TEXT DEFAULT "active"')
        conn.commit()
        logger.info("🔧 Миграция: добавлена колонка status")

    if 'created_at' not in columns:
        cursor.execute('ALTER TABLE bookings ADD COLUMN created_at TEXT')
        conn.commit()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            'UPDATE bookings SET created_at = ? WHERE created_at IS NULL',
            (now,))
        conn.commit()
        logger.info("🔧 Миграция: добавлена колонка created_at")

    conn.close()


init_db()
migrate_db()


def add_booking(user_id: int, gender: str, service: str, date: str,
                time: str, name: str, phone: str, comment: str = "") -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO bookings (user_id, gender, service, date, time, name, phone, comment) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (user_id, gender, service, date, time, name, phone, comment)
    )
    booking_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def is_slot_taken(date: str, time: str,
                  exclude_id: Optional[int] = None) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    if exclude_id:
        cursor.execute(
            'SELECT COUNT(*) FROM bookings WHERE date = ? AND time = ? '
            'AND status = "active" AND id != ?',
            (date, time, exclude_id)
        )
    else:
        cursor.execute(
            'SELECT COUNT(*) FROM bookings WHERE date = ? AND time = ? '
            'AND status = "active"',
            (date, time)
        )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def get_user_bookings(user_id: int) -> List[Tuple]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, service, date, time, name, phone, status, comment '
        'FROM bookings WHERE user_id = ? ORDER BY date, time',
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_bookings(limit: int = 50) -> List[Tuple]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, user_id, gender, service, date, time, name, phone, status, comment '
        'FROM bookings ORDER BY date DESC, time DESC LIMIT ?',
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_today_bookings() -> List[Tuple]:
    today = datetime.now().strftime('%d.%m.%Y')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, user_id, gender, service, date, time, name, phone, status, comment '
        'FROM bookings WHERE date = ? ORDER BY time',
        (today,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def cancel_booking(booking_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE bookings SET status = "cancelled" WHERE id = ?',
        (booking_id,))
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


# ==================== УСЛУГИ ====================

MALE_SERVICES = {
    "💇‍♂️ Стрижка": "Стрижка",
    "🧔 Стрижка бороды": "Стрижка бороды",
    "🪒 Бритьё опасной бритвой": "Бритьё опасной бритвой",
    "💆‍♂️ Комплекс (стрижка+борода)": "Комплекс (стрижка+борода)",
    "🎨 Тонирование бороды": "Тонирование бороды",
    "✨ Укладка": "Укладка",
    "💅 Маникюр": "Маникюр",
    "🦶 Педикюр": "Педикюр",
    "🧖 Коррекция бровей": "Коррекция бровей",
    "💆 Массаж головы": "Массаж головы",
    "🧴 Уход за лицом": "Уход за лицом"
}

FEMALE_SERVICES = {
    "💇‍♀️ Стрижка": "Стрижка",
    "✨ Укладка": "Укладка",
    "💅 Маникюр": "Маникюр",
    "🦶 Педикюр": "Педикюр",
    "🎨 Окрашивание": "Окрашивание",
    "🧖 Коррекция бровей": "Коррекция бровей",
    "💆 Массаж головы": "Массаж головы",
    "🧴 Уход за лицом": "Уход за лицом",
    "💄 Макияж": "Макияж",
    "🌸 Наращивание ресниц": "Наращивание ресниц"
}

WORKING_HOURS_START = 9
WORKING_HOURS_END = 20


# ==================== КЛАВИАТУРЫ ====================

def get_gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨 Мужчина"),
             KeyboardButton(text="👩 Женщина")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_services_keyboard(gender: str) -> ReplyKeyboardMarkup:
    services = MALE_SERVICES if gender == "male" else FEMALE_SERVICES
    buttons = []
    row = []
    for i, (display, _) in enumerate(services.items()):
        row.append(KeyboardButton(text=display))
        if len(row) == 2 or i == len(services) - 1:
            buttons.append(row)
            row = []
    return ReplyKeyboardMarkup(
        keyboard=buttons, resize_keyboard=True, one_time_keyboard=True
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


# ==================== FSM ====================

class Booking(StatesGroup):
    choose_gender = State()
    choose_service = State()
    choose_date = State()
    choose_time = State()
    enter_contacts = State()
    enter_comment = State()
    confirm = State()


# ==================== ИНИЦИАЛИЗАЦИЯ ====================

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================

async def show_confirmation(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    name = user_data.get('name')
    phone = user_data.get('phone')
    comment = user_data.get('comment', '')

    confirmation_text = (
        "📋 <b>Проверьте данные записи:</b>\n\n"
        f"💼 Услуга: <b>{user_data['service']}</b>\n"
        f"📅 Дата: <b>{user_data['date']}</b>\n"
        f"🕐 Время: <b>{user_data['time']}</b>\n"
        f"👤 Имя: <b>{name}</b>\n"
        f"📞 Телефон: <b>{phone}</b>\n"
    )
    if comment:
        confirmation_text += f"💬 Комментарий: <b>{comment}</b>\n"
    confirmation_text += "\nВсё верно? Нажмите <b>Подтвердить</b> ✅"

    await message.answer(
        confirmation_text,
        reply_markup=get_confirm_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(Booking.confirm)


def format_booking(row: Tuple, admin: bool = False) -> str:
    if not admin:
        bid, service, date, time_val, name, phone, status, comment = row
        status_emoji = "✅" if status == "active" else "🚫"
        result = (
            f"{status_emoji} <b>Запись #{bid}</b>\n"
            f"   💼 Услуга: {service}\n"
            f"   📅 Дата: {date}\n"
            f"   🕐 Время: {time_val}\n"
            f"   👤 Имя: {name}\n"
            f"   📞 Телефон: {phone}\n"
        )
        if comment:
            result += f"   💬 Комментарий: {comment}\n"
        return result
    else:
        bid, uid, gender, service, date, time_val, name, phone, status, comment = row
        status_emoji = "✅" if status == "active" else "🚫"
        gender_emoji = "👨" if gender == "male" else "👩" if gender == "female" else "❓"
        result = (
            f"{status_emoji} <b>Запись #{bid}</b> {gender_emoji} (User: {uid})\n"
            f"   💼 Услуга: {service}\n"
            f"   📅 Дата: {date}\n"
            f"   🕐 Время: {time_val}\n"
            f"   👤 Имя: {name}\n"
            f"   📞 Телефон: {phone}\n"
        )
        if comment:
            result += f"   💬 Комментарий: {comment}\n"
        return result


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


# ==================== ОБРАБОТЧИКИ ====================

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
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


@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 <b>Команды бота:</b>\n\n"
        "📝 <b>/book</b> или <b>📝 Записаться</b> — записаться на услугу\n"
        "📋 <b>/mybookings</b> или <b>📋 Мои записи</b> — посмотреть свои записи\n"
        "❌ <b>/cancel</b> — отменить текущую операцию\n\n"
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
    if text == "👨 Мужчина":
        gender = "male"
    elif text == "👩 Женщина":
        gender = "female"
    else:
        await message.answer(
            "⚠️ Пожалуйста, выберите <b>👨 Мужчина</b> или "
            "<b>👩 Женщина</b> с помощью кнопок ниже 👇",
            reply_markup=get_gender_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(gender=gender)
    await message.answer(
        "💈 <b>Выберите услугу:</b>\n\n"
        "Нажмите на одну из кнопок ниже 👇",
        reply_markup=get_services_keyboard(gender),
        parse_mode="HTML"
    )
    await state.set_state(Booking.choose_service)


@dp.message(Booking.choose_service)
async def process_service(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    gender = user_data.get('gender', 'male')
    services = MALE_SERVICES if gender == "male" else FEMALE_SERVICES

    text = message.text
    service = None
    for display, value in services.items():
        if text == display or text == value:
            service = value
            break

    if not service:
        await message.answer(
            "⚠️ Пожалуйста, выберите услугу из списка, "
            "используя кнопки ниже 👇",
            reply_markup=get_services_keyboard(gender)
        )
        return

    await state.update_data(service=service)
    await message.answer(
        f"✅ Вы выбрали: <b>{service}</b>\n\n"
        f"📅 Теперь укажите дату записи в формате <b>ДД.ММ.ГГГГ</b>\n"
        f"Например: <code>25.07.2026</code>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(Booking.choose_date)


@dp.message(Booking.choose_date)
async def process_date(message: types.Message, state: FSMContext):
    date_input = message.text.strip()
    try:
        booking_date = datetime.strptime(date_input, '%d.%m.%Y').date()
        today = date.today()

        if booking_date < today:
            await message.answer(
                "⚠️ <b>Нельзя записаться на прошедшую дату!</b>\n"
                "Пожалуйста, укажите сегодняшнюю или будущую дату:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="HTML"
            )
            return

        if booking_date > today + timedelta(days=90):
            await message.answer(
                "⚠️ <b>Запись возможна не более чем на 3 месяца вперед.</b>\n"
                "Пожалуйста, укажите более близкую дату:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="HTML"
            )
            return

        formatted_date = booking_date.strftime('%d.%m.%Y')
    except ValueError:
        await message.answer(
            "❌ <b>Неверный формат даты!</b>\n\n"
            "Используйте формат: <b>ДД.ММ.ГГГГ</b>\n"
            "Например: <code>20.07.2026</code>",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(date=formatted_date)
    await message.answer(
        f"📅 Дата: <b>{formatted_date}</b>\n\n"
        f"🕐 Укажите удобное время в формате <b>ЧЧ:ММ</b>\n"
        f"Рабочие часы: с <b>{WORKING_HOURS_START}:00</b> "
        f"до <b>{WORKING_HOURS_END}:00</b>\n"
        f"Например: <code>15:30</code>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(Booking.choose_time)


@dp.message(Booking.choose_time)
async def process_time(message: types.Message, state: FSMContext):
    time_input = message.text.strip()
    try:
        booking_time = datetime.strptime(time_input, '%H:%M').time()
        formatted_time = booking_time.strftime('%H:%M')

        if (booking_time.hour < WORKING_HOURS_START or
                booking_time.hour >= WORKING_HOURS_END):
            await message.answer(
                f"⚠️ <b>Мы работаем с {WORKING_HOURS_START}:00 "
                f"до {WORKING_HOURS_END}:00!</b>\n"
                f"Пожалуйста, выберите время в пределах рабочих часов:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="HTML"
            )
            return

        if booking_time.minute not in [0, 30]:
            await message.answer(
                "⚠️ <b>Запись возможна только каждые 30 минут</b> "
                "(например, 10:00, 10:30, 11:00...)\n"
                "Пожалуйста, укажите другое время:",
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

    if is_slot_taken(booking_date, formatted_time):
        await message.answer(
            f"😔 <b>К сожалению, время {formatted_time} на {booking_date} "
            f"уже занято.</b>\n\n"
            f"Пожалуйста, выберите другое время:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(time=formatted_time)
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
    clean_phone = ''.join(c for c in phone if c.isdigit() or c == '+')
    digits_only = ''.join(c for c in clean_phone if c.isdigit())

    if len(digits_only) < 10:
        await message.answer(
            "❌ <b>Неверный номер телефона!</b>\n"
            "Введите номер из хотя бы 10 цифр:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML"
        )
        return

    await state.update_data(name=name, phone=clean_phone)
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
    await show_confirmation(message, state)


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
    await show_confirmation(message, state)


@dp.message(Booking.confirm, F.text == "✅ Подтвердить")
async def process_confirm(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = message.from_user.id

    gender = user_data.get('gender', 'male')
    service = user_data.get('service', 'Не указано')
    date_val = user_data.get('date', 'Не указана')
    time_val = user_data.get('time', 'Не указано')
    name = user_data.get('name', 'Не указано')
    phone = user_data.get('phone', 'Не указан')
    comment = user_data.get('comment', '')

    try:
        booking_id = add_booking(
            user_id=user_id,
            gender=gender,
            service=service,
            date=date_val,
            time=time_val,
            name=name,
            phone=phone,
            comment=comment
        )

        admin_text = (
            "🔔 <b>НОВАЯ ЗАПИСЬ!</b>\n\n"
            f"🆔 ID записи: <code>{booking_id}</code>\n"
            f"👤 Клиент: <b>{name}</b>\n"
            f"{'👨' if gender == 'male' else '👩'} Пол: "
            f"{'Мужчина' if gender == 'male' else 'Женщина'}\n"
            f"💼 Услуга: <b>{service}</b>\n"
            f"📅 Дата: <b>{date_val}</b>\n"
            f"🕐 Время: <b>{time_val}</b>\n"
            f"📞 Телефон: <b>{phone}</b>\n"
        )
        if comment:
            admin_text += f"💬 Комментарий: <b>{comment}</b>\n"
        admin_text += f"🆔 User ID: <code>{user_id}</code>"

        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=admin_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить админу: {e}")

        client_text = (
            "🎉 <b>Запись подтверждена!</b>\n\n"
            f"💼 Услуга: {service}\n"
            f"📅 Дата: {date_val}\n"
            f"🕐 Время: {time_val}\n"
        )
        if comment:
            client_text += f"💬 Комментарий: {comment}\n"
        client_text += (
            "\n✨ Мы ждём вас! Если нужно перенести или отменить запись — "
            "свяжитесь с нами."
        )

        await message.answer(
            client_text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Ошибка записи в БД: {e}")
        await message.answer(
            "❌ <b>Произошла ошибка при сохранении записи.</b>\n"
            "Пожалуйста, попробуйте позже или свяжитесь с администратором.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )

    await state.clear()


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
    user_id = message.from_user.id
    bookings = get_user_bookings(user_id)

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
    await message.answer(
        text, reply_markup=get_main_keyboard(), parse_mode="HTML"
    )


@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    bookings = get_all_bookings(limit=20)

    if not bookings:
        await message.answer("📭 Записей пока нет.")
        return

    text = "📊 <b>Последние записи:</b>\n\n"
    for row in bookings:
        text += format_booking(row, admin=True) + "\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command('today'))
async def cmd_today(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
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

    await message.answer(text, parse_mode="HTML")


@dp.message()
async def unknown_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await message.answer(
            "⚠️ Я не понимаю это сообщение в текущем контексте.\n"
            "Пожалуйста, следуйте инструкциям или отправьте /cancel для отмены."
        )
    else:
        await message.answer(
            "🤔 Я не понял команду.\n\n"
            "Используйте кнопки меню или отправьте /help для списка команд.",
            reply_markup=get_main_keyboard()
        )


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
