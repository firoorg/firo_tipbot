# CLAUDE.md — Firo Tipbot

## Project Overview

Firo Tipbot is a **Telegram bot** that enables users to send and receive Firo cryptocurrency tips within Telegram chats. It manages user wallets, processes deposits/withdrawals, and supports features like anonymous tips and red envelopes. The bot communicates with a Firo full node via JSON-RPC and persists state in MongoDB.

## Architecture

```
tipbot.py                  # Main application — TipBot class + main() entry point (~1350 lines)
api/firo_wallet_api.py     # Firo JSON-RPC wrapper (FiroWalletAPI class)
services.json              # Configuration (MongoDB, bot token, RPC endpoint, i18n strings)
update_address.py          # Utility script for batch Spark address migration
images/                    # Template PNG images for tip/deposit confirmation UI
fonts/                     # ProximaNova TrueType fonts for image generation
```

### Key patterns

- **Single-file monolith**: All bot logic lives in `tipbot.py` inside the `TipBot` class
- **Synchronous polling**: Uses `telegram.Bot` (python-telegram-bot v12) with manual `getUpdates` polling — no async/await
- **Scheduled tasks**: `schedule` library runs in a background thread:
  - `update_balance()` every 60 seconds — monitors blockchain for deposits
  - `automintunspent()` every 300 seconds — anonymizes transparent funds
- **Image generation**: PIL/Pillow creates template-based confirmation images sent to users
- **Spark protocol**: Uses Firo's Spark privacy protocol (migrated from Lelantus)

### Data flow

1. Bot polls Telegram for new messages via `getUpdates`
2. Commands are parsed and dispatched to handler methods
3. Tips modify MongoDB balances directly (no on-chain tx)
4. Withdrawals call `spendspark()` RPC and track confirmations
5. Deposits detected by polling `listsparkmints()` and matching addresses

## Tech Stack

- **Language**: Python 3.6+ (uses f-strings)
- **Bot framework**: python-telegram-bot 12.2.0 (synchronous API)
- **Database**: MongoDB via pymongo 3.11.4
- **Blockchain**: Firo node JSON-RPC via requests
- **Image processing**: Pillow 8.2.0, matplotlib 3.4.2
- **QR codes**: pyqrcode 1.2.1
- **Task scheduling**: schedule 1.1.0

## Running the Bot

```bash
pip3 install -r requirements.txt
# Configure services.json with bot_token, httpprovider (RPC URL), and log_ch
python3 tipbot.py
```

**Prerequisites**: MongoDB running locally, Firo full node with RPC enabled.

For production, deploy as a systemd service (see `ReadMe.md`).

## Configuration

All configuration is in `services.json` (not environment variables):

| Key | Purpose |
|-----|---------|
| `mongo.connectionString` | MongoDB connection URI |
| `telegram_bot.bot_token` | Telegram Bot API token |
| `httpprovider` | Firo node RPC endpoint (e.g., `http://user:pass@127.0.0.1:8332`) |
| `log_ch` | Telegram channel ID for admin logging |
| `dictionary` | All user-facing message templates (i18n) |

**Security note**: `services.json` contains secrets (bot token, RPC credentials). Never commit real credentials.

## MongoDB Collections

| Collection | Purpose |
|------------|---------|
| `users` | User profiles: `_id` (user_id), `username`, `Address` (array), `Balance`, `Locked`, `IsVerified` |
| `txs` | Blockchain transactions: `txId`, type, confirmations, amount |
| `senders` | Pending withdrawal tracking |
| `tip_logs` | Tip transaction history |
| `envelopes` | Red envelope state (group feature) |
| `captcha` | User verification state |
| `commands_history` | Audit log of all commands |

## Key Constants

- `AV_FEE = 0.002` — Fixed withdrawal fee in FIRO
- `SATS_IN_BTC = 1e8` — Satoshi conversion factor
- Deposit confirmation threshold: **2 confirmations**

## Bot Commands

| Command | Context | Description |
|---------|---------|-------------|
| `/start` | DM | Register user and create wallet |
| `/tip @user amount` | DM/Group | Send a tip |
| `/atip @user amount` | DM | Send an anonymous tip |
| `/balance` | DM | Check wallet balance |
| `/deposit` | DM | Show deposit address with QR code |
| `/withdraw addr amount` | DM | Withdraw to external address |
| `/envelope amount` | Group | Create a red envelope |
| `/help` | Any | Show help text |

## Development Guidelines

### Code style
- No linter or formatter is configured — maintain consistency with existing code
- Existing code uses 4-space indentation, snake_case for functions/variables
- HTML-formatted Telegram messages (use `<b>`, `<pre>`, `<a>` tags)

### Commit conventions
- Lowercase, imperative mood, no trailing period
- Examples: `fix inaccurate info`, `rework spark address check`, `revert missing fee subtraction`

### Important considerations
- The bot is a **single-threaded polling loop** — blocking calls halt message processing
- Tips are **off-chain** (database-only); only deposits and withdrawals touch the blockchain
- User addresses are stored as arrays in the `Address` field (supports address migration)
- The `update_address_and_balance()` method validates Spark addresses and creates new ones if needed
- No test suite exists — test changes manually against a testnet node (see `Testnet.md`)

### Files you should not modify without care
- `services.json` — contains secrets and all i18n strings
- `images/` — template PNGs that the image generation code depends on by exact filename
- `fonts/` — font files referenced by exact path in `tipbot.py`
