# [24:06:26 16:00:00] {Major features, testing, and documentation overhaul}
- **Authoritative split:** Expense confirmation and supermemory storage now use the authoritative split as returned by the Splitwise API, not just the parsed input. User names and shares are mapped from the API response.
- **Supermemory integration:** All expenses and chat messages are stored in supermemory, partitioned per user. Metadata now includes the authoritative split (as JSON string) and content type.
- **Semantic search endpoint:** Added `/api/search_memories` and improved webhook logic to allow users to search their own expense and chat history in natural language.
- **Error handling:** Webhook handler now robustly distinguishes between commands, expenses, and chat/search, with early returns and clear user feedback. All error paths store chat messages in supermemory.
- **Testing:** Added and fixed advanced tests for expense storage, chat message storage, semantic search, and error handling. All tests now pass; coverage is 75%+ for core logic.
- **Documentation:** README.md completely rewritten with advanced, industry-standard documentation: setup, production, usage, semantic memory, commands, testing, troubleshooting, and changelog highlights.
- **Security:** All secrets and tokens are environment-driven; user tokens are gitignored. Best practices for OAuth and API key management documented.

# [24:06:25 12:20:00] {Major bugfixes and improvements for Splitwise Telegram Connector}
- Fixed floating point rounding errors in expense splitting: shares are now rounded to 2 decimals and any surplus/deficit is absorbed by the payer, ensuring the sum matches the total cost exactly (Splitwise API requirement).
- Improved error handling: after calling the Splitwise API, the bot now checks for errors in the response and only sends a success message if the expense was actually created. If not, a detailed error is sent to the user.
- Updated the Telegram webhook handler to use the new error check logic for expense creation.
- Added instructions and best practices for updating CALLBACK_BASE_URL in both the .env file and Splitwise developer portal when the ngrok URL changes.
- Verified all uses of CALLBACK_BASE_URL are consistent and environment-driven (no hardcoded URLs remain).
- Provided robust restart and troubleshooting instructions for local development and OAuth debugging.
- All changes are backward compatible and improve reliability for both local and production deployments.