import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Получаем токен из переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Проверка: если токен не найден, выводим ошибку и завершаем работу
if not BOT_TOKEN:
    logging.error("Ошибка: Переменная окружения BOT_TOKEN не установлена!")
    raise ValueError("Не удалось получить токен бота. Проверьте настройки в панели Bothost.")

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
    logging.info("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
