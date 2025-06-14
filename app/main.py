from fastapi import FastAPI, Request, APIRouter, HTTPException, Depends
from pydantic import BaseModel
from dotenv import load_dotenv
import os
import httpx
import openai
import logging
import json
from urllib.parse import urlencode

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV VARIABLES
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPLITWISE_CONSUMER_KEY = os.getenv("SPLITWISE_CONSUMER_KEY")
SPLITWISE_CONSUMER_SECRET = os.getenv("SPLITWISE_CONSUMER_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CALLBACK_BASE_URL = os.getenv("CALLBACK_BASE_URL")  # e.g. https://your-domain.com/auth/splitwise/callback

openai.api_key = OPENAI_API_KEY

# Storage files
TOKENS_FILE = "user_tokens.json"
PENDING_FILE = "pending_oauth.json"

# Ensure storage exists
for fname in (TOKENS_FILE, PENDING_FILE):
    if not os.path.exists(fname):
        with open(fname, 'w') as f:
            json.dump({}, f)

app = FastAPI(
    title="Splitwise Telegram MCP API",
    description="API to interact with Splitwise through Telegram bot with multi-user support.",
    version="1.0.0"
)
router = APIRouter()

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
    with open(fname) as f:
        return json.load(f)

def save_json(fname, data):
    with open(fname, 'w') as f:
        json.dump(data, f)

def get_user_token(chat_id: str) -> str | None:
    tokens = load_json(TOKENS_FILE)
    return tokens.get(chat_id)

def set_user_token(chat_id: str, access_token: str):
    tokens = load_json(TOKENS_FILE)
    tokens[chat_id] = access_token
    save_json(TOKENS_FILE, tokens)

# ----------- Splitwise OAuth Flow -----------
@router.get("/auth/splitwise/start")
async def start_oauth(chat_id: int):
    # Request OAuth token
    oauth_request_url = "https://secure.splitwise.com/oauth/request_token"
    callback_url = f"{CALLBACK_BASE_URL}?chat_id={chat_id}"
    oauth_params = {
        'oauth_consumer_key': SPLITWISE_CONSUMER_KEY,
        'oauth_signature_method': 'PLAINTEXT',
        'oauth_signature': f"{SPLITWISE_CONSUMER_SECRET}&",
        'oauth_callback': callback_url
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(oauth_request_url, params=oauth_params)
        res.raise_for_status()
        # Parse response
        data = dict(x.split('=') for x in res.text.split('&'))
        request_token = data['oauth_token']
        request_secret = data['oauth_token_secret']
    # Store pending
    pending = load_json(PENDING_FILE)
    pending[str(chat_id)] = {'request_token': request_token, 'request_secret': request_secret}
    save_json(PENDING_FILE, pending)
    # Redirect user to authorize URL
    params = urlencode({'oauth_token': request_token})
    auth_url = f"https://secure.splitwise.com/oauth/authorize?{params}"
    return {"auth_url": auth_url}

@router.get("/auth/splitwise/callback")
async def splitwise_callback(chat_id: int, oauth_token: str, oauth_verifier: str):
    # Exchange for access token
    pending = load_json(PENDING_FILE)
    entry = pending.pop(str(chat_id), None)
    if not entry or entry['request_token'] != oauth_token:
        raise HTTPException(status_code=400, detail="Invalid or expired request token")
    set_user_token(str(chat_id), '')  # placeholder
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
        data = dict(x.split('=') for x in res.text.split('&'))
    access_token = data['oauth_token']
    # Save permanent token
    set_user_token(str(chat_id), access_token)
    save_json(PENDING_FILE, pending)
    return {"status": "authorized"}

# ----------- Services -----------
async def parse_expense_from_text(text: str) -> dict:
    prompt = f"The user sent this message: \"{text}\"\nExtract cost, description, paid_by and owed_by as JSON."
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}]
        )
        return eval(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"OpenAI parse error: {e}")
        raise HTTPException(status_code=500, detail="Parsing failed")

async def create_splitwise_expense(chat_id: str, expense: dict):
    token = get_user_token(chat_id)
    if not token:
        raise HTTPException(status_code=401, detail="User not authorized with Splitwise")
    url = "https://secure.splitwise.com/api/v3.0/create_expense"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"cost": expense["cost"], "description": expense["description"], "currency_code": "INR"}
    equal = round(expense["cost"]/len(expense["owed_by"]), 2)
    data.update({"users__0__user_id": expense["paid_by"], "users__0__paid_share": expense["cost"]})
    for i, uid in enumerate(expense["owed_by"]):
        data[f"users__{i}__user_id"] = uid
        data[f"users__{i}__owed_share"] = equal
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, data=data, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"Splitwise API error: {e}")
        raise HTTPException(status_code=502, detail="Splitwise error")

# ----------- Telegram Webhook  -----------
@router.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    payload = await req.json()
    msg = payload.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id"))
    text = msg.get("text", "")
    if text.startswith("/start"):
        # send auth link
        resp = await start_oauth(int(chat_id))
        await send_telegram_message(chat_id, f"Authorize here: {resp['auth_url']}")
    else:
        try:
            parsed = await parse_expense_from_text(text)
            res = await create_splitwise_expense(chat_id, parsed)
            await send_telegram_message(chat_id, f"✅ Added: {parsed['description']} - ₹{parsed['cost']}")
        except HTTPException as he:
            await send_telegram_message(chat_id, f"❌ {he.detail}")
        except Exception as e:
            logger.exception("Webhook handling error")
            await send_telegram_message(chat_id, "❌ Internal error")
    return {"ok": True}

# ----------- Telegram Messaging -----------
async def send_telegram_message(chat_id: str, text: str):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text}
            )
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")

# ----------- Additional Endpoints -----------
@router.post("/api/expense")
async def api_create_expense(expense: ExpenseInput, chat_id: str):
    return await create_splitwise_expense(chat_id, expense.dict())

@router.post("/api/parse")
async def api_parse_expense(payload: ParseInput):
    return {"parsed": await parse_expense_from_text(payload.text)}

@router.post("/api/setup-webhook")
async def setup_telegram_webhook(data: WebhookInput):
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", json={"url": data.url}
            )
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")
        raise HTTPException(status_code=500, detail="Webhook setup failed")

@app.get("/health")
def health():
    return {"status": "ok"}

app.include_router(router)

@app.get("/")
def root():
    return {"status": "Splitwise Telegram MCP running"}
