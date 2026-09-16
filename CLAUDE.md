# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Taskflow OS" WhatsApp API — a FastAPI backend that turns WhatsApp Business into a sales channel. Merchants manage a product catalog and receive orders over WhatsApp; a Claude-powered AI ("Amara") negotiates prices and takes orders from customers in the same chat, using per-merchant product catalogs and optional knowledge-base documents. Payments/installments are handled via the Tunzaa payment provider. Login is passwordless OTP-over-WhatsApp (no passwords in this system despite the `hashed_password` field in seed scripts).

## Commands

Local dev (no Docker):
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Docker dev stack (API + Redis + Celery worker, with hot reload via bind mount):
```bash
docker compose up --build
```

Migrations (Alembic; `scripts/run.sh` runs `alembic upgrade head` automatically on container start):
```bash
alembic upgrade head
alembic revision --autogenerate -m "description"
```

Manual/exploratory testing — there is no pytest suite in this repo:
- `tests/whatsapp_api_tests.py` is a standalone script (run directly with `python tests/whatsapp_api_tests.py`) that hits the real Meta Graph API to sanity-check `whatsapp_business_management` / `business_management` permissions and webhook subscription. Requires real Meta credentials in `.env`.
- `scripts/simulate_whatsapp.py` — interactive CLI that simulates an inbound WhatsApp webhook against a locally running server (`http://localhost:8000`), then polls the DB for the AI's outbound reply. Use this to manually test the AI sales flow end-to-end without a real WhatsApp number.
- `scripts/seed_whatsapp_account.py` — seeds a dummy user + `WhatsAppAccount` row from `.env` (`PHONE_NUMBER_ID`/`WHATSAPP_TOKEN`) for local testing.

Production image build/run uses `Dockerfile.prod` (multi-stage, non-root `webapp` user, Gunicorn+Uvicorn workers via `app/core/gunicorn_conf.py`); `docker-compose.prod.yml` pulls the published `whoisladleo/whatsapp:latest` image. CI (`.github/workflows/docker-publish.yml`) builds `Dockerfile.prod` and pushes to Docker Hub as `whoisladleo/whatsapp-api` on every push to `master`.

## Architecture

**Request flow**: `main.py` wires up FastAPI, CORS, and mounts routers under `/api/v1` (`app/api/v1/*.py`): `auth`, `whatsapp`, `instagram`, `knowledge`, `tunzaa_webhook`, `accounts`, `products`, `orders`, `pool`. Static product images are served from `/static` (backed by `static/uploads/products`, or Cloudflare R2 when `R2_*` env vars are set).

**Multi-tenant model**: A `User` (merchant) owns one or more `WhatsAppAccount` rows (each tied to a Meta `phone_number_id` + access token). Most WhatsApp-account-scoped endpoints (`/whatsapp/messages`, `/whatsapp/messages/send`) resolve the account via the `get_whatsapp_account` dependency in `app/api/v1/whatsapp.py`, which accepts the account either as the `X-WhatsApp-Account-ID` / `X-Account-ID` header or query param — not from the JWT. Account-management endpoints (`/whatsapp/accounts*`) and everything under `/products`, `/knowledge` are scoped by the authenticated user (`get_current_user` in `app/api/deps.py`, JWT bearer via `PyJWT`).

**Number provisioning**: `POST /whatsapp/accounts` supports two paths. Bring-your-own-number (BYON): the merchant supplies their own `phone_number_id` + `access_token`, used as-is. Pool-assigned: both fields omitted, and a number is auto-claimed from `PhoneNumberPool` (`app/crud/whatsapp.py`'s `claim_available_pool_number` — an atomic conditional-`UPDATE` claim, not `SELECT ... FOR UPDATE`, so it's correct on both SQLite tests and Postgres prod) using the shared `settings.WHATSAPP_POOL_ACCESS_TOKEN`. Pool numbers are pre-provisioned in Meta Business Manager out-of-band (`scripts/add_pool_number.py` registers one as `AVAILABLE` after that manual Meta-side setup) — this is a provisioning pool, not a shared/multiplexed number: each business still gets a dedicated `phone_number_id` for the life of the account, so webhook routing is unaffected either way. Supplying exactly one of the two fields is a 400; an empty pool on an auto-assign request is a 409. Deleting an account releases its pool row (`RELEASED`, not immediately reusable) via `release_pool_number_for_account`.

**Auth**: OTP-based, not password-based. `/auth/request-otp` creates the user if needed and sends a 6-digit OTP to their WhatsApp number using the platform's *default* `WhatsAppProvider` (from `settings.WHATSAPP_TOKEN`/`PHONE_NUMBER_ID`, not the merchant's own account). `/auth/verify-otp` checks the OTP and issues a JWT (`sub` = user id, `app/core/security.py`).

**Inbound webhook pipeline** (`POST /whatsapp/webhook` in `app/api/v1/whatsapp.py`):
1. `WebhookParser.parse_inbound` (`app/core/webhook_parser.py`) flattens Meta's nested webhook JSON into simplified message/status dicts.
2. The account is looked up by `phone_number_id`; message/status is persisted via `app/crud/message.py`.
3. Routing: if the sender's phone number matches the account owner's `username` (merchants log in with their phone number as username), the message is treated as a **merchant command** and goes to `handle_merchant_message` (`app/services/merchant_handler.py`) — regex-parsed slash commands (`/stock`, `/out_of_stock`, `/price`, `/manage`) that mutate `Product` rows directly. Otherwise, if `account.ai_enabled`, it's routed to `llm_service.generate_response` (`app/services/llm.py`) as a **customer** message.
4. The AI reply (or merchant command reply) is sent back via `WhatsAppProvider.send_text` and logged as an outbound `Message`.
5. If `account.callback_url` is set, the raw parsed webhook item is also forwarded there (best-effort, failures swallowed).

**AI sales engine** (`app/services/llm.py`, `LLMService`, backed by the `anthropic` SDK / Claude): per customer phone number it tracks a `Customer` + `ConversationContext` (state machine: `NEGOTIATING` → `HUMAN_HANDOFF` / `MANAGEMENT`). When `context.state == HUMAN_HANDOFF`, the AI stays silent so a human can take over. There's a special-cased "platform account" (`account.name.lower() == 'taskflow'`) where the *merchant themselves* is treated as the counterparty for onboarding rather than a customer — look here first if AI behavior seems wrong for a specific account. The AI's product catalog comes from `Product` rows filtered by `merchant_id`; optional markdown knowledge base content can come either from a `KnowledgeBase` DB row (`app/crud/knowledge.py`, looked up via `account.product_slug`) or a static file at `knowledge/{product_slug}.md` (`LLMService._get_knowledge_base`) — these are two separate, not-yet-unified mechanisms.

**Payments** (`app/services/tunzaa.py`, `TunzaaService`): wraps the Tunzaa API for mobile-money push payments and "Lipa kwa Awamu" (pay-in-installments) plans, tied to `Order`/`Installment` models. Auth token is cached in-process with a 55-minute TTL. `app/api/v1/tunzaa_webhook.py` receives payment/installment status callbacks from Tunzaa. `app/tasks/order_reminders.py` is a Celery task (see `app/core/celery_app.py`) for installment due-date reminders, run via `celery_worker` service / `celery -A app.core.celery_app worker`.

**DB layer**: single `app/db/models.py` (SQLAlchemy 2.0 `DeclarativeBase`) defines all models; no per-domain model files. `app/crud/*.py` holds one module per aggregate (`account`, `knowledge`, `message`, `product`, `user`, `whatsapp`) with plain functions taking a `Session` — no repository classes/ORM abstraction beyond that. `app/schemas/*.py` holds Pydantic request/response models, one file per domain, imported directly into the matching router.

**Config**: all runtime config is a single `pydantic-settings` `Settings` object in `app/core/config.py`, loaded from `.env` (see `.env.example` for the minimal required set, `.env.prod.example` for the full production set including R2 and Tunzaa). There is no per-environment settings class — `RUN_MODE=production` (checked in `scripts/run.sh`) toggles Gunicorn vs. `uvicorn --reload`, not settings.

## Notes for changes

- Language: WhatsApp-facing user copy (OTP messages, etc.) is written in Swahili in places (e.g. `auth.py`'s OTP message) — match this when touching customer-facing message text for the Tanzania merchant base.
- `WhatsAppProvider` (`app/core/provider.py`) is instantiated per-account with that account's own `access_token`/`phone_number_id`, except in the auth OTP flow which deliberately uses the platform-wide default credentials from `settings`.
- When adding a new merchant slash command, extend `handle_merchant_message` in `app/services/merchant_handler.py` — commands are matched by string prefix + regex, not a subcommand framework.
- Migrations live in `alembic/versions/`; some are named by revision hash (autogenerated) and some by hand-picked sequence names (`001_initial_migration.py`, `002_add_brokerage.py`) — check `alembic/versions/` history/heads before creating a new one rather than assuming linear numbering.
