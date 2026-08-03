# Telegram Mini App

React SPA (`frontend/`) on top of a REST/WS layer (`app/miniapp/`) that reuses the
same domain logic as the Telegram bot. Screens follow
`design_handoff_asaka_miniapp/README.md`.

## Design principle

The Mini App needs **data**, the bot needs **text**. Nothing in `app/miniapp/`
re-implements a business rule — every route calls the module the LangGraph nodes
already call:

| Route | Reuses |
|---|---|
| `/products`, `/catalog` | `app.agent.products._get_products_by_category` |
| `/qualify/{category}/tree`, `/qualify/result` | `app.agent.qualify` (`_TREES`, `filter_qualified_products`) |
| `/calc` | `app.agent.nodes.calc_flow._lookup_credit_rate` / `_lookup_deposit_rate` / `_clamp_term` / `_clamp_downpayment` |
| `/calc/schedule`, `/calc/schedule.pdf` | `app.utils.amortization`, `app.utils.pdf_generator` |
| `/branches*` | `app.agent.branches` (search + service matrix) |
| `/rates` | `app.utils.cbu_rates` |
| `/chat/*` | `app.services.chat_service.ChatService` |

Annuity math moved to `app/utils/amortization.py` so the PDF generator, the bot
calculator and the Mini App produce identical numbers.

## Authentication

Every request carries Telegram's signed launch params:

```
Authorization: tma <initData>
```

`app/miniapp/auth.py` recomputes the HMAC-SHA256 with the bot token
(`hash` and `signature` excluded from the data-check-string) and rejects payloads
older than `MINIAPP_INIT_DATA_TTL_SECONDS`. The WebSocket takes the same payload
as an `init_data` query parameter.

`MINIAPP_DEV_MODE=true` accepts unsigned requests and pins them to
`MINIAPP_DEV_USER_ID` — that is how the app runs in a plain browser. It also
enables CORS for `localhost`. **Never enable it in production**: it makes the
whole API unauthenticated.

## Endpoints

All under `/api/miniapp`:

```
GET  /bootstrap                     user (incl. theme), session, limits, operator hours
POST /settings/language             {lang}
POST /settings/theme                {theme: auto|light|dark}
POST /settings/phone                {phone}
GET  /sessions                      history with lifecycle states + counts
GET  /sessions/{id}                 one conversation, read-only when closed
GET  /session/history               timeline of the current session
POST /session/end

GET  /catalog                       categories grouped for screen 09
GET  /products?category=
GET  /products/{id}
GET  /qualify/{category}/tree       decision tree, localized
POST /qualify/result                {category, answers} → filtered products

POST /calc                          payment/income + applied clamps
POST /calc/schedule?preview=        amortization rows
GET  /calc/schedule.pdf             the same PDF the bot sends

POST /leads                         idempotent on client_request_id

GET  /branches?type=&q=&limit=
GET  /branches/nearest?lat=&lon=
GET  /branches/service-matrix
GET  /branches/{office_type}/{id}

GET  /rates?top=
GET  /links

GET  /chat/history
POST /chat/message                  bot turn (daily limit enforced; 410 on a closed session_id)
POST /chat/operator                 {enabled} — handoff on/off
POST /chat/rating                   {rating, comment}
WS   /ws?init_data=                 operator events
```

### Operator events (WebSocket)

`app/miniapp/hub.py` holds `session_id → sockets` in memory. The Socket.IO
callbacks in `app/api/fastapi_app.py` publish to it *in addition to* sending the
message to Telegram, so the bot keeps working unchanged:

`operator_joined` · `operator_message` · `operator_file` · `operator_left`
(`ask_rating`) · `chat_ended` · `operator_error` · `inactivity_warning`

Single-process only. Running multiple uvicorn workers would need Redis pub/sub
behind the same `publish()` signature.

## Frontend

```
frontend/
├── src/
│   ├── telegram.ts        window.Telegram.WebApp wrapper (works without it too)
│   ├── api.ts             typed client + WS URL
│   ├── i18n.ts            ru/uz/en chrome strings
│   ├── store.ts           zustand: nav stack, app, qualify, calc, chat, lead
│   ├── format.ts          money/rate/phone formatting
│   ├── components/        Screen (MainButton wiring), Icon, ui primitives
│   ├── screens/           Onboarding, Home, Chat, Catalog, Product, Calculator, Lead, Branches, Service
│   └── styles/tokens.css  design tokens from the handoff (light + dark)
```

- **Navigation** is a stack in the store. `BackButton` shows from depth ≥ 2 and
  closes the app at the root. There is no in-content back arrow inside Telegram.
- **MainButton** is native inside Telegram; outside it, `Screen` renders a sticky
  DOM bar so every flow stays testable in a browser.
- **Theme** comes from `themeParams` over the token defaults, and follows
  `prefers-color-scheme` in a browser.

## Running locally

```bash
make miniapp-install                  # once

# backend (dev mode = no initData required)
MINIAPP_DEV_MODE=true python3 main.py

# frontend, in another shell
make miniapp-dev                      # http://localhost:5173/app/
# backend on another port:
# cd frontend && VITE_API_TARGET=http://127.0.0.1:8099 npm run dev
```

The dev server proxies `/api` to the backend, so the SPA and the API share an
origin exactly as they do in production.

## Serving the build

`make miniapp-build` writes `frontend/dist`, which `app/miniapp/static.py` mounts
at `/app` with an SPA fallback. When the directory is absent the mount is skipped
and a line is logged — that is the normal development state.

Telegram only opens `https` URLs, so pointing the bot at the Mini App needs a real
domain (or ngrok):

```
MINIAPP_URL=https://agent-bot.uz/app
```

With it set, startup installs the chat menu button and `/app` replies with a
`WebAppInfo` button. Without it, `/app` answers "coming soon" and nothing else
changes.

## Session lifecycle (screens 26 / 26a)

A session lives `SESSION_INACTIVITY_TIMEOUT_MINUTES` from the last message; the
existing inactivity watcher then closes it with `closed_reason="timeout"`. That
is the only state the history screen calls **expired** — readable, not writable.
Anything the user or the operator closed deliberately stays an ordinary
**ended** conversation. Posting to a session that is not the current active one
answers **410**, which is what the archive screen turns into "start a new
dialog". Closed conversations stay readable for `ARCHIVE_DAYS` (30).

## Theme

`User.theme` holds `auto` (default, follows the Telegram client's `themeParams`)
or a pinned `light` / `dark`. A pinned theme also *stops* the client palette
from being applied, so a dark Telegram cannot bleed into a light Mini App —
see `applyStoredTheme` in `src/telegram.ts`.

## Not done yet

- Rich chat cards (`product_list` / `calc_result` / `branch_card`, screen 06) —
  needs a `ui_payload` field alongside `answer` in `BotState`; today the chat
  renders the agent's text.
- Offline outbox in `CloudStorage`; the app shows the offline plate but does not
  queue messages.
- Attachments from the user (the composer's paperclip is disabled); operator
  files arrive as links.
- Static map tile on the office screen is still the striped placeholder.
- Home variant 2b (the contrast hero) from iteration 2 — 2a was chosen.
