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
SPLITWISE_CLIENT_ID    = os.getenv("SPLITWISE_CLIENT_ID")    # your Splitwise OAuth2 client id
SPLITWISE_CLIENT_SECRET = os.getenv("SPLITWISE_CLIENT_SECRET")  # your Splitwise OAuth2 client secret
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")            # your OpenAI API key
CALLBACK_BASE_URL         = os.getenv("CALLBACK_BASE_URL")         # e.g. 'https://your-domain.com/auth/splitwise/callback'

# Validate critical env vars
for var_name in ["TELEGRAM_BOT_TOKEN", "SPLITWISE_CLIENT_ID", "SPLITWISE_CLIENT_SECRET", "OPENAI_API_KEY", "CALLBACK_BASE_URL"]:
    if not globals().get(var_name):
        logging.error(f"Missing required environment variable: {var_name}")
        # If missing, the service will still start, but endpoints depending on it will fail.

# Configure OpenAI key
openai.api_key = OPENAI_API_KEY

# Storage files
tokens_file = "user_tokens.json"

# Ensure storage exists
if not os.path.exists(tokens_file):
    with open(tokens_file, 'w') as f:
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

def get_user_token(chat_id: str) -> dict | None:
    tokens = load_json(tokens_file)
    return tokens.get(chat_id)

def set_user_token(chat_id: str, access_token: str, splitwise_id: int, splitwise_name: str):
    tokens = load_json(tokens_file)
    tokens[chat_id] = {
        "access_token": access_token,
        "splitwise_id": splitwise_id,
        "splitwise_name": splitwise_name
    }
    save_json(tokens_file, tokens)

# ----------- Splitwise User Info Helper -----------
async def get_splitwise_current_user(token: str):
    url = "https://secure.splitwise.com/api/v3.0/get_current_user"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        return res.json()["user"]

# ----------- Splitwise OAuth 2.0 Flow -----------
@router.get("/auth/splitwise/start")
async def start_oauth(chat_id: int):
    params = {
        "client_id": SPLITWISE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": f"{CALLBACK_BASE_URL}",
        "scope": "",  # Splitwise does not use scopes, but keep for spec compliance
        "state": str(chat_id)
    }
    auth_url = f"https://secure.splitwise.com/oauth/authorize?{urlencode(params)}"
    return {"auth_url": auth_url}

@router.get("/auth/splitwise/callback")
async def callback_oauth(code: str, state: str):
    chat_id = state  # Use state as chat_id
    token_url = "https://secure.splitwise.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": SPLITWISE_CLIENT_ID,
        "client_secret": SPLITWISE_CLIENT_SECRET,
        "redirect_uri": f"{CALLBACK_BASE_URL}",
        "code": code,
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(token_url, data=data)
        if res.status_code != 200:
            logging.error(f"Splitwise token error: {res.text} | Sent data: {data}")
            raise HTTPException(status_code=502, detail=f"Splitwise token error: {res.text}")
        try:
            token_data = res.json()
        except Exception as e:
            logging.error(f"Could not parse Splitwise token response as JSON: {res.text}")
            raise HTTPException(status_code=502, detail="Splitwise token response not JSON")
    if "access_token" not in token_data:
        logging.error(f"Splitwise token response missing access_token: {token_data}")
        raise HTTPException(status_code=502, detail="Splitwise token response missing access_token")
    # Fetch Splitwise user info
    user_info = await get_splitwise_current_user(token_data["access_token"])
    splitwise_id = user_info["id"]
    splitwise_name = user_info.get("first_name", "Me")
    set_user_token(str(chat_id), token_data["access_token"], splitwise_id, splitwise_name)
    await send_telegram_message(chat_id, "✅ Splitwise account authorized! You can now add expenses.")
    return {"status": "authorized"}

# ----------- Services -----------
async def parse_expense_from_text(text: str, friends: list, self_name: str, self_user_id: int, telegram_name: str = None) -> dict:
    friend_list_str = ", ".join([f"{f['first_name']} (id: {f['id']})" for f in friends])
    self_refs = [self_name]
    if telegram_name and telegram_name != self_name:
        self_refs.append(telegram_name)
    self_refs += ["me", "mine", "self"]
    self_refs_str = ", ".join(self_refs)
    system_message = (
        "You are a financial assistant that extracts structured JSON from natural-language expense messages. "
        "Return ONLY a raw JSON object without markdown formatting or commentary. "
        "Ensure all amounts are numbers, not strings."
    )
    user_message = (
        f'You are the user: {self_name} (id: {self_user_id}).\n'
        f'You may refer to yourself as: {self_refs_str}.\n'
        f'Your friends are: {friend_list_str}.\n'
        f'The user sent this message: "{text}".\n'
        "Your job is to convert it into structured JSON with:\n"
        '- amount: total expense amount (number)\n'
        '- currency: currency code (e.g., "INR")\n'
        '- payer: who paid (name or "me")\n'
        '- participants: a list of objects with:\n'
        '    - name: participant name\n'
        '    - share: amount they owe (number or null if unspecified)\n'
        '- description: a concise, natural-sounding summary (max 4 words, no repetition, no generic phrases like "expense for")\n\n'
        "⚠️ Output ONLY a valid JSON object. No markdown, no extra text.\n"
        "⚠️ Ensure all amounts are numbers, not strings."
    )
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message}
            ]
        )
        content = response.choices[0].message.content
        logging.debug(f"OpenAI response content: {content}")
        content = re.sub(r"^```json\\s*|^```\\s*|```$", "", content.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        return parsed
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
    headers = {"Authorization": f"Bearer {token['access_token']}"}
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

async def get_splitwise_friends(token: str):
    url = "https://secure.splitwise.com/api/v3.0/get_friends"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        return res.json()["friends"]

def match_name_to_user_id(name, friends):
    name = name.lower()
    for friend in friends:
        first = (friend.get("first_name") or "").lower()
        last = (friend.get("last_name") or "").lower()
        if name in first or name in last:
            return friend["id"]
    return None

def normalize_expense(parsed, friends, self_user_id):
    # Map payer and participants names to user_ids
    payer_name = parsed.get("payer")
    participants = parsed.get("participants", [])
    # If payer is 'me', use self_user_id
    if isinstance(payer_name, str) and payer_name.strip().lower() in ["mine", "me", "self"]:
        paid_by = self_user_id
    else:
        paid_by = match_name_to_user_id(payer_name, friends)
    if paid_by is None:
        raise HTTPException(status_code=400, detail=f"Could not match payer name: {payer_name}")
    owed_by = []
    shares = {}
    for part in participants:
        name = part.get("name")
        share = part.get("share")
        if isinstance(name, str) and name.strip().lower() in ["mine", "me", "self"]:
            uid = self_user_id
        else:
            uid = match_name_to_user_id(name, friends)
        if uid is None:
            raise HTTPException(status_code=400, detail=f"Could not match participant name: {name}")
        owed_by.append(uid)
        if share is not None:
            shares[str(uid)] = share
    cost = parsed.get("amount")
    if cost is None:
        raise HTTPException(status_code=400, detail="No amount found in parsed expense")
    return {
        "cost": cost,
        "description": parsed.get("description", ""),
        "paid_by": paid_by,
        "owed_by": owed_by,
        "shares": shares,
        "currency_code": parsed.get("currency", "INR")
    }

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

    # Check Splitwise token before parsing
    token = get_user_token(chat_id)
    if not token:
        await send_telegram_message(chat_id, "❌ User not authorized with Splitwise. Send /start to authorize.")
        return {"ok": True}

    try:
        friends = await get_splitwise_friends(token["access_token"])
        parsed = await parse_expense_from_text(text, friends, token["splitwise_name"], token["splitwise_id"])
        logging.debug(f"Parsed expense: {parsed}")
        normalized = normalize_expense(parsed, friends, token["splitwise_id"])
        logging.debug(f"Normalized expense: {normalized}")
        clarification = await validate_expense_clarity(text, parsed)
        if clarification:
            await send_telegram_message(chat_id, f"❓ {clarification}")
            return {"ok": True}
        res = await create_splitwise_expense(chat_id, normalized)
        logging.debug(f"Splitwise response: {res}")
        await send_telegram_message(chat_id, f"✅ Expense added: {normalized['description']} - ₹{normalized['cost']}")
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
async def api_parse_expense(payload: ParseInput, chat_id: str):
    token = get_user_token(chat_id)
    if not token:
        raise HTTPException(status_code=401, detail="User not authorized with Splitwise")
    friends = await get_splitwise_friends(token["access_token"])
    parsed = await parse_expense_from_text(payload.text, friends, token["splitwise_name"], token["splitwise_id"])
    normalized = normalize_expense(parsed, friends, token["splitwise_id"])
    return {"parsed": normalized}

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

async def validate_expense_clarity(text: str, parsed: dict) -> str | None:
    """Use OpenAI to check if the parsed expense is clear. Return a clarification question if not, else None."""
    prompt = (
        f"The user sent this message: '{text}'.\n"
        f"You parsed it as: {json.dumps(parsed, ensure_ascii=False)}\n"
        "Does this message clearly specify who paid and who owes what? "
        "If yes, reply ONLY with 'OK'. If not, reply with a clarification question to ask the user."
    )
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    content = response.choices[0].message.content.strip()
    if content.upper() == "OK":
        return None
    return content

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
