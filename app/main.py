from fastapi import FastAPI, Request, APIRouter, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import os
import httpx
import openai
import logging
import json
from urllib.parse import urlencode
import traceback
import re

# Load environment variables
load_dotenv()

# Set up logging
tlogging = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')

# ====== Environment Variables ======
# Please set these in your Render/hosting environment
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")    # e.g. '123456:ABC-DEF...'
SPLITWISE_CONSUMER_KEY    = os.getenv("SPLITWISE_CONSUMER_KEY")    # your Splitwise consumer key
SPLITWISE_CONSUMER_SECRET = os.getenv("SPLITWISE_CONSUMER_SECRET")  # your Splitwise consumer secret
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")            # your OpenAI API key
CALLBACK_BASE_URL         = os.getenv("CALLBACK_BASE_URL")         # e.g. 'https://your-domain.com/auth/splitwise/callback'

# Validate critical env vars
for var_name in ["TELEGRAM_BOT_TOKEN", "SPLITWISE_CONSUMER_KEY", "SPLITWISE_CONSUMER_SECRET", "OPENAI_API_KEY", "CALLBACK_BASE_URL"]:
    if not globals().get(var_name):
        logging.error(f"Missing required environment variable: {var_name}")
        # If missing, the service will still start, but endpoints depending on it will fail.

# Configure OpenAI key
openai.api_key = OPENAI_API_KEY

# Storage files
tokens_file = "user_tokens.json"
pending_file = "pending_oauth.json"

# Ensure storage exists
for fname in (tokens_file, pending_file):
    if not os.path.exists(fname):
        with open(fname, 'w') as f:
            json.dump({}, f)

# Initialize FastAPI
app = FastAPI(
    title="Splitwise Telegram Connector",
    description="FastAPI app to connect Telegram bot with Splitwise, with OAuth and expense parsing.",
    version="1.0.0"
)
router = APIRouter()

@app.get("/health")
def health():
    return {"status": "ok"}

# ----------- Models -----------
class ExpenseInput(BaseModel):
    cost: float
    description: str
    paid_by: int
    owed_by: list[int]

class ParseInput(BaseModel):
    text: str

class WebhookInput(BaseModel):
    url: str

# ----------- Storage Helpers -----------
def load_json(fname):
    with open(fname, 'r') as f:
        return json.load(f)

def save_json(fname, data):
    with open(fname, 'w') as f:
        json.dump(data, f)

def get_user_token(chat_id: str) -> str | None:
    tokens = load_json(tokens_file)
    return tokens.get(chat_id)

def set_user_token(chat_id: str, access_token: str):
    tokens = load_json(tokens_file)
    tokens[chat_id] = access_token
    save_json(tokens_file, tokens)

# ----------- Splitwise OAuth Flow -----------
@router.get("/auth/splitwise/start")
async def start_oauth(chat_id: int):
    """Begin OAuth flow: redirect user to Splitwise authorize URL."""
    request_url = "https://secure.splitwise.com/oauth/request_token"
    callback_url = f"{CALLBACK_BASE_URL}?chat_id={chat_id}"
    params = {
        'oauth_consumer_key': SPLITWISE_CONSUMER_KEY,
        'oauth_signature_method': 'PLAINTEXT',
        'oauth_signature': f"{SPLITWISE_CONSUMER_SECRET}&",
        'oauth_callback': callback_url
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(request_url, params=params)
        res.raise_for_status()
    data = dict(pair.split('=') for pair in res.text.split('&'))
    request_token = data['oauth_token']
    request_secret = data['oauth_token_secret']

    pending = load_json(pending_file)
    pending[str(chat_id)] = {'request_token': request_token, 'request_secret': request_secret}
    save_json(pending_file, pending)

    auth_url = f"https://secure.splitwise.com/oauth/authorize?{urlencode({'oauth_token': request_token})}"
    return {"auth_url": auth_url}

@router.get("/auth/splitwise/callback")
async def callback_oauth(oauth_token: str, oauth_verifier: str, chat_id: int):
    """Handle OAuth callback: exchange for access token and store it."""
    pending = load_json(pending_file)
    entry = pending.pop(str(chat_id), None)
    if not entry or entry['request_token'] != oauth_token:
        raise HTTPException(status_code=400, detail="Invalid or expired request token")

    access_url = "https://secure.splitwise.com/oauth/access_token"
    params = {
        'oauth_consumer_key': SPLITWISE_CONSUMER_KEY,
        'oauth_token': oauth_token,
        'oauth_signature_method': 'PLAINTEXT',
        'oauth_signature': f"{SPLITWISE_CONSUMER_SECRET}&",
        'oauth_verifier': oauth_verifier
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(access_url, params=params)
        res.raise_for_status()
    token_data = dict(pair.split('=') for pair in res.text.split('&'))
    set_user_token(str(chat_id), token_data['oauth_token'])
    save_json(pending_file, pending)
    return {"status": "authorized"}

# ----------- Services -----------
async def parse_expense_from_text(text: str) -> dict:
    prompt = (
        "Extract amount, description, paid_by and owed_by from: '{}'. Respond ONLY with a valid JSON object, no explanation.".format(text)
    )
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        content = response.choices[0].message.content
        logging.debug(f"OpenAI response content: {content}")

        # Remove markdown code block if present
        content = re.sub(r"^```json\s*|^```\s*|```$", "", content.strip(), flags=re.MULTILINE).strip()

        return json.loads(content)
    except json.JSONDecodeError as e:
        logging.error(f"OpenAI returned invalid JSON: {content}")
        raise HTTPException(status_code=500, detail=f"Parsing failed: Invalid JSON returned by model: {content}")
    except Exception as e:
        logging.error(f"OpenAI parsing error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(e)}")

async def create_splitwise_expense(chat_id: str, expense: dict):
    token = get_user_token(chat_id)
    if not token:
        raise HTTPException(status_code=401, detail="User not authorized with Splitwise")
    url = "https://secure.splitwise.com/api/v3.0/create_expense"
    headers = {"Authorization": f"Bearer {token}"}
    # Build payload
    data = {"cost": expense["cost"], "description": expense["description"], "currency_code": expense.get("currency_code", "INR")}
    data.update({"users__0__user_id": expense["paid_by"], "users__0__paid_share": expense["cost"]})
    equal = round(expense["cost"]/len(expense["owed_by"]), 2)
    for i, uid in enumerate(expense["owed_by"]):
        data[f"users__{i}__user_id"] = uid
        share = expense.get("shares", {}).get(str(uid), equal)
        data[f"users__{i}__owed_share"] = share
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, data=data, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logging.error(f"Splitwise API error: {e}")
        raise HTTPException(status_code=502, detail="Splitwise error")

# ----------- Telegram Messaging -----------
async def send_telegram_message(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        logging.debug(f"Sending Telegram message to {chat_id}: {text}")
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        logging.warning(f"Telegram send error: {e}")

# ----------- Telegram Webhook -----------
@router.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    logging.debug("Received webhook call")
    try:
        payload = await req.json()
        logging.debug(f"Webhook payload: {payload}")
    except Exception as e:
        logging.error(f"Invalid JSON payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        logging.info("No message in payload, ignoring")
        return {"ok": True}

    chat_id = str(msg.get("chat", {}).get("id"))
    text = msg.get("text", "").strip()
    logging.debug(f"Message from {chat_id}: {text}")

    if text.startswith("/start"):
        try:
            resp = await start_oauth(int(chat_id))
            await send_telegram_message(chat_id, f"Authorize here: {resp['auth_url']}")
        except Exception as e:
            logging.exception("Error starting OAuth")
            await send_telegram_message(chat_id, f"❌ OAuth start error: {e}")
        return {"ok": True}

    # Handle expense
    try:
        parsed = await parse_expense_from_text(text)
        logging.debug(f"Parsed expense: {parsed}")
        res = await create_splitwise_expense(chat_id, parsed)
        logging.debug(f"Splitwise response: {res}")
        await send_telegram_message(chat_id, f"✅ Expense added: {parsed['description']} - ₹{parsed['cost']}")
    except HTTPException as he:
        logging.warning(f"HTTP error: {he.detail}")
        await send_telegram_message(chat_id, f"❌ {he.detail}")
    except Exception as e:
        logging.exception("Webhook handling error")
        await send_telegram_message(chat_id, "❌ Internal error")
    return {"ok": True}

# ----------- Additional API Endpoints -----------
@router.post("/api/expense")
async def api_create_expense(expense: ExpenseInput, chat_id: str):
    return await create_splitwise_expense(chat_id, expense.dict())

@router.post("/api/parse")
async def api_parse_expense(payload: ParseInput):
    return {"parsed": await parse_expense_from_text(payload.text)}

@router.post("/api/setup-webhook")
async def setup_telegram_webhook(data: WebhookInput):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json={"url": data.url})
            res.raise_for_status()
        return res.json()
    except Exception as e:
        logging.exception("Webhook setup failed")
        raise HTTPException(status_code=500, detail="Webhook setup failed")

# Include router and root
app.include_router(router)

@app.get("/")
def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
