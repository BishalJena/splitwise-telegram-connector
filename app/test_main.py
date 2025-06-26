import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import json
import os
from app.main import app

client = TestClient(app)

# Test Data
TEST_CHAT_ID = "123456789"
TEST_TOKEN = "test_token"
TEST_SPLITWISE_ID = 12345
TEST_SPLITWISE_NAME = "TestUser"

@pytest.fixture
def mock_token_storage():
    """Mock token storage for tests"""
    tokens = {
        TEST_CHAT_ID: {
            "access_token": TEST_TOKEN,
            "splitwise_id": TEST_SPLITWISE_ID,
            "splitwise_name": TEST_SPLITWISE_NAME
        }
    }
    with patch('app.main.load_json', return_value=tokens):
        yield tokens

@pytest.fixture
def mock_splitwise_friends():
    """Mock Splitwise friends data"""
    return {
        "friends": [
            {
                "id": 67890,
                "first_name": "John",
                "last_name": "Doe",
                "balance": [{"amount": "-100.0", "currency_code": "INR"}]
            },
            {
                "id": 67891,
                "first_name": "Alice",
                "last_name": "Smith",
                "balance": [{"amount": "200.0", "currency_code": "INR"}]
            }
        ]
    }

@pytest.mark.asyncio
async def test_health_check():
    """Test health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_root_endpoint():
    """Test root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "running"}

@pytest.mark.asyncio
async def test_oauth_start():
    """Test OAuth start endpoint"""
    response = client.get(f"/auth/splitwise/start?chat_id={TEST_CHAT_ID}")
    assert response.status_code == 200
    assert "auth_url" in response.json()
    auth_url = response.json()["auth_url"]
    assert "secure.splitwise.com/oauth/authorize" in auth_url
    assert f"state={TEST_CHAT_ID}" in auth_url

@pytest.mark.asyncio
async def test_oauth_callback_success():
    """Test successful OAuth callback"""
    mock_token_response = {
        "access_token": "new_test_token",
        "token_type": "Bearer"
    }
    mock_user_response = {
        "user": {
            "id": TEST_SPLITWISE_ID,
            "first_name": TEST_SPLITWISE_NAME
        }
    }
    with patch('httpx.AsyncClient.post', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_token_response
    )), patch('httpx.AsyncClient.get', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_user_response
    )):
        response = client.get(f"/auth/splitwise/callback?code=test_code&state={TEST_CHAT_ID}")
        assert response.status_code == 200
        assert response.json() == {"status": "authorized"}

@pytest.mark.asyncio
async def test_oauth_callback_failure():
    """Test failed OAuth callback"""
    with patch('httpx.AsyncClient.post', return_value=MagicMock(
        status_code=400,
        text="Invalid code"
    )):
        response = client.get(f"/auth/splitwise/callback?code=invalid_code&state={TEST_CHAT_ID}")
        assert response.status_code == 502

@pytest.mark.asyncio
async def test_webhook_setup():
    """Test webhook setup endpoint"""
    mock_response = {"ok": True, "result": True}
    with patch('httpx.AsyncClient.post', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_response
    )):
        response = client.post("/api/setup-webhook", json={"url": "https://test.com/webhook"})
        assert response.status_code == 200
        assert response.json() == mock_response

@pytest.mark.asyncio
async def test_unauthorized_expense(mock_token_storage):
    """Test expense creation without authorization"""
    webhook_data = {
        "message": {
            "chat": {"id": "999999"},  # Unauthorized chat_id
            "text": "paid 100 for lunch"
        }
    }
    response = client.post("/telegram/webhook", json=webhook_data)
    assert response.status_code == 200

@pytest.mark.asyncio
async def test_simple_expense_creation(mock_token_storage, mock_splitwise_friends):
    """Test creating a simple expense"""
    with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
        with patch('app.main.create_splitwise_expense') as mock_create:
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 500 for lunch"
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            assert call_args[0] == TEST_CHAT_ID
            assert "lunch" in call_args[1]["description"].lower()
            assert call_args[1]["cost"] == 500

@pytest.mark.asyncio
async def test_multi_person_expense(mock_token_storage, mock_splitwise_friends):
    """Test creating an expense with multiple people"""
    with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
        with patch('app.main.create_splitwise_expense') as mock_create:
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 900 for dinner with John and Alice"
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            expense_data = call_args[1]
            assert expense_data["cost"] == 900
            assert "dinner" in expense_data["description"].lower()
            assert len(expense_data["owed_by"]) == 3  # Including self
            assert 67890 in expense_data["owed_by"]  # John
            assert 67891 in expense_data["owed_by"]  # Alice

@pytest.mark.asyncio
async def test_specific_split_expense(mock_token_storage, mock_splitwise_friends):
    """Test creating an expense with specific splits"""
    with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
        with patch('app.main.create_splitwise_expense') as mock_create:
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 1000 for rent, John owes 600"
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            expense_data = call_args[1]
            assert expense_data["cost"] == 1000
            assert "rent" in expense_data["description"].lower()
            assert str(67890) in expense_data["shares"]  # John's share
            assert expense_data["shares"][str(67890)] == 600

@pytest.mark.asyncio
async def test_show_balances(mock_token_storage, mock_splitwise_friends):
    """Test showing user balances"""
    with patch('httpx.AsyncClient.get', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_splitwise_friends
    )), patch('app.main.send_telegram_message') as mock_send, \
       patch('app.main.parse_command_regex', return_value={"command": "show_balances"}):
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "show balances"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "balance" in call_args[1].lower()

@pytest.mark.asyncio
async def test_show_friend_balance(mock_token_storage, mock_splitwise_friends):
    """Test showing balance with specific friend"""
    with patch('httpx.AsyncClient.get', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_splitwise_friends
    )), patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "how much do i owe John"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "John" in call_args[1]

@pytest.mark.asyncio
async def test_delete_last_expense(mock_token_storage):
    """Test deleting the last expense"""
    mock_expense = {
        "expenses": [{
            "id": 12345,
            "description": "Test expense",
            "cost": "100.0"
        }]
    }
    with patch('httpx.AsyncClient.get', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_expense
    )), patch('httpx.AsyncClient.post', return_value=MagicMock(
        status_code=200,
        json=lambda: {"success": True}
    )), patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "delete last expense"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "deleted" in call_args[1].lower()

@pytest.mark.asyncio
async def test_show_category_expenses(mock_token_storage):
    """Test showing expenses by category"""
    mock_categories = {
        "categories": [{
            "name": "Food & Drink",
            "id": 1,
            "subcategories": [{
                "name": "Food",
                "id": 101
            }]
        }]
    }
    mock_expenses = {
        "expenses": [{
            "description": "Lunch",
            "cost": "100.0",
            "currency_code": "INR",
            "date": "2024-01-01"
        }]
    }
    with patch('httpx.AsyncClient.get', side_effect=[
        MagicMock(status_code=200, json=lambda: mock_categories),
        MagicMock(status_code=200, json=lambda: mock_expenses)
    ]), patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "show me food expenses"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "food" in call_args[1].lower()

@pytest.mark.asyncio
async def test_invalid_category(mock_token_storage):
    """Test showing expenses for invalid category"""
    mock_categories = {
        "categories": [{
            "name": "Food & Drink",
            "id": 1,
            "subcategories": [{
                "name": "Food",
                "id": 101
            }]
        }]
    }
    with patch('httpx.AsyncClient.get', return_value=MagicMock(
        status_code=200,
        json=lambda: mock_categories
    )), patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "show me xyz expenses"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "not found" in call_args[1].lower()

@pytest.mark.asyncio
async def test_help_command():
    """Test help command"""
    with patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": TEST_CHAT_ID},
                "text": "/help"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        assert "available commands" in call_args[1].lower()

def valid_token():
    return {
        "access_token": "test_token",
        "splitwise_id": 12345,
        "splitwise_name": "TestUser"
    }

def test_chat_message_stored_in_supermemory():
    with patch('app.main.get_user_token', return_value=valid_token()):
        with patch('app.main.get_splitwise_friends', return_value=[]):
            with patch('app.main.supermemory_client.memories.add') as mock_add:
                webhook_data = {
                    "message": {
                        "chat": {"id": "12345"},
                        "text": "hello, this is a test"
                    }
                }
                response = client.post("/telegram/webhook", json=webhook_data)
                assert response.status_code == 200
                assert mock_add.called
                call_args = mock_add.call_args[1]
                assert call_args['content'] == "hello, this is a test"
                assert call_args['container_tags'] == ["12345"]
                assert call_args['metadata']['type'] == "chat_message"
                assert call_args['metadata']['content_type'] == "chat_message"
                assert "timestamp" in call_args['metadata']

def test_expense_stored_in_supermemory(mock_splitwise_friends):
    with patch('app.main.get_user_token', return_value=valid_token()):
        with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
            # Mock Splitwise API response with authoritative split
            authoritative_expense = {
                "expenses": [
                    {
                        "cost": "500.00",
                        "description": "Lunch",
                        "currency_code": "INR",
                        "users": [
                            {"user_id": 12345, "paid_share": "500.00", "owed_share": "250.00", "user": {"id": 12345, "first_name": "TestUser"}},
                            {"user_id": 67890, "paid_share": "0.00", "owed_share": "150.00", "user": {"id": 67890, "first_name": "John"}},
                            {"user_id": 67891, "paid_share": "0.00", "owed_share": "100.00", "user": {"id": 67891, "first_name": "Alice"}},
                        ]
                    }
                ]
            }
            with patch('app.main.create_splitwise_expense', return_value=authoritative_expense) as mock_create_exp, \
                 patch('app.main.supermemory_client.memories.add') as mock_add_mem, \
                 patch('app.main.send_telegram_message') as mock_send:
                webhook_data = {
                    "message": {
                        "chat": {"id": "12345"},
                        "text": "paid 500 for lunch"
                    }
                }
                response = client.post("/telegram/webhook", json=webhook_data)
                assert response.status_code == 200
                assert mock_add_mem.called
                expense_call = [c for c in mock_add_mem.call_args_list if c[1].get('metadata', {}).get('content_type') == 'expense']
                assert expense_call, "No expense memory stored"
                args, kwargs = expense_call[0]
                assert kwargs['container_tags'] == ["12345"]
                assert kwargs['metadata']['type'] == "expense"
                assert kwargs['metadata']['description']
                assert kwargs['metadata']['amount']
                # Check authoritative split (now a JSON string)
                split_json = kwargs['metadata'].get('split')
                assert split_json is not None and isinstance(split_json, str)
                split = json.loads(split_json)
                # Check that all users are present and shares match
                expected = {
                    12345: ("TestUser", "250.00", "500.00"),
                    67890: ("John", "150.00", "0.00"),
                    67891: ("Alice", "100.00", "0.00"),
                }
                for entry in split:
                    uid = int(entry['user_id'])
                    assert uid in expected
                    name, owed, paid = expected[uid]
                    assert entry['name'] == name
                    assert entry['owed_share'] == owed
                    assert entry['paid_share'] == paid
                # Check confirmation message includes authoritative split
                assert mock_send.called
                sent_msg = "".join([str(call[0][1]) for call in mock_send.call_args_list])
                for name, owed, _ in expected.values():
                    assert name in sent_msg
                    assert owed in sent_msg

def test_search_query_returns_results():
    with patch('app.main.get_user_token', return_value=valid_token()):
        with patch('app.main.get_splitwise_friends', return_value=[]):
            with patch('app.main.supermemory_client.search.execute') as mock_search, \
                 patch('app.main.send_telegram_message') as mock_send:
                mock_search.return_value.results = [
                    type('Result', (), {
                        'chunks': [type('Chunk', (), {'content': 'pizza expense'})],
                        'metadata': {'content_type': 'expense', 'description': 'pizza', 'amount': 10, 'currency': 'USD'},
                        'score': 0.95,
                        'document_id': 'doc1'
                    })()
                ]
                webhook_data = {
                    "message": {
                        "chat": {"id": "12345"},
                        "text": "pizza"
                    }
                }
                response = client.post("/telegram/webhook", json=webhook_data)
                assert response.status_code == 200
                mock_send.assert_called()
                found = any("pizza" in call[0][1] for call in mock_send.call_args_list)
                assert found

def test_invalid_expense_format(mock_token_storage, mock_splitwise_friends):
    with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
        with patch('app.main.send_telegram_message') as mock_send:
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "lunch with John"  # Missing amount
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            # Allow for multiple calls, check expected message in one
            found = any("splitwise error" in call[0][1].lower() for call in mock_send.call_args_list)
            assert found

def test_expense_with_unknown_friend(mock_token_storage, mock_splitwise_friends):
    friends_initial = mock_splitwise_friends["friends"]
    new_friend = {"id": "new:Charlie", "first_name": "Charlie", "last_name": "", "balance": [{"amount": "0", "currency_code": "INR"}]}
    friends_with_new = friends_initial + [new_friend]
    parsed_expense = {
        "amount": 500,
        "description": "lunch",
        "payer": "me",
        "participants": [
            {"name": "John", "share": 250},
            {"name": "Bobb", "share": 250}
        ]
    }
    with patch('app.main.get_splitwise_friends', side_effect=[friends_initial, friends_initial, friends_with_new, friends_initial]):
        with patch('app.main.send_telegram_message') as mock_send, \
             patch('app.main.pending_new_friend', {}):
            # Step 1: Send expense with misspelled friend (should trigger unknown friend logic)
            with patch('app.main.parse_expense_from_text', return_value=parsed_expense):
                webhook_data = {
                    "message": {
                        "chat": {"id": TEST_CHAT_ID},
                        "text": "paid 500 for lunch with Bobb"  # Misspelled friend
                    }
                }
                response = client.post("/telegram/webhook", json=webhook_data)
                assert response.status_code == 200
                mock_send.assert_called()
                found = any(
                    ("please reply with the correct friend" in call[0][1].lower()) or
                    ("could not find anyone named" in call[0][1].lower()) or
                    ("splitwise error" in call[0][1].lower())
                    for call in mock_send.call_args_list
                )
                if not found:
                    print("Sent messages:", [call[0][1] for call in mock_send.call_args_list])
                assert found
            # Step 2: Simulate user replying with a new friend name (should allow expense creation)
            with patch('app.main.create_splitwise_expense') as mock_create, \
                 patch('app.main.parse_expense_from_text', return_value=parsed_expense):
                webhook_data2 = {
                    "message": {
                        "chat": {"id": TEST_CHAT_ID},
                        "text": "Charlie"
                    }
                }
                response2 = client.post("/telegram/webhook", json=webhook_data2)
                assert response2.status_code == 200
                mock_create.assert_called()

@pytest.mark.asyncio
async def test_expense_api_endpoint(mock_token_storage):
    """Test direct expense API endpoint"""
    with patch('app.main.create_splitwise_expense', return_value={"success": True}):
        expense_data = {
            "cost": 100.0,
            "description": "Test API expense",
            "paid_by": TEST_SPLITWISE_ID,
            "owed_by": [TEST_SPLITWISE_ID, 67890]
        }
        response = client.post(f"/api/expense?chat_id={TEST_CHAT_ID}", json=expense_data)
        assert response.status_code == 200
        assert response.json() == {"success": True}

@pytest.mark.asyncio
async def test_parse_api_endpoint(mock_token_storage, mock_splitwise_friends):
    """Test expense parsing API endpoint"""
    with patch('app.main.get_splitwise_friends', return_value=mock_splitwise_friends["friends"]):
        with patch('app.main.parse_expense_from_text', return_value={
            "amount": 100,
            "description": "lunch",
            "payer": "me",
            "participants": [
                {"name": "John", "share": None}
            ]
        }):
            payload = {"text": "paid 100 for lunch with John"}
            response = client.post(f"/api/parse?chat_id={TEST_CHAT_ID}", json=payload)
            assert response.status_code == 200
            parsed = response.json()["parsed"]
            assert "cost" in parsed
            assert "description" in parsed
            assert "owed_by" in parsed
            assert parsed["cost"] == 100
            assert "lunch" in parsed["description"].lower()
            assert TEST_SPLITWISE_ID in parsed["owed_by"]  # Self is included
            assert 67890 in parsed["owed_by"]  # John's ID

@pytest.mark.asyncio
async def test_complex_expense_parsing(mock_token_storage):
    """Test parsing a complex, real-world expense note with items, discounts, exclusions, and final total."""
    complex_text = (
        "I paid 674 for dinner. Manan and Siddham were there.  "
        "Cold drinks were 95 total, 40 each to manan and siddham, 15 mine — no discount on those.  "
        "I had chicken biryani for 120. They had 150 combo meals each.  "
        "Also had veg manchurian (120) and mushroom (99), we shared those three-way.  "
        "10% student discount on food only, not drinks.  "
        "Total after all is 674."
    )
    # Mock friends list to include Manan and Siddham
    friends = [
        {"id": 111, "first_name": "Manan", "last_name": "", "balance": [{"amount": "0", "currency_code": "INR"}]},
        {"id": 222, "first_name": "Siddham", "last_name": "", "balance": [{"amount": "0", "currency_code": "INR"}]}
    ]
    with patch('app.main.get_splitwise_friends', return_value=friends):
        with patch('app.main.create_splitwise_expense') as mock_create:
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": complex_text
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            expense_data = call_args[1]
            # Check that the total cost matches
            assert expense_data["cost"] == 674
            # Check that all participants are present
            assert set(expense_data["owed_by"]) == {TEST_SPLITWISE_ID, 111, 222}
            # Check that the shares sum to the total
            assert abs(sum(expense_data["shares"].values()) - 674) < 0.01
            # Check that the description is short
            assert len(expense_data["description"].split()) <= 4

def test_supermemory_error_handling(mock_token_storage):
    with patch('app.main.supermemory_client.memories.add', side_effect=Exception("fail")) as mock_add, \
         patch('app.main.send_telegram_message') as mock_send:
        webhook_data = {
            "message": {
                "chat": {"id": "12345"},
                "text": "hello, this is a test"
            }
        }
        response = client.post("/telegram/webhook", json=webhook_data)
        assert response.status_code == 200
        # Should still send a message to the user if not authorized, otherwise not
        # Accept either 0 or 1 calls depending on auth logic
        assert mock_send.call_count in (0, 1)