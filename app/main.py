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
import asyncio

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

# Add at the top with other global variables
pending_expenses = {}

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
    print(f"Saved token for {chat_id}: {tokens[chat_id]}")

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
    self_refs = [self_name, "me", "mine", "self", "I"]
    if telegram_name and telegram_name != self_name:
        self_refs.append(telegram_name)
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
        f'Whenever the message refers to any of {self_refs_str}, always use "{self_name}" (id: {self_user_id}) in the output.\n'
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

def match_name_to_user_id(name, friends, self_user_id=None, self_name=None):
    name = name.lower().strip()
    self_names = ["me", "mine", "self", "i"]
    if self_name:
        self_names.append(self_name.lower())
    if self_user_id and name in self_names:
        return self_user_id
    if self_name and name == self_name.lower():
        return self_user_id
    for friend in friends:
        first = (friend.get("first_name") or "").lower()
        last = (friend.get("last_name") or "").lower()
        if name in first or name in last:
            return friend["id"]
    return None

def normalize_expense(parsed, friends, self_user_id, self_name=None):
    # Defensive: check for empty or missing fields
    if not parsed or not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Parsing failed: No data returned from model. Please rephrase your message.")
    payer_name = parsed.get("payer")
    participants = parsed.get("participants", [])
    if payer_name is None:
        raise HTTPException(status_code=400, detail="No payer found in parsed expense. Please specify who paid.")
    # If payer is 'me', use self_user_id
    paid_by = match_name_to_user_id(payer_name, friends, self_user_id, self_name)
    if paid_by is None:
        raise HTTPException(status_code=400, detail=f"Could not match payer name: {payer_name}")
    owed_by = []
    shares = {}
    for part in participants:
        name = part.get("name")
        share = part.get("share")
        if name is None:
            raise HTTPException(status_code=400, detail="No participant name found in parsed expense. Please specify all participants.")
        uid = match_name_to_user_id(name, friends, self_user_id, self_name)
        if uid is None:
            raise HTTPException(status_code=400, detail=f"Could not match participant name: {name}")
        owed_by.append(uid)
        if share is not None:
            shares[str(uid)] = share
    cost = parsed.get("amount")
    if cost is None:
        raise HTTPException(status_code=400, detail="No amount found in parsed expense. Please specify the amount.")
    # --- Fix: Ensure payer is in participants and shares sum to cost ---
    if self_user_id not in owed_by:
        # Add self as participant with remaining share
        remaining = cost - sum(shares.values())
        if remaining < 0:
            raise HTTPException(status_code=400, detail="Participant shares exceed total cost. Please check your message.")
        owed_by.append(self_user_id)
        shares[str(self_user_id)] = remaining
    else:
        # If self is already in, check if shares sum to cost
        total_shares = sum(shares.values())
        if abs(total_shares - cost) > 0.01:
            # Adjust self's share to make total match cost
            diff = cost - total_shares
            shares[str(self_user_id)] = shares.get(str(self_user_id), 0) + diff
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

    # --- Hybrid Command Parsing ---
    parsed = parse_command_regex(text)
    if parsed:
        vetted = await vet_command_with_llm(text, parsed)
        if vetted.get("command") and vetted["command"] != "unknown":
            cmd = vetted["command"]
            if cmd == "show_recent_expenses":
                await handle_show_recent_expenses(chat_id, token)
            elif cmd == "show_expenses_by_category":
                await handle_show_expenses_by_category(chat_id, token, vetted.get("category"))
            elif cmd == "show_expenses_with_friend":
                await handle_show_expenses_with_friend(chat_id, token, vetted.get("friend"))
            elif cmd == "show_balance_with_friend":
                await handle_show_balance_with_friend(chat_id, token, vetted.get("friend"))
            elif cmd == "show_balances":
                await handle_show_balances(chat_id, token)
            elif cmd == "delete_expense":
                await handle_delete_expense(chat_id, token, vetted.get("expense_id"))
            elif cmd == "help":
                await handle_help(chat_id)
            else:
                await send_telegram_message(chat_id, "❌ Command recognized but not implemented.")
            return {"ok": True}
        elif vetted.get("command") == "unknown":
            await send_telegram_message(chat_id, "❌ Sorry, I couldn't understand your command. Please try again or use /help.")
            return {"ok": True}
    try:
        friends = await get_splitwise_friends(token["access_token"])
        parsed = await parse_expense_from_text(text, friends, token["splitwise_name"], token["splitwise_id"])
        logging.debug(f"Parsed expense: {parsed}")
        normalized = normalize_expense(parsed, friends, token["splitwise_id"], token["splitwise_name"])
        logging.debug(f"Normalized expense: {normalized}")
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
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": f'The user sent this message: \'{text}\'.\nYou parsed it as: {json.dumps(parsed)}\nDoes this message clearly specify who paid and who owes what? If yes, reply ONLY with \'OK\'. If not, reply with a clarification question to ask the user.'}
            ]
        )
        content = response.choices[0].message.content.strip()
        if content == "OK":
            return None
        return content
    except Exception as e:
        logging.error(f"Clarity validation error: {e}")
        return None

def parse_command_regex(text):
    text = text.strip().lower()
    # 1. Show recent expenses
    if re.match(r"show (me )?recent expenses", text):
        return {"command": "show_recent_expenses"}
    # 2. Show expenses by category
    m = re.match(r"show (me )?(?P<category>\w+) expenses", text)
    if m:
        return {"command": "show_expenses_by_category", "category": m.group("category")}
    # 3. Show expenses with a friend
    m = re.match(r"show (me )?expenses with (?P<friend>[\w ]+)", text)
    if m:
        return {"command": "show_expenses_with_friend", "friend": m.group("friend").strip()}
    # 4. Show balance with a friend
    m = re.match(r"how much do i owe (?P<friend>[\w ]+)", text)
    if m:
        return {"command": "show_balance_with_friend", "friend": m.group("friend").strip()}
    # 5. Show total owed/owing
    if re.match(r"show (me )?my balances", text):
        return {"command": "show_balances"}
    # 6. Delete an expense
    m = re.match(r"delete expense #(\d+)", text)
    if m:
        return {"command": "delete_expense", "expense_id": int(m.group(1))}
    if re.match(r"delete last expense", text):
        return {"command": "delete_expense", "expense_id": None}
    # 8. Help
    if text.startswith("/help") or text == "help":
        return {"command": "help"}
    return None

async def vet_command_with_llm(text, parsed_command):
    prompt = (
        f'The user sent: "{text}".\n'
        f'My regex parser thinks this means: {json.dumps(parsed_command)}.\n'
        'Is this correct? If yes, reply ONLY with the JSON object. '
        'If not, reply with the correct command and arguments as a JSON object, '
        'or reply with {"command": "unknown"} if you can\'t tell.'
    )
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    response = await asyncio.to_thread(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
    )
    content = response.choices[0].message.content.strip()
    try:
        return json.loads(content)
    except Exception:
        return {"command": "unknown"}

# --- Command Handlers (stubs) ---
async def handle_show_recent_expenses(chat_id, token):
    url = "https://secure.splitwise.com/api/v3.0/get_expenses"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    params = {"limit": 5}
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, params=params)
            res.raise_for_status()
            data = res.json()
            expenses = data.get("expenses", [])
            if not expenses:
                await send_telegram_message(chat_id, "No recent expenses found.")
                return
            msg_lines = []
            for e in expenses:
                desc = e.get("description", "(No description)")
                cost = float(e.get("cost", "0"))
                currency = e.get("currency_code", "")
                date = e.get("date", "")[:10]
                msg_lines.append(f"{desc} | {cost:.2f} {currency} | {date}")
            msg = "Recent expenses:\n" + "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Failed to fetch recent expenses: {e}")

async def handle_show_expenses_by_category(chat_id, token, category):
    cat_url = "https://secure.splitwise.com/api/v3.0/get_categories"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    try:
        async with httpx.AsyncClient() as client:
            cat_res = await client.get(cat_url, headers=headers)
            cat_res.raise_for_status()
            categories = cat_res.json().get("categories", [])
            subcats = []
            for parent in categories:
                for sub in parent.get("subcategories", []):
                    subcats.append(sub)
            cat_name = category.lower()
            matches = [sub for sub in subcats if cat_name in sub.get("name", "").lower()]
            if not matches:
                await send_telegram_message(chat_id, f"❌ Category '{category}' not found.")
                return
            if len(matches) > 1:
                names = ', '.join([sub['name'] for sub in matches])
                await send_telegram_message(chat_id, f"Multiple categories match '{category}': {names}. Please be more specific.")
                return
            cat_id = matches[0]["id"]
            cat_label = matches[0]["name"]
            exp_url = "https://secure.splitwise.com/api/v3.0/get_expenses"
            params = {"category_id": cat_id, "limit": 5}
            exp_res = await client.get(exp_url, headers=headers, params=params)
            exp_res.raise_for_status()
            expenses = exp_res.json().get("expenses", [])
            if not expenses:
                await send_telegram_message(chat_id, f"No recent expenses found for category '{cat_label}'.")
                return
            msg_lines = []
            for e in expenses:
                desc = e.get("description", "(No description)")
                cost = float(e.get("cost", "0"))
                currency = e.get("currency_code", "")
                date = e.get("date", "")[:10]
                msg_lines.append(f"{desc} | {cost:.2f} {currency} | {date}")
            msg = f"Recent expenses for '{cat_label}':\n" + "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Failed to fetch expenses for category '{category}': {e}")

async def handle_show_expenses_with_friend(chat_id, token, friend):
    friends_url = "https://secure.splitwise.com/api/v3.0/get_friends"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    try:
        async with httpx.AsyncClient() as client:
            friends_res = await client.get(friends_url, headers=headers)
            friends_res.raise_for_status()
            friends = friends_res.json().get("friends", [])
            friend_name = friend.lower()
            matches = [f for f in friends if friend_name in (f.get("first_name") or "").lower() or friend_name in (f.get("last_name") or "").lower()]
            if not matches:
                await send_telegram_message(chat_id, f"❌ Friend '{friend}' not found.")
                return
            if len(matches) > 1:
                names = ', '.join([f.get('first_name', '') for f in matches])
                await send_telegram_message(chat_id, f"Multiple friends match '{friend}': {names}. Please be more specific.")
                return
            friend_id = matches[0]["id"]
            friend_label = matches[0].get("first_name", "")
            exp_url = "https://secure.splitwise.com/api/v3.0/get_expenses"
            params = {"friend_id": friend_id, "limit": 5}
            exp_res = await client.get(exp_url, headers=headers, params=params)
            exp_res.raise_for_status()
            expenses = exp_res.json().get("expenses", [])
            if not expenses:
                await send_telegram_message(chat_id, f"No recent expenses found with '{friend_label}'.")
                return
            msg_lines = []
            for e in expenses:
                desc = e.get("description", "(No description)")
                cost = float(e.get("cost", "0"))
                currency = e.get("currency_code", "")
                date = e.get("date", "")[:10]
                msg_lines.append(f"{desc} | {cost:.2f} {currency} | {date}")
            msg = f"Recent expenses with '{friend_label}':\n" + "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Failed to fetch expenses with '{friend}': {e}")

async def handle_show_balance_with_friend(chat_id, token, friend):
    friends_url = "https://secure.splitwise.com/api/v3.0/get_friends"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    try:
        async with httpx.AsyncClient() as client:
            friends_res = await client.get(friends_url, headers=headers)
            friends_res.raise_for_status()
            friends = friends_res.json().get("friends", [])
            friend_name = friend.lower()
            matches = [f for f in friends if friend_name in (f.get("first_name") or "").lower() or friend_name in (f.get("last_name") or "").lower()]
            if not matches:
                await send_telegram_message(chat_id, f"❌ Friend '{friend}' not found.")
                return
            if len(matches) > 1:
                names = ', '.join([f.get('first_name', '') for f in matches])
                await send_telegram_message(chat_id, f"Multiple friends match '{friend}': {names}. Please be more specific.")
                return
            friend_obj = matches[0]
            balances = friend_obj.get("balance", [])
            if not balances:
                await send_telegram_message(chat_id, f"No balance found with '{friend_obj.get('first_name','')}'.")
                return
            msg_lines = []
            for b in balances:
                amount = float(b.get("amount", "0"))
                currency = b.get("currency_code", "")
                if amount > 0:
                    msg_lines.append(f"You are owed {amount:.2f} {currency} by {friend_obj.get('first_name','')}.")
                elif amount < 0:
                    msg_lines.append(f"You owe {abs(amount):.2f} {currency} to {friend_obj.get('first_name','')}.")
                else:
                    msg_lines.append(f"You and {friend_obj.get('first_name','')} are settled up in {currency}.")
            msg = "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Failed to fetch balance with '{friend}': {e}")

async def handle_show_balances(chat_id, token):
    friends_url = "https://secure.splitwise.com/api/v3.0/get_friends"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    try:
        async with httpx.AsyncClient() as client:
            friends_res = await client.get(friends_url, headers=headers)
            friends_res.raise_for_status()
            friends = friends_res.json().get("friends", [])
            totals_owed = {}
            totals_owes = {}
            for f in friends:
                for b in f.get("balance", []):
                    amount = float(b.get("amount", "0"))
                    currency = b.get("currency_code", "")
                    if amount > 0:
                        totals_owed[currency] = totals_owed.get(currency, 0) + amount
                    elif amount < 0:
                        totals_owes[currency] = totals_owes.get(currency, 0) + abs(amount)
            msg_lines = []
            if totals_owed:
                for currency, amt in totals_owed.items():
                    msg_lines.append(f"You are owed {amt:.2f} {currency}")
            if totals_owes:
                for currency, amt in totals_owes.items():
                    msg_lines.append(f"You owe {amt:.2f} {currency}")
            if not msg_lines:
                msg_lines.append("You are all settled up with everyone!")
            msg = "Your balances summary:\n" + "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Failed to fetch balances summary: {e}")

async def handle_delete_expense(chat_id, token, expense_id):
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    try:
        async with httpx.AsyncClient() as client:
            if expense_id is None:
                exp_url = "https://secure.splitwise.com/api/v3.0/get_expenses"
                exp_res = await client.get(exp_url, headers=headers, params={"limit": 1})
                exp_res.raise_for_status()
                expenses = exp_res.json().get("expenses", [])
                if not expenses:
                    await send_telegram_message(chat_id, "No expenses found to delete.")
                    return
                expense_id = expenses[0].get("id")
                if not expense_id:
                    await send_telegram_message(chat_id, "Could not determine expense ID to delete.")
                    return
            del_url = f"https://secure.splitwise.com/api/v3.0/delete_expense/{expense_id}"
            del_res = await client.post(del_url, headers=headers)
            del_res.raise_for_status()
            result = del_res.json()
            if result.get("success"):
                await send_telegram_message(chat_id, f"✅ Expense #{expense_id} deleted.")
            else:
                await send_telegram_message(chat_id, f"❌ Failed to delete expense #{expense_id}.")
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Error deleting expense: {e}")

async def handle_help(chat_id):
    help_text = (
        "Available commands:\n"
        "- show me recent expenses\n"
        "- show me <category> expenses\n"
        "- show expenses with <friend>\n"
        "- how much do I owe <friend>\n"
        "- show my balances\n"
        "- delete expense #<id>\n"
        "- delete last expense\n"
        "- /help\n"
        "Or just send an expense description to add it!"
    )
    await send_telegram_message(chat_id, help_text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
