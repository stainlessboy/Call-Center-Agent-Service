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
GET  /sessions/{id}                 one conversation, read-only when closed — messages incl. `ui_blocks` (Phase 4 fix — was missing it)
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

GET  /chat/history                  messages incl. `ui_blocks` per turn (see below)
POST /chat/message                  bot turn (daily limit enforced; 410 on a closed session_id) — response incl. `ui_blocks`
POST /chat/operator                 {enabled} — handoff on/off
POST /chat/rating                   {rating, comment}
WS   /ws?init_data=                 operator events + assistant token stream
```

### Operator events (WebSocket)

`app/miniapp/hub.py` holds `session_id → sockets` in memory. The Socket.IO
callbacks in `app/api/fastapi_app.py` publish to it *in addition to* sending the
message to Telegram, so the bot keeps working unchanged:

`operator_joined` · `operator_message` · `operator_file` · `operator_left`
(`ask_rating`) · `chat_ended` · `operator_error` · `inactivity_warning`

Single-process only. Running multiple uvicorn workers would need Redis pub/sub
behind the same `publish()` signature.

### Assistant token stream (WebSocket)

Live token streaming for bot turns, added alongside `ui_blocks` (Phase 3 of the
"personal consultant" redesign). Every event on `/ws` has the shape
`{"event": "<name>", ...fields}` (same envelope as the operator events above —
`hub.publish(session_id, event, **data)`); this is why these two use `"event"`
rather than a `"type"` key.

| Event | Fields | When |
|---|---|---|
| `assistant_token` | `text: str` | Once per streamed chunk of `node_faq`'s final prose answer, as OpenAI emits it. |
| `assistant_done` | — | Once, right after `POST /chat/message`'s `handle_user_message()` call finishes — **always**, even on a turn that produced zero `assistant_token` events (a `calc_flow`/`qualify_flow` deterministic turn, an FAQ strict-match short-circuit, human_mode, an LLM error that fell back to a canned reply, ...). The client should use this — not the POST response — to know the streaming bubble is finalized, since the POST can be slower to arrive than the socket events. |

Mechanics (`app/agent/streaming.py`, `app/agent/nodes/faq.py::_run_llm_round`):
`POST /chat/message` arms a per-request `on_token` callback (a `contextvars.ContextVar`,
armed only for an authenticated, non-`human_mode` turn on an already-known
`session_id`, gated by `MINIAPP_STREAMING_ENABLED`) before calling
`ChatService.handle_user_message`, and resets it in a `finally`. `node_faq`
picks it up automatically — nothing at the graph/state level changes. Only
`node_faq`'s own tool-loop rounds stream (the customer-facing prose channel);
side-question LLM calls inside `calc_flow`/`qualify_flow` and the memory
extractor never stream. **The Telegram path never arms this callback, so its
behavior is completely unchanged** — same `.ainvoke()` call as before Phase 3.

A round that turns out to be a tool call (not prose) is detected via the
first `tool_call_chunks` delta and silently stops forwarding for the rest of
that round — in practice this only ever means an all-empty round streams
nothing, since OpenAI tool-calling responses don't interleave prose with a
tool call.

## UI blocks

Structured data for chat cards, published alongside the answer text so the
Mini App can render a real product card / office card / rate table / payment
schedule / comparison grid instead of parsing the bot's HTML string. Telegram
is entirely unaffected — `ui_blocks` is an additive field the bot's own
rendering path never reads.

**Envelope** — every block is `{"type": "<block_type>", "data": {...}}`.
A turn can carry zero, one, or several blocks (`"ui_blocks": [...]` — `[]`
when there is nothing structured to show, e.g. plain FAQ answers). Delivered:

- **Live**: `POST /chat/message` → response body `ui_blocks` (final, for the
  turn just completed).
- **History**: `GET /chat/history` → `messages[].ui_blocks` (persisted on the
  `role="agent"` `Message` row, JSONB column added in migration
  `c7d8e9f0a1b2` — see "Persistence" below). `[]` for user/system/operator
  rows and for any agent turn with nothing structured.

Where a block comes from:

| Block type | Producer |
|---|---|
| `product_list` | `get_products` tool, `recommend_product` tool (`kind: "recommend"`), qualify-questionnaire results (`kind: "qualify_result"`, `nodes/qualify_flow.py`), the Phase 4 dead-end rescue pitch (`kind: "recommend"`, `nodes/qualify_flow.py::_dead_end_with_rescue`) |
| `product_card` | `select_product` tool |
| `office_list` | `find_office` tool, `select_office` tool when the user picks "all" |
| `office_detail` | `select_office` tool for a single pick |
| `rate_table` | `get_currency_info` tool |
| `calc_result` | `custom_loan_calculator` tool (kind `"credit"`, no product), `nodes/calc_flow.py`'s deterministic per-product finalization (kind `"credit"` or `"deposit"`), the Phase 4 `what_if_scenario` tool (kind `"credit"`, `data.is_hypothetical: true`), and the Phase 4 `affordability_check` tool (kind `"affordability"`, see below) |
| `comparison_table` | `nodes/qualify_flow.py` when the questionnaire narrows results to MORE than one product, and the Phase 4 `compare_products` tool |

**Product dict** (the shape used inside `product_list.data.products`,
`product_card.data.product` and `comparison_table.data.products` — one
schema for all three, field set depends on `category`; source:
`app.agent.products._product_public_dict`, the same aggregate dict the bot's
own text renderer (`_format_product_card`/`_format_product_list_text`)
consumes, minus the internal rate-matching-only `rate_rules` list):

```jsonc
// mortgage / autoloan / microloan / education_credit
{
  "id": 12,
  "name": "Ипотека Стандарт", "name_en": "...", "name_uz": "...",
  "rate": "17.0–22.0%",              // display string
  "rate_min_pct": 17.0, "rate_max_pct": 22.0,
  "term": "до 180 мес.",             // display string — RU only, see note below
  "amount": "от 50 000 000 сум",     // display string — RU only
  "amount_min": 50000000, "amount_max": null,
  "downpayment": "от 25%",           // display string — RU only
  "collateral": "...", "purpose": "...",
  "rate_matrix": [                   // per-condition rates, already used by the bot's text card
    {"income_type": "official", "rate_min_pct": 17.0, "rate_max_pct": 19.0,
     "rate_condition_text": "", "term_min_months": 12, "term_max_months": 180,
     "downpayment_min_pct": 25, "downpayment_max_pct": null}
  ],
  "rate_condition_kind": "income_type",
  "needs_age": false, "needs_downpayment": true,
  "reason": ["best_rate"]            // ONLY present when this list came from recommend_product; tags: "best_rate" | "age_fit" — localize client-side, not pre-rendered text
}

// deposit
{
  "name": "...", "name_en": "...", "name_uz": "...",
  "rate": "17.0–19.0%", "rate_pct": 17.0,
  "term": "12 мес.", "term_months": 12, "term_min": 1, "term_max": 36,
  "min_amount": "1 000 000 сум",
  "min_amounts_by_currency": {"UZS": [1000000, "1 000 000 сум"], "USD": [100, "100"]},
  "currency": "UZS, USD",
  "topup": "...", "payout": "...",
  "rate_schedule": [
    {"currency": "UZS", "term_months": 12, "term_text": "12 мес.",
     "rate_pct": 17.0, "rate_text": "17%", "min_amount": 1000000, "min_amount_text": "1 000 000 сум"}
  ]
}

// debit_card / fx_card
{
  "name": "...", "name_en": "...", "name_uz": "...",
  "network": "Uzcard", "currency": "UZS",
  "cashback": "...", "issue_fee": "...", "annual_fee": "...",
  "delivery": true, "validity": "...", "reissue_fee": "...",
  "transfer_fee": "...", "issuance_time": "...",
  "mobile_order": true, "pickup": false, "payroll": true
}
```

> **Deviation from a pure raw-data contract**: `term`/`amount`/`downpayment`
> (credit) already existed as Russian-only pre-formatted strings in the
> aggregate product dict the bot's text renderer consumes (`_fmt_term_months_range`
> / `_fmt_pct_range` in `app/utils/data_loaders.py`, baked in at load time — not
> something Phase 3 introduced). Reusing that dict as-is (per the spec's brief:
> serialize from "реально доступные поля") means these three fields are RU
> text regardless of the user's Mini App language. `rate`/`amount_min`/
> `amount_max`/`term_min`/`term_max`/etc are locale-neutral numbers — prefer
> those and treat the RU strings as a fallback/legacy display option. Making
> them fully bilingual would mean threading a `lang` parameter through
> `_get_products_by_category`'s DB-aggregation pass, which is out of scope for
> this phase; flagged here for whoever picks up full Mini App i18n on product
> cards.

**Office dict** (`office_list.data.offices[]` / `office_detail.data.office`;
source: `app.agent.branches.office_public_dict`). `region_*` only has values
for `sales_office`; `landmark_*`/`location_url` only for `filial`:

```json
{
  "id": 5, "office_type": "filial",
  "name_ru": "...", "name_uz": "...",
  "address_ru": "...", "address_uz": "...",
  "region_ru": null, "region_uz": null,
  "landmark_ru": "...", "landmark_uz": "...",
  "location_url": "https://maps...",
  "latitude": 41.31, "longitude": 69.24,
  "phone": "+998...", "hours": "09:00-18:00"
}
```

```json
// office_list
{"type": "office_list", "data": {"office_type": "filial", "query": "Андижан", "offices": [ /* office dict */ ]}}
// office_detail
{"type": "office_detail", "data": {"office": { /* office dict */ }}}
```
`office_type`/`query` are `null`/`""` when the block came from `select_office("all")`
(a saved mixed-type list from a prior `find_office` call, not a fresh search).

**rate_table** (source: `get_currency_info` tool, `app.utils.cbu_rates.fetch_cbu_rates`
for the fixed set USD/EUR/RUB/GBP/KZT/CNY):

```json
{
  "type": "rate_table",
  "data": {
    "date": "06.08.2026",
    "rates": [
      {"code": "USD", "name_ru": "Доллар США", "name_en": "US Dollar", "name_uz": "AQSH dollari",
       "nominal": 1, "rate": 12750.5, "diff": 5.2, "icon": "🇺🇸"}
    ]
  }
}
```
`rate`/`diff`/`nominal` are parsed to `float` from the CBU JSON's string
fields; `null` if a value fails to parse rather than blocking the tool.

**calc_result** — `kind: "credit"` (source: `custom_loan_calculator` tool, and
`nodes/calc_flow.py`'s deterministic finalization; math from
`app.utils.amortization.amortize`, the SAME function used for the PDF, so the
numbers always agree with the PDF schedule):

```json
{
  "type": "calc_result",
  "data": {
    "kind": "credit",
    "product_name": "Ипотека Стандарт",
    "amount": 50000000, "downpayment": 10000000, "downpayment_pct": 20.0,
    "principal": 40000000, "rate_pct": 22.0, "term_months": 36,
    "monthly_payment": 1550000.0, "total_payment": 55800000.0, "overpayment": 15800000.0,
    "schedule": [
      {"month": 1, "payment": 1550000.0, "principal_part": 850000.0, "interest_part": 700000.0, "balance": 39150000.0}
    ],
    "schedule_truncated": false,
    "dti_ratio": 0.62,
    "is_hypothetical": false
  }
}
```
`product_name` is `null` for `custom_loan_calculator` (not tied to a product).
**Schedule size decision**: capped at `MAX_SCHEDULE_ROWS = 360` rows
(`app/agent/ui_blocks.py`) — covers every real bank product (≤240 months /
20 years) with headroom; `custom_loan_calculator` allows free-form terms up
to 600 months (50 years), so `schedule_truncated: true` can appear there.
When truncated, treat the PDF (still generated/attached the same way as
before Phase 3) as the source of the full schedule.

**`dti_ratio`** (Phase 4 "Экспертиза" schema extension) — `monthly_payment /
state.user_profile.facts.income_monthly`, or `null` when the client's income
isn't known yet. Only present (possibly `null`) on `calc_result` blocks
produced by `nodes/calc_flow.py`'s deterministic per-product finalization —
`custom_loan_calculator`'s block does NOT carry this field (out of scope for
Phase 4, tracked as a known asymmetry rather than silently added). A ratio
above `DTI_WARN_RATIO` (env, default `0.45`) also appends a soft warning to
the reply text and is the same threshold `affordability_check` uses.

**`is_hypothetical`** (Phase 4) — `true` only on the block produced by the
`what_if_scenario` tool: a side-hypothesis recalculation that intentionally
never touches `dialog.calc_slots` (see that tool's docstring in
`app/agent/tools.py`). Render it distinctly from the customer's real active
calculation — e.g. don't let it replace the last "real" calc_result card.
Absent (falsy) on every other `calc_result` producer.

**`kind: "affordability"`** (Phase 4, source: the `affordability_check`
tool) — a lighter variant with no schedule/principal, since the tool may only
know a raw monthly payment with no amount/term context at all:

```json
{
  "type": "calc_result",
  "data": {
    "kind": "affordability",
    "monthly_payment": 3000000.0,
    "loan_amount": 40000000, "term_months": 36,
    "income_monthly": 5000000,
    "dti_ratio": 0.6,
    "dti_warn_ratio": 0.45
  }
}
```
`loan_amount`/`term_months` are `null` when the tool was given a bare
`monthly_payment` with nothing to derive them from. `income_monthly`/
`dti_ratio` are `null` when the client's income isn't known — the reply text
gives the general 40-50%-of-income rule in that case instead of a verdict.

`kind: "deposit"` — simple interest, no amortization schedule:

```json
{
  "type": "calc_result",
  "data": {
    "kind": "deposit",
    "product_name": "Депозит Стандарт",
    "amount": 20000000, "term_months": 12, "rate_pct": 19.0,
    "interest_total": 3800000.0, "monthly_income": 316666.67, "total": 23800000.0
  }
}
```

**comparison_table** (source: `nodes/qualify_flow.py::render_filter_result`,
only added when the questionnaire's DB filter matched MORE than one product —
a single result gets a `product_list` only):

```json
{
  "type": "comparison_table",
  "data": {
    "category": "mortgage",
    "columns": ["rate", "term", "amount", "downpayment"],
    "products": [ /* product dict, same shape as product_list */ ]
  }
}
```
`columns` is a rendering hint (`app.agent.products._comparison_columns`) —
which of the product dict's fields are meaningfully comparable side-by-side
for that category (differs for credit / deposit / card).

### Persistence

`ui_blocks` is stored on `Message.ui_blocks` (JSONB, migration `c7d8e9f0a1b2`)
— populated only on `role="agent"` rows, `NULL` everywhere else — so the
history screen can re-render cards after a reload instead of falling back to
text only. This was a deliberate choice over "recompute from `dialog` on
read": several block types (the credit `calc_result` schedule, the exact
`rate_table` snapshot, a `recommend_product` pitch) are the result of a
point-in-time computation (a rate quoted that day, an amortization run for
numbers the user is no longer editing) that `dialog` does not retain in a
replayable form — storing the rendered block is the only way old turns look
the same on reload as they did live.

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

- Rich chat cards (screen 06) are now DATA-complete (`ui_blocks`, see above) —
  the frontend rendering of these cards (`product_list` / `product_card` /
  `office_list` / `office_detail` / `rate_table` / `calc_result` /
  `comparison_table` components) is a separate, not-yet-done frontend task.
- `compare_products` / `what_if` / `affordability` tools (Phase 4 of the
  personal-consultant redesign) — out of scope for Phase 3.
- Offline outbox in `CloudStorage`; the app shows the offline plate but does not
  queue messages.
- Attachments from the user (the composer's paperclip is disabled); operator
  files arrive as links.
- Static map tile on the office screen is still the striped placeholder.
- Home variant 2b (the contrast hero) from iteration 2 — 2a was chosen.
