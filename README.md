# Splitwise Telegram Connector

A FastAPI-based backend to connect Telegram with Splitwise, allowing users to add expenses via a Telegram bot, with OpenAI-powered parsing and semantic memory/search via supermemory.

---

## Features
- Multi-user Splitwise OAuth
- Add expenses via Telegram
- OpenAI-powered expense parsing
- Command-based expense management
- Balance checking and expense history
- **Semantic memory:** All chat messages and expenses are stored per user in supermemory
- **Semantic search:** Users can search their own chat and expense history in natural language

---

## 1. Setup: Step-by-Step

### Prerequisites
- Create a Telegram bot using [@BotFather](https://t.me/botfather)
- Register an [OAuth application on Splitwise](https://secure.splitwise.com/apps)
- Get an [OpenAI API key](https://platform.openai.com/api-keys)
- Get a [supermemory API key](https://supermemory.ai/)

### Environment Variables
Create a `.env` file in your project root:
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
SPLITWISE_CLIENT_ID=your_splitwise_client_id
SPLITWISE_CLIENT_SECRET=your_splitwise_client_secret
OPENAI_API_KEY=your_openai_api_key
SUPERMEMORY_API_KEY=your_supermemory_api_key
CALLBACK_BASE_URL=https://your-domain.com
PORT=8000
```
- **CALLBACK_BASE_URL** must match the URL you set in the Splitwise app dashboard. For local dev, use an [ngrok](https://ngrok.com/) HTTPS URL.

### Installation
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Start the Server
```bash
bash start.sh
```
Or, for development:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Set Telegram Webhook
```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" -d "url=https://your-domain.com/telegram/webhook"
```
Or use the `/api/setup-webhook` endpoint.

---

## 2. Production Best Practices
- **Secrets:** Never commit `.env` or API keys. Use environment variables in your deployment platform.
- **OAuth:** If using a dynamic URL (e.g., ngrok), update both `.env` and Splitwise app settings.
- **Security:** User tokens are stored in `user_tokens.json` (gitignored). Rotate API keys regularly.
- **Monitoring:** Enable logging and monitor for errors in your hosting environment.

---

## 3. How It Works

### User Flow
1. User sends `/start` to the bot.
2. Bot replies with an OAuth link.
3. User authorizes Splitwise.
4. User can now add expenses, check balances, and search history.

### Adding Expenses
- Send messages like:
  - `"paid 500 for lunch"`
  - `"paid 1000 for dinner with John and Alice"`
- The bot uses OpenAI to parse the message, then creates the expense in Splitwise.
- The **authoritative split** (from Splitwise API) is shown in the confirmation and stored in supermemory.

### Semantic Memory & Search
- **All chat messages and expenses** are stored in supermemory, partitioned per user.
- **Semantic search:** Send a query like `"pizza"` or `"expenses with Alice last month"` to get relevant results from your history.

### Commands
- `/help` — Show all commands and usage tips.
- `show me recent expenses` — List recent transactions.
- `show me <category> expenses` — Filter by category.
- `show expenses with <friend>` — Filter by friend.
- `how much do I owe <friend>` — Check balance.
- `delete last expense` — Remove the most recent expense.

---

## 4. Testing & Development

### Running Tests
```bash
pytest --cov=app --disable-warnings
```
- **Coverage:** All core features, error handling, and supermemory integration are tested.

### Project Structure
```
splitwise-telegram-connector/
├── app/
│   ├── __init__.py
│   ├── main.py
│   └── test_main.py
├── requirements.txt
├── start.sh
├── README.md
└── .gitignore
```

---

## 5. Troubleshooting & Tips
- **OAuth Issues:** If you get redirect errors, check that CALLBACK_BASE_URL matches your public URL in both `.env` and Splitwise app settings.
- **ngrok:** If your ngrok URL changes, update both `.env` and Splitwise.
- **Expense Parsing:** If the bot can't parse your message, try rephrasing or use `/help` for examples.
- **Supermemory Errors:** Ensure your API key is valid and you have network access.

---

## 6. Changelog Highlights
- **Accurate expense splitting:** Handles rounding and ensures shares sum to total.
- **Robust error handling:** Only confirms expenses if Splitwise API succeeds.
- **Semantic memory:** All user data is partitioned and searchable.
- **Production-ready:** Secure, tested, and easy to deploy.

---

## 7. License
MIT License
