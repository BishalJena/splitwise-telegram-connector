from fastapi import FastAPI, Request, APIRouter, HTTPException, Query
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
from typing import Optional
from supermemory import Supermemory
import datetime
from fastapi.responses import JSONResponse

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
# In-memory context for pending new friend creation
pending_new_friend = {}

# After load_dotenv()
supermemory_client = Supermemory(api_key=os.environ.get("SUPERMEMORY_API_KEY"))

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
        "You are an expert expense‑splitting assistant. "
        "Users will send you quick, messy notes about group expenses. "
        "Your task is to identify every item, its cost, who had it (or if it was shared), "
        "any percentage discounts (and which items are excluded), "
        "and if a final total is given, adjust shares proportionally so they sum exactly. "
        "If an item is not marked as shared, assign it only to the person(s) mentioned. "
        "If a participant is not mentioned for an item, assume it is shared by all unless context suggests otherwise. "
        "If currency is not specified, default to INR. "
        "Return only a raw JSON object—no markdown, no commentary."
    )
    user_message = (
        f"You are the user {self_name} (id {self_user_id}). "
        f"Your friends are: {friend_list_str}. "
        f"Whenever text refers to me (me, mine, {self_name}, {telegram_name or ''}), map it to {self_name} (id {self_user_id}). "
        f"The user's input was:\n\"{text}\"\n\n"
        "Extract and convert it into structured JSON with:\n"
        "- amount: total bill (number)\n"
        "- currency: currency code (e.g. INR)\n"
        "- payer: who paid\n"
        "- participants: [{ name: ..., share: ... }, …]\n"
        "- description: short summary (max 4 words)\n\n"
        "Rules:\n"
        "1. Find every item and its cost. If an item is shared, split it equally or as specified.\n"
        "2. If an item is not marked as shared, assign it only to the person(s) mentioned.\n"
        "3. If a participant is not mentioned for an item, assume it is shared by all unless context suggests otherwise.\n"
        "4. Detect any % discount and apply only to eligible items; exclude items explicitly noted (e.g. soft drinks).\n"
        "5. If the user states a final total different from the computed sum (due to rounding/tax), distribute the difference proportionally.\n"
        "6. If currency is not specified, default to INR.\n"
        "7. Ensure shares sum exactly to total bill.\n"
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
    data = {
        "cost": expense["cost"],
        "description": expense["description"],
        "currency_code": expense.get("currency_code", "INR")
    }

    # --- Fix: Ensure shares sum exactly to total cost ---
    shares = expense.get("shares", {})
    owed_by = expense["owed_by"]
    cost = float(expense["cost"])
    share_sum = sum(float(shares.get(str(uid), 0)) for uid in owed_by)
    diff = round(cost - share_sum, 2)
    if abs(diff) > 0.01 and owed_by:
        # Adjust the last participant's share
        last_uid = owed_by[-1]
        shares[str(last_uid)] = round(float(shares.get(str(last_uid), 0)) + diff, 2)

    # Handle paid shares and owed shares
    for i, uid in enumerate(owed_by):
        data[f"users__{i}__user_id"] = uid
        # Set paid share
        if uid == expense["paid_by"]:
            data[f"users__{i}__paid_share"] = "{:.2f}".format(float(expense["cost"]))
        else:
            data[f"users__{i}__paid_share"] = "0.00"
        # Get owed share from shares dict or calculate equal split
        share = shares.get(str(uid))
        if share is None:
            share = cost / len(owed_by)
        data[f"users__{i}__owed_share"] = "{:.2f}".format(float(share))

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
    # If name is an int and matches self or a friend, return it
    if isinstance(name, int):
        if self_user_id and name == self_user_id:
            return self_user_id
        for friend in friends:
            if name == friend.get("id"):
                return name
        return None
    # If name is a string, proceed as before
    name = str(name).lower().strip()
    self_names = ["me", "mine", "self", "i"]
    if self_name:
        self_names.append(str(self_name).lower())
    if self_user_id and name in self_names:
        return self_user_id
    if self_name and name == str(self_name).lower():
        return self_user_id
    for friend in friends:
        first = (friend.get("first_name") or "").lower()
        last = (friend.get("last_name") or "").lower()
        if name in first or name in last:
            return friend["id"]
    return None

def normalize_expense(parsed, friends, self_user_id, self_name=None, chat_id=None, allow_fake_id=False):
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
    unknown_friend = None
    for part in participants:
        name = part.get("name")
        share = part.get("share")
        if name is None:
            raise HTTPException(status_code=400, detail="No participant name found in parsed expense. Please specify all participants.")
        uid = match_name_to_user_id(name, friends, self_user_id, self_name)
        if uid is None and not allow_fake_id:
            unknown_friend = name
            break
        if uid is None and allow_fake_id:
            # After confirmation, assign the fake id
            uid = part.get("id", f"new:{name}")
        owed_by.append(uid)
        if share is not None:
            shares[str(uid)] = share
    if unknown_friend and chat_id:
        # Store pending action and raise special exception
        pending_new_friend[chat_id] = {
            "friend_name": unknown_friend,
            "parsed": parsed,
            "friends": friends,
            "self_user_id": self_user_id,
            "self_name": self_name
        }
        raise HTTPException(status_code=409, detail=f"PENDING_NEW_FRIEND::{unknown_friend}")
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

    # Check for pending new friend correction/creation
    if chat_id in pending_new_friend:
        pending = pending_new_friend[chat_id]
        reply = text.strip()
        if reply.lower() in ["no", "n"]:
            await send_telegram_message(chat_id, "Okay, not creating a new friend. Expense cancelled.")
            del pending_new_friend[chat_id]
            return {"ok": True}
        # Check if reply matches an existing friend
        friends = pending["friends"]
        self_user_id = pending["self_user_id"]
        self_name = pending["self_name"]
        matched_id = match_name_to_user_id(reply, friends, self_user_id, self_name)
        parsed = pending["parsed"]
        if matched_id is not None:
            # Replace the unknown friend's name with the matched friend's id in participants
            for part in parsed["participants"]:
                if part["name"] == pending["friend_name"]:
                    part["name"] = reply
            try:
                normalized = normalize_expense(parsed, friends, self_user_id, self_name, allow_fake_id=False)
                res = await create_splitwise_expense(chat_id, normalized)
                await send_telegram_message(chat_id, f"✅ Expense added with corrected friend '{reply}': {normalized['description']} - {format_amount(normalized['cost'], normalized['currency_code'])}")
            except Exception as e:
                await send_telegram_message(chat_id, f"❌ Error adding expense with corrected friend: {e}")
            del pending_new_friend[chat_id]
            return {"ok": True}
        else:
            # Treat as a new friend name, assign fake id
            new_name = reply
            fake_id = f"new:{new_name}"
            for part in parsed["participants"]:
                if part["name"] == pending["friend_name"]:
                    part["name"] = new_name
                    part["id"] = fake_id
            try:
                normalized = normalize_expense(parsed, friends + [{"id": fake_id, "first_name": new_name}], self_user_id, self_name, allow_fake_id=True)
                res = await create_splitwise_expense(chat_id, normalized)
                await send_telegram_message(chat_id, f"✅ New friend '{new_name}' created and expense added: {normalized['description']} - {format_amount(normalized['cost'], normalized['currency_code'])}")
            except Exception as e:
                await send_telegram_message(chat_id, f"❌ Error adding expense with new friend: {e}")
            del pending_new_friend[chat_id]
            return {"ok": True}

    if text.startswith("/start"):
        try:
            resp = await start_oauth(int(chat_id))
            await send_telegram_message(chat_id, f"Authorize here: {resp['auth_url']}")
        except Exception as e:
            logging.exception("Error starting OAuth")
            await send_telegram_message(chat_id, f"❌ OAuth start error: {e}")
        return {"ok": True}

    # Check Splitwise token before proceeding
    token = get_user_token(chat_id)
    if not token:
        await send_telegram_message(chat_id, "❌ User not authorized with Splitwise. Send /start to authorize.")
        return {"ok": True}

    # First check if it's a command
    command_data = parse_command_regex(text)
    if command_data:
        try:
            cmd = command_data["command"]
            if cmd == "show_recent_expenses":
                await handle_show_recent_expenses(chat_id, token)
                return {"ok": True}
            elif cmd == "show_expenses_by_category":
                await handle_show_expenses_by_category(chat_id, token, command_data.get("category"))
                return {"ok": True}
            elif cmd == "show_expenses_with_friend":
                await handle_show_expenses_with_friend(chat_id, token, command_data.get("friend"))
                return {"ok": True}
            elif cmd == "show_balance_with_friend":
                await handle_show_balance_with_friend(chat_id, token, command_data.get("friend"))
                return {"ok": True}
            elif cmd == "show_balances":
                await handle_show_balances(chat_id, token)
                return {"ok": True}
            elif cmd == "delete_expense":
                await handle_delete_expense(chat_id, token, command_data.get("expense_id"))
                return {"ok": True}
            elif cmd == "help":
                await handle_help(chat_id)
                return {"ok": True}
            else:
                await send_telegram_message(chat_id, "❌ Command recognized but not implemented.")
                return {"ok": True}
        except Exception as e:
            logging.exception("Command handling error")
            await send_telegram_message(chat_id, f"❌ Error executing command: {str(e)}")
            return {"ok": True}

    # If not a command, try to parse as an expense
    try:
        friends = await get_splitwise_friends(token["access_token"])
        parsed = await parse_expense_from_text(text, friends, token["splitwise_name"], token["splitwise_id"])
        logging.debug(f"Parsed expense: {parsed}")
        normalized = normalize_expense(parsed, friends, token["splitwise_id"], token["splitwise_name"], chat_id=chat_id)
        logging.debug(f"Normalized expense: {normalized}")
        res = await create_splitwise_expense(chat_id, normalized)
        logging.debug(f"Splitwise response: {res}")
        if 'errors' in res and res['errors']:
            await send_telegram_message(chat_id, f"❌ Failed to add expense: {res['errors']}")
        else:
            # --- Use authoritative split from Splitwise response ---
            expense_obj = None
            user_splits = []
            # Splitwise API may return 'expenses' (list) or just a single expense
            if isinstance(res, dict) and 'expenses' in res and res['expenses']:
                expense_obj = res['expenses'][0]
                user_splits = expense_obj.get('users', [])
            elif isinstance(res, dict) and 'users' in res:
                expense_obj = res
                user_splits = res.get('users', [])
            else:
                # fallback: use normalized
                expense_obj = normalized
                user_splits = []

            # Build a mapping from user_id to name (self + friends)
            user_id_to_name = {str(f["id"]): f.get("first_name", "") for f in friends}
            user_id_to_name[str(token["splitwise_id"])] = token["splitwise_name"]

            split_lines = []
            split_meta = []
            for u in user_splits:
                uid = str(u.get("user_id") or (u.get("user", {}) or {}).get("id"))
                name = user_id_to_name.get(uid, f"User {uid}")
                owed = u.get("owed_share")
                paid = u.get("paid_share")
                split_lines.append(f"{name}: {format_amount(owed, (expense_obj.get('currency_code') or normalized.get('currency_code', 'INR')))}")
                split_meta.append({
                    "user_id": uid,
                    "name": name,
                    "owed_share": owed,
                    "paid_share": paid
                })
            split_details = "\n".join(split_lines)
            msg = f"✅ Expense added: {expense_obj.get('description', normalized.get('description'))} - {format_amount(expense_obj.get('cost', normalized.get('cost')), expense_obj.get('currency_code', normalized.get('currency_code', 'INR')))}"
            if split_details:
                msg += f"\n{split_details}"
            await send_telegram_message(chat_id, msg)
            try:
                supermemory_client.memories.add(
                    content=text,  # The original user message
                    container_tags=[str(chat_id)],
                    metadata={
                        "type": "expense",
                        "content_type": "expense",
                        "description": expense_obj.get("description", normalized.get("description")),
                        "amount": expense_obj.get("cost", normalized.get("cost")),
                        "currency": expense_obj.get("currency_code", normalized.get("currency_code", "INR")),
                        "split": json.dumps(split_meta),
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }
                )
            except Exception as e:
                logging.warning(f"supermemory expense store failed: {e}")
            return {"ok": True}
    except HTTPException as he:
        if he.status_code == 409 and str(he.detail).startswith("PENDING_NEW_FRIEND::"):
            friend_name = str(he.detail).split("::", 1)[1]
            await send_telegram_message(chat_id, f"❌ Could not find anyone named '{friend_name}' in your Splitwise friends. Please reply with the correct friend, a new name to create, or 'no' to cancel.")
            return {"ok": True}
        else:
            logging.warning(f"HTTP error: {he.detail}")
            # --- Store as chat message in supermemory if not a command or expense ---
            try:
                supermemory_client.memories.add(
                    content=text,
                    container_tags=[str(chat_id)],
                    metadata={
                        "type": "chat_message",
                        "content_type": "chat_message",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }
                )
            except Exception as se:
                logging.warning(f"supermemory chat_message store failed: {se}")
            await send_telegram_message(chat_id, f"❌ {he.detail}")
            # Do not return here; proceed to semantic search
    except Exception as e:
        logging.exception("Expense handling error")
        # --- Store as chat message in supermemory if not a command or expense ---
        try:
            supermemory_client.memories.add(
                content=text,
                container_tags=[str(chat_id)],
                metadata={
                    "type": "chat_message",
                    "content_type": "chat_message",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
                }
            )
        except Exception as se:
            logging.warning(f"supermemory chat_message store failed: {se}")
        await send_telegram_message(chat_id, "❌ Error processing expense. Please check your message format.")
        # Do not return here; proceed to semantic search

    # If not a command or expense, treat as a search query
    try:
        results = supermemory_client.search.execute(
            q=text,
            user_id=str(chat_id),
            limit=5,
            rerank=True,
            rewrite_query=True
        )
        if not results.results:
            await send_telegram_message(chat_id, "No relevant results found in your history.")
        else:
            msg_lines = []
            for r in results.results:
                snippet = r.chunks[0].content if r.chunks else ""
                meta = r.metadata or {}
                if meta.get("content_type") == "expense":
                    desc = meta.get("description", "(No description)")
                    amt = meta.get("amount", "?")
                    curr = meta.get("currency", "")
                    msg_lines.append(f"• {desc}: {amt} {curr}")
                else:
                    msg_lines.append(f"• {snippet[:100]}")
            msg = "Here are the most relevant results I found:\n" + "\n".join(msg_lines)
            await send_telegram_message(chat_id, msg)
    except Exception as e:
        logging.warning(f"supermemory search in webhook failed: {e}")
        await send_telegram_message(chat_id, "Sorry, I couldn't search your history due to an error.")
    return {"ok": True}

# ----------- Additional API Endpoints -----------
@router.post("/api/expense")
async def api_create_expense(expense: ExpenseInput, chat_id: str):
    """Create a Splitwise expense via API"""
    return await create_splitwise_expense(chat_id, expense.model_dump())

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

@app.post("/api/search_memories")
async def search_memories(chat_id: str = Query(...), query: str = Query(...), type_filter: str = Query(None)):
    """Search a user's chat and expense history using supermemory semantic search."""
    filters = None
    if type_filter:
        filters = {
            "AND": [
                {"key": "content_type", "value": type_filter, "negate": False}
            ]
        }
    try:
        results = supermemory_client.search.execute(
            q=query,
            user_id=str(chat_id),
            limit=10,
            filters=filters,
            rewrite_query=True,
            rerank=True
        )
        formatted = [
            {
                "id": r.document_id,
                "content": r.chunks[0].content if r.chunks else "",
                "score": r.score,
                "metadata": r.metadata
            }
            for r in results.results
        ]
        return JSONResponse(content={"results": formatted})
    except Exception as e:
        logging.warning(f"supermemory search failed: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

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

def parse_command_regex(text: str) -> Optional[dict]:
    """Parse command patterns from text message"""
    # Direct commands
    if text.startswith("/"):
        command = text[1:].split()[0].lower()
        return {"command": command}

    # Show balances
    if re.match(r"^(show|get)\s+balances?$", text, re.I):
        return {"command": "show_balances"}

    # Show balance with friend
    friend_balance = re.match(r"^(show|get|how\s+much)\s+(balance|do\s+i\s+owe)\s+(?:with\s+)?(\w+)$", text, re.I)
    if friend_balance:
        return {
            "command": "show_balance_with_friend",
            "friend": friend_balance.group(3)
        }

    # Show expenses by category
    category_match = re.match(r"^show\s+(?:me\s+)?(\w+)\s+expenses$", text, re.I)
    if category_match:
        return {
            "command": "show_expenses_by_category",
            "category": category_match.group(1)
        }

    # Delete expense
    if re.match(r"^delete\s+(?:the\s+)?last\s+expense$", text, re.I):
        return {"command": "delete_expense"}

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

# ----------- Currency Formatting -----------
def format_amount(amount, currency_code="INR"):
    try:
        amount = float(amount)
    except Exception:
        return str(amount)
    currency_code = (currency_code or "INR").upper()
    symbol_map = {
        "INR": "₹",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
    }
    symbol = symbol_map.get(currency_code, "")
    if symbol:
        return f"{symbol}{amount:,.2f}"
    else:
        return f"{amount:,.2f} {currency_code}"

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

async def handle_show_balances(chat_id: str, token: dict):
    """Handle showing all balances"""
    friends = await get_splitwise_friends(token["access_token"])
    if not friends:
        await send_telegram_message(chat_id, "No friends found.")
        return

    message = "💰 Your balances:\n\n"
    for friend in friends:
        balance = friend.get("balance", [{}])[0].get("amount", "0")
        currency = friend.get("balance", [{}])[0].get("currency_code", "INR")
        name = f"{friend.get('first_name', '')} {friend.get('last_name', '')}".strip()
        if float(balance) != 0:
            symbol = "🔴" if float(balance) < 0 else "🟢"
            message += f"{symbol} {name}: {format_amount(balance, currency)}\n"
    
    await send_telegram_message(chat_id, message)

async def handle_show_balance_with_friend(chat_id: str, token: dict, friend_name: str):
    """Handle showing balance with specific friend"""
    if not friend_name:
        await send_telegram_message(chat_id, "Please specify a friend's name.")
        return

    friends = await get_splitwise_friends(token["access_token"])
    friend = next((f for f in friends if friend_name.lower() in f["first_name"].lower()), None)
    
    if not friend:
        await send_telegram_message(chat_id, f"Friend '{friend_name}' not found.")
        return

    balance = friend.get("balance", [{}])[0].get("amount", "0")
    currency = friend.get("balance", [{}])[0].get("currency_code", "INR")
    name = f"{friend.get('first_name', '')} {friend.get('last_name', '')}".strip()
    
    if float(balance) == 0:
        message = f"👌 You're all settled with {name}!"
    elif float(balance) < 0:
        message = f"🔴 You owe {name}: {format_amount(abs(float(balance)), currency)}"
    else:
        message = f"🟢 {name} owes you: {format_amount(balance, currency)}"
    
    await send_telegram_message(chat_id, message)

async def handle_show_expenses_by_category(chat_id: str, token: dict, category: str):
    """Handle showing expenses by category"""
    if not category:
        await send_telegram_message(chat_id, "Please specify a category.")
        return

    # Get all categories first
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://secure.splitwise.com/api/v3.0/get_categories",
            headers={"Authorization": f"Bearer {token['access_token']}"}
        )
        res.raise_for_status()
        categories = res.json().get("categories", [])

    # Find matching category
    category_lower = category.lower()
    matched_category = None
    for cat in categories:
        if category_lower in cat["name"].lower():
            matched_category = cat
            break
        for subcat in cat.get("subcategories", []):
            if category_lower in subcat["name"].lower():
                matched_category = subcat
                break
        if matched_category:
            break

    if not matched_category:
        await send_telegram_message(chat_id, f"Category '{category}' not found.")
        return

    # Get expenses for this category
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://secure.splitwise.com/api/v3.0/get_expenses",
            params={"limit": 10, "category_id": matched_category["id"]},
            headers={"Authorization": f"Bearer {token['access_token']}"}
        )
        res.raise_for_status()
        expenses = res.json().get("expenses", [])

    if not expenses:
        await send_telegram_message(chat_id, f"No recent expenses found in category '{matched_category['name']}'.")
        return

    message = f"📊 Recent {matched_category['name']} expenses:\n\n"
    for exp in expenses:
        date = exp.get("date", "").split("T")[0]
        cost = exp.get("cost")
        currency = exp.get("currency_code", "INR")
        message += f"• {date} - {exp.get('description')}: {format_amount(cost, currency)}\n"
    
    await send_telegram_message(chat_id, message)

async def handle_delete_expense(chat_id: str, token: dict, expense_id: Optional[int] = None):
    """Handle deleting an expense"""
    # Get most recent expense if no ID provided
    if not expense_id:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://secure.splitwise.com/api/v3.0/get_expenses",
                params={"limit": 1},
                headers={"Authorization": f"Bearer {token['access_token']}"}
            )
            res.raise_for_status()
            expenses = res.json().get("expenses", [])
            if not expenses:
                await send_telegram_message(chat_id, "No recent expenses found.")
                return
            expense_id = expenses[0]["id"]

    # Delete the expense
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"https://secure.splitwise.com/api/v3.0/delete_expense/{expense_id}",
            headers={"Authorization": f"Bearer {token['access_token']}"}
        )
        res.raise_for_status()
        
    await send_telegram_message(chat_id, "✅ Expense deleted successfully!")

async def handle_help(chat_id: str):
    """Handle help command"""
    help_text = """
🤖 Available commands:

• Create expense:
  "I paid [amount] for [description], split between [names]"
  Example: "I paid 100 for lunch, split between John and me"

• Custom splits:
  - Equal split: "split equally"
  - Ratio split: "1:2:1 split between A, B, C"
  - Exact split: "A pays 30, B pays 20"

• Show balances:
  - "show balances"
  - "how much do I owe [name]"

• Manage expenses:
  - "show [category] expenses"
  - "delete last expense"

• Other commands:
  - /start - Connect Splitwise
  - /help - Show this help
"""
    await send_telegram_message(chat_id, help_text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
