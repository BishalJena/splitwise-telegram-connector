# Splitwise Telegram Connector

A FastAPI-based backend to connect Telegram with Splitwise, allowing users to add expenses via a Telegram bot.

https://github.com/user-attachments/assets/60e81d3b-027b-40fd-965d-2d31ab384e75

[Youtube Demo: https://youtu.be/FY1rhC9Ax3g?si=y6nG-xkbs_epzSEJ]

## Features
- Multi-user Splitwise OAuth
- Add expenses via Telegram
- OpenAI-powered expense parsing
- Command-based expense management
- Balance checking and expense history

## Setup

### Prerequisites
1. Create a Telegram bot using [@BotFather](https://t.me/botfather)
2. Register an [OAuth application on Splitwise](https://secure.splitwise.com/apps)
3. Get an [OpenAI API key](https://platform.openai.com/api-keys)

### Environment Variables
Create a `.env` file with the following variables:
```bash
# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Splitwise OAuth Credentials
SPLITWISE_CLIENT_ID=your_splitwise_client_id
SPLITWISE_CLIENT_SECRET=your_splitwise_client_secret

# OpenAI API Key
OPENAI_API_KEY=your_openai_api_key

# Base URL for OAuth Callback
CALLBACK_BASE_URL=https://your-domain.com

# Optional: Port for local development (default: 8000)
PORT=8000
```

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/splitwise-telegram-connector.git
   cd splitwise-telegram-connector
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Run the server:
   ```bash
   bash start.sh
   ```

## Usage

### Available Commands
- `show me recent expenses` - View recent transactions
- `show me <category> expenses` - View expenses by category
- `show expenses with <friend>` - View expenses with a specific friend
- `how much do I owe <friend>` - Check balance with a friend
- `show my balances` - View all balances
- `delete expense #<id>` - Delete a specific expense
- `delete last expense` - Delete the most recent expense
- `/help` - Show all available commands

Or simply send an expense description to add it, for example:
- "paid 500 for lunch"
- "paid 1000 for dinner with John and Alice"

## Development

### Running Tests
```bash
python -m pytest app/test_main.py -v
```

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

## Security Notes
- Never commit your `.env` file
- Keep your OAuth tokens secure
- Regularly rotate your API keys
- User tokens are stored in `user_tokens.json` (gitignored)

## License
MIT License
