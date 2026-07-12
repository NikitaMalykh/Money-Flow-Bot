# 💈 Money Flow Bot

Telegram-бот для онлайн-записи в барбершоп / салон красоты.  
FSM-диалог, inline-календарь, проверка пересечения слотов по длительности услуг, автонапоминания (24ч / 3ч / 1.5ч), GDPR (согласие / удаление данных), админ-панель.

## 🚀 Быстрый старт

```bash
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env: BOT_TOKEN и ADMIN_CHAT_ID
python main.py