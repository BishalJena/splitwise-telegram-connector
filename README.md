# Splitwise Telegram Connector

A FastAPI-based backend to connect Telegram with Splitwise, allowing users to add expenses via a Telegram bot.

## Features
- Multi-user Splitwise OAuth
- Add expenses via Telegram
- OpenAI-powered expense parsing

## Setup
1. Copy `.env.example` to `.env` and fill in your credentials.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the server:
   ```bash
   bash start.sh
   ```

## Folder Structure
```
splitwise-telegram-connector/
├── app/
│   └── main.py
├── requirements.txt
├── start.sh
├── .env.example
├── README.md
└── .gitignore
```
