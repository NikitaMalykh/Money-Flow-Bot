import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Вставьте сюда ваш токен от BotFather
BOT_TOKEN = "8703925321:AAHhoAkDl-JZAohfJd928NKwTVNjuXmtJ4k"

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация объектов
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Обработка команды /start


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я твой первый бот. Отправь мне любое сообщение.")

# Обработка любых других текстовых сообщений (эхо-бот)


@dp.message()
async def echo(message: types.Message):
    await message.answer(f"Ты написал: {message.text}")

# Запуск бота


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
