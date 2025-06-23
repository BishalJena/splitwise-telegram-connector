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

@pytest.mark.asyncio
async def test_invalid_expense_format(mock_token_storage, mock_splitwise_friends):
    """Test expense with invalid format"""
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
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0]
            assert any(msg in call_args[1].lower() for msg in ["no amount found", "invalid format", "splitwise error"])

@pytest.mark.asyncio
async def test_expense_with_unknown_friend(mock_token_storage, mock_splitwise_friends):
    """Test expense with unknown friend triggers correction/creation prompt and handles all reply types."""
    friends_initial = mock_splitwise_friends["friends"]
    new_friend = {"id": "new:Charlie", "first_name": "Charlie", "last_name": "", "balance": [{"amount": "0", "currency_code": "INR"}]}
    friends_with_new = friends_initial + [new_friend]
    with patch('app.main.get_splitwise_friends', side_effect=[friends_initial, friends_initial, friends_with_new, friends_initial]):
        with patch('app.main.send_telegram_message') as mock_send, \
             patch('app.main.create_splitwise_expense') as mock_create:
            # Step 1: Send expense with misspelled friend
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 500 for lunch with Bobb"  # Misspelled friend
                }
            }
            response = client.post("/telegram/webhook", json=webhook_data)
            assert response.status_code == 200
            mock_send.assert_called()
            prompt_call = mock_send.call_args_list[-1][0][1].lower()
            assert "please reply with the correct friend's name" in prompt_call

            # Step 2: Simulate user replying with correct name (existing friend)
            mock_send.reset_mock()
            mock_create.reset_mock()
            webhook_data_correct = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "John"
                }
            }
            response_correct = client.post("/telegram/webhook", json=webhook_data_correct)
            assert response_correct.status_code == 200
            mock_create.assert_called_once()
            success_call = mock_send.call_args_list[-1][0][1].lower()
            assert "expense added with corrected friend 'john'" in success_call

            # Step 3: Simulate user replying with a new name (new friend)
            mock_send.reset_mock()
            mock_create.reset_mock()
            # Re-trigger pending_new_friend for this test
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 500 for lunch with Bobb"  # Misspelled friend
                }
            }
            client.post("/telegram/webhook", json=webhook_data)
            webhook_data_new = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "Charlie"
                }
            }
            response_new = client.post("/telegram/webhook", json=webhook_data_new)
            assert response_new.status_code == 200
            mock_create.assert_called_once()
            new_friend_call = mock_send.call_args_list[-1][0][1].lower()
            assert "new friend 'charlie' created and expense added" in new_friend_call

            # Step 4: Simulate user replying 'no' (cancel)
            mock_send.reset_mock()
            # Re-trigger pending_new_friend for this test
            webhook_data = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "paid 500 for lunch with Bobb"  # Misspelled friend
                }
            }
            client.post("/telegram/webhook", json=webhook_data)
            webhook_data_no = {
                "message": {
                    "chat": {"id": TEST_CHAT_ID},
                    "text": "no"
                }
            }
            response_no = client.post("/telegram/webhook", json=webhook_data_no)
            assert response_no.status_code == 200
            cancel_call = mock_send.call_args_list[-1][0][1].lower()
            assert "expense cancelled" in cancel_call

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