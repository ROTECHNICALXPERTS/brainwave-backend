# AutoResearch

An autonomous multi-agent research orchestrator that **pays for its own tool calls**
using the [x402](https://x402.org) protocol, settled in testnet USDC on **Base
Sepolia**, and compiles the results into a single cited research report.

A user submits a query + a USDC budget cap. A planner LLM decomposes it into a task
graph (search, fact-check, enrichment, summarize, compile-report). The orchestrator
executes independent tasks in parallel, and for every task it pays the x402-gated
microservice that performs it — signing a gasless EIP-3009 USDC payment, retrying
after the `402`, and logging the transaction to a payment ledger. If a job is about
to exceed its budget cap, the orchestrator automatically switches the final report
step to a cheaper model tier, or stops the job as `budget_exceeded`. If a service
call itself fails (killed process, timeout, bad response), the orchestrator retries
against an alternate provider before giving up on that step. Search runs a live
reverse auction across 3 competing providers, and the finished report gets a SHA-256
provenance receipt anchored in its own Base Sepolia transaction.

```
User Query
   │
   ▼
Planner (LLM, JSON task graph)
   │
   ▼
Orchestrator ──quotes+pays──► Search (3 competing providers, reverse auction) ┐
             ──pays──────────► Fact-Check API                                ├─ parallel
             ──pays──────────► Enrichment API                                ┘
             ──pays──────────► Summarizer API                              (fan-in)
             ──pays──────────► Report-Gen API premium  ─┐ budget-aware + failure fallback
             ──pays──────────► Report-Gen API cheap    ─┘ (independent process/port)
   │
   ▼
Citation Compiler → cited Markdown report, cost-per-claim breakdown, payment ledger
   │
   ▼
Provenance receipt → SHA-256(report) anchored in one small Base Sepolia tx
```

Full spec: [`docs/x402-research-agent-spec.md`](docs/x402-research-agent-spec.md).

## Stack

- **Python 3.11+**, FastAPI + uvicorn for every service and the orchestrator
- **`x402==1.0.0`** (official Coinbase/x402-foundation SDK) for the payment
  middleware (server side) and payment client (buyer side). Deliberately pinned to
  the 1.0.x API (`payTo` address + price string + `network="base-sepolia"` +
  `x402HttpxClient`) rather than the newer 2.x API (CAIP-2 network ids + resource-server
  scheme registration) — same protocol, far less boilerplate for this project.
- **`eth-account` / `web3.py`** for the Base Sepolia wallet
- **SQLite** (plain `sqlite3`, no ORM) for the payment ledger — `data/ledger.db`
- **Groq** (cheap/fast tier: planner, fact-check, enrichment, summarizer) and
  **Gemini** (strong tier: final report synthesis only) for LLM calls - both free,
  called via their OpenAI-compatible chat-completions endpoints
- **Streamlit** for a local test console (not part of the production API)

## Project layout

```
shared/                     config, pydantic schemas, wallet, SQLite ledger, x402 client, LLM
                            wrapper, mock search data, report synthesis logic, search logic,
                            provenance receipts
services/
  search/main.py            POST /api/search, GET /quote  ~$0.0015-0.0045 (randomized at startup)
  search-b/main.py          POST /api/search, GET /quote  ~$0.0015-0.0045 - auction competitor
  search-c/main.py          POST /api/search, GET /quote  ~$0.0015-0.0045 - auction competitor
  fact-check/main.py        POST /api/fact-check          $0.003
  enrichment/main.py        POST /api/enrich               $0.002
  summarizer/main.py        POST /api/summarize            $0.004
  report-gen/main.py        POST /api/report                $0.006 (premium/strong model), own process/port
  report-gen-cheap/main.py  POST /api/report                $0.003 (cheap/fast model), own process/port
orchestrator/               planner.py, orchestrate.py (task graph exec, budget guard, failure
                            fallback, reverse auction), citations.py (cost-per-claim), job_store.py,
                            main.py (the HTTP API), auth.py (caller identity)
mcp_server/                 MCP server - exposes the orchestrator to Claude Desktop / Claude Code /
                            Cursor as 5 tools. Thin adapter over the same HTTP API; see its README.
extension/                  Chrome extension - right-click any sentence on any page to fact-check it
                            for one x402 payment, or run the full pipeline on it. See its README.
wallet/                     generate_wallet.py, check_balance.py
frontend-react/             React + Vite multi-page site (marketing pages + the live console)
  src/site/                 nav, footer, pages, live-data components, theme toggle
  src/pages/ConsolePage.tsx the working console, lazily loaded as its own chunk
  src/components/           mission control, final report, agent steps (the console internals)
  src/api/client.ts         typed API client + ledger-derived stats
frontend/app.py             Streamlit test console (original, still works)
scripts/
  test_e2e.py               headless end-to-end smoke test
  test_live_cost_meter.py   asserts total_spent_usdc is live + monotonic mid-job
  test_failover.py          kills report-gen (premium) mid-job, confirms clean fallback recovery
  manage_keys.py            mint / list / revoke API keys and see today's spend per key
run_all.sh                  starts orchestrator + all 8 services
requirements.txt            single shared virtualenv for everything
```

`report-gen` and `report-gen-cheap` are deliberately two independent processes on
two independent ports (not two routes on one app) sharing logic from
`shared/report_logic.py` - so killing one for a demo genuinely leaves the other
reachable, and the orchestrator's tier fallback is a real process-level failover,
not a no-op. The 3 search providers similarly share logic from `shared/search_logic.py`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # if you haven't already
python wallet/generate_wallet.py   # generates BUYER_* and SELLER_PAYTO_ADDRESS into .env
```

Then edit `.env`:

1. **Fund the buyer wallet** (the address printed by `generate_wallet.py`) with
   Base Sepolia testnet USDC via the [Circle faucet](https://faucet.circle.com)
   (select network "Base Sepolia"). x402 payments are gasless (EIP-3009
   `transferWithAuthorization`), so you only need testnet **USDC**, not ETH.
   Check it landed with:
   ```bash
   python wallet/check_balance.py
   ```
2. **LLM keys** — both free:
   - `GROQ_API_KEY` from [console.groq.com/keys](https://console.groq.com/keys) - powers the cheap/fast
     tier (planner, fact-check, enrichment, summarizer).
   - `GEMINI_API_KEY` from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) - powers the
     strong tier (final report synthesis only).
   Each is independent, and the pipeline runs end-to-end either way. Missing
   `GROQ_API_KEY` degrades the cheap-tier calls to heuristics. Missing `GEMINI_API_KEY`
   routes the strong tier through Groq instead — still a real synthesised report, just
   not the stronger model. Only with *neither* key does report synthesis drop to a
   deterministic template (see "What's real vs mocked" below).

   Note on the model: keys created recently cannot call the `gemini-2.x` line at all
   (it 404s with "no longer available to new users" despite still being listed), so the
   default is `gemini-3.5-flash`. Thinking is disabled for that call — Gemini 3.x
   otherwise leaks fragments of its own reasoning into the response, which silently
   fails the report's required output format.
3. **Optional**: `TAVILY_API_KEY` for real web search results (otherwise the Search
   API returns curated mock results).
4. **Optional, for the provenance receipt**: a small amount of Base Sepolia
   **testnet ETH** in the buyer wallet (same [Alchemy faucet](https://www.alchemy.com/faucets/base-sepolia)
   as before). This is the *one* non-gasless transaction in the whole system - see
   "On-chain provenance receipt" below. Without it, everything else still works;
   the report just won't have a `provenance_tx_hash`.

## Running it

```bash
./run_all.sh
```

Starts the 8 microservices (ports `4001`–`4008`) and the orchestrator (port `4000`).
Ctrl-C stops everything.

### Test it headlessly

```bash
python scripts/test_e2e.py "your query here" 0.05
```

Submits a job, polls the trace live, prints the final report, citations, and
payment ledger, and — if the buyer wallet is funded — a real
`https://sepolia.basescan.org/tx/<hash>` link.

```bash
python scripts/test_live_cost_meter.py            # asserts total_spent_usdc updates live, mid-job
python scripts/test_failover.py                   # kills report-gen (premium) mid-job, confirms recovery
```

`test_failover.py` needs the stack already running (`./run_all.sh`) - it finds and
kills the process on the report-gen port, watches the orchestrator fall back to
`report-gen-cheap`, then restarts report-gen so the stack is back to normal.

### Test it with a UI

Two frontends ship with this repo, and both talk to the same orchestrator on `:4000`.

**The React site (primary).** A full multi-page product site with the live console
built into it:

```bash
cd frontend-react
npm install
npm run dev          # http://localhost:5173
```

| Route | What it is |
|---|---|
| `/` | Landing page — hero, live settlement figures pulled from the real ledger, a scrolling feed of genuine transactions, the interactive pipeline diagram |
| `/how-it-works` | The orchestrator explained stage by stage, the anatomy of one 402 exchange, plus the most recent real job read live from the ledger |
| `/features` | Deep dive on each capability (payments, auction, narration, failover, cost attribution, self-critique, provenance) |
| `/developers` | Quickstart in Python / JavaScript / cURL, API reference, real response shapes |
| `/about` | Architecture, stack, and an honest "what's real vs what degrades" table |
| `/console` | **The working tool** — submit a query + budget and watch it run live |

Every figure on the marketing pages (total USDC settled, transaction count, job
count, the transaction ticker, the spotlighted job) is computed from `/api/ledger`
at runtime. With the backend stopped, those sections say so rather than showing
invented numbers.

**Light and dark themes.** The toggle sits in the nav (and in the console header).
It follows the OS setting until you pick explicitly, then remembers your choice in
`localStorage`; an inline script in `index.html` applies the theme before first
paint so there's no white flash on load. Colours are driven by semantic tokens in
`src/index.css` — `--color-ink*` / `--color-paper*` / `--color-line*` for surfaces
and text, plus `--tone-{signal,brand,warn,amber,danger,violet}-{bg,text,border}`
triples for badges and callouts, so every status chip stays legible in both themes.
Two ambient light systems are tokenised the same way. `--amb-*` drives the wash behind
every page: one dominant bloom anchored top-centre with two quieter flanks, then a
gradient back to paper below the fold, so the colour sits where the eye starts on every
route. Light runs warm orange with cool flanks; dark runs a single red family. `--deck-*`
and `--dome-*` drive the console's grid floor and the dome of light it sits on.

**The Streamlit console (original).** Still here, useful for quick headless poking:

```bash
streamlit run frontend/app.py
```

Submit a query + budget, watch the live payment trace (status, tier, tx hash →
clickable BaseScan link), see the final cited report, look up any past job by ID,
and browse the raw payment ledger across all jobs.

## API

The original three endpoints and their core fields are unchanged; the fields below
are additive only.

- `POST /api/research` → `{query, budget_cap_usdc}` → `202 {job_id}`
- `GET /api/research/{job_id}/trace` → live steps, total spend vs cap. **Polls live** -
  `total_spent_usdc` is recomputed from the ledger on every call, so it updates
  mid-job, not just at completion (see `scripts/test_live_cost_meter.py`).
  Step `status` values: `paying` → `paid`/`completed` / `failed` / `tier_downgraded`
  (pre-emptive, budget-driven) / `skipped_budget_exceeded`. A step that succeeded
  after an earlier attempt failed also carries:
  - `provider_switched: true`
  - `switched_from` / `switched_to`: the provider/tier names involved (e.g. `"premium"` → `"cheap"`, or `"search_c"` → `"search"`)
  A `web_search` step also carries the reputation-weighted reverse-auction result:
  - `quotes_considered`: `[{provider, price_usdc, error, success_rate}]` for every provider queried
  - `chosen_provider`: which one is/was being paid (not always the raw-cheapest quote - see "Reputation-weighted reverse auction" below)
  The trace also carries a live narration feed:
  - `narration`: `[{event, text, task_id, timestamp}]`, appended to as the job runs - see "Live AI reasoning stream" below
- `GET /api/research/{job_id}/report` → `report_markdown`, `citations`, `payment_ledger`,
  `total_spent_usdc` (409 until the job is `completed`). Each item in `citations` now also has:
  - `cost_usdc`: this claim's share of the payment that produced it (see "Cost-per-claim breakdown" below)
  - `tx_hash`, `paid_at`: the settling transaction and its timestamp
  - `review_confidence` (`"strong"`/`"moderate"`/`"weak"`), `review_note`: an independent LLM's self-critique of this claim - see "AI self-critique" below, `null` if no LLM key was set
  The report itself also carries:
  - `trust_score`, `weak_claims_count`: aggregate of the self-critique pass, `null` if it didn't run
  - a provenance receipt (nullable if it couldn't be submitted): `report_hash` (SHA-256 of the canonical `{report_markdown, citations, payment_ledger}`), `provenance_tx_hash`, `provenance_explorer_url`
- `GET /api/ledger` → raw payment ledger across all jobs (for the frontend/demoing)
- `GET /api/quota` → this caller's remaining daily budget, per-job maximum and rate-limit
  usage — so a client can check its headroom rather than discover it by being refused
- `POST /api/fact-check` → `{claim, context?}` → one claim checked against a second source
  for a single x402 settlement (~$0.003, a few seconds), returning the verdict plus its
  `tx_hash`. Synchronous, unlike `/api/research`: it runs the one paid call directly rather
  than planning a job, because the full pipeline is far too heavy for one sentence. This is
  what the browser extension calls.

Authentication is optional by default and required when `REQUIRE_API_KEY=true`; send the
key as `Authorization: Bearer <key>` or `X-API-Key: <key>`. `POST /api/research` answers
**402** with the specific limit that was hit when a caller is out of budget, having created
no job and charged nothing. See "Spend limits and API keys" below.

Full base JSON shapes are in [`docs/x402-research-agent-spec.md`](docs/x402-research-agent-spec.md) §7.

### Cost-per-claim breakdown

Each citation is attributed to the exact payment that produced it. Splitting rule
(picked so `sum(cost_usdc)` across all citations always equals `total_spent_usdc`
exactly, in case a judge adds it up live):

- A task's paid amount is split **evenly** across every claim attributed to it
  (never duplicated - duplicating would make the sum overshoot the moment any task
  backs more than one claim).
- Tasks that are never the direct source of a claim (`summarize`, `compile_report`
  itself, or anything unused) still cost money - that's "orchestration overhead,"
  spread evenly across every citation's `cost_usdc`.
- The last citation absorbs any floating-point rounding residual so the displayed
  numbers sum exactly, not just approximately.

See `orchestrator/citations.py` for the implementation.

## What's real vs mocked

| Piece | Real | Falls back to (no key/funds) |
|---|---|---|
| x402 payment sign/verify/settle | Always real — actual EIP-3009 signature, real facilitator call (`https://x402.org/facilitator`) | — |
| Search reverse auction | Always real — 3 independent processes, live `/quote` calls, genuine cheapest-first selection | — |
| Search results | Tavily API if `TAVILY_API_KEY` set | Curated mock results |
| Fact-check, enrichment, summarize | LLM call (cheap tier) | Regex/keyword-overlap heuristics |
| Report synthesis | LLM call (strong tier for `/api/report`, cheap tier for `/api/report/cheap`) | Deterministic markdown template, still fully cited |
| On-chain payment settlement | Real Base Sepolia tx, once the buyer wallet holds testnet USDC | Payment correctly signs + verifies, then fails at settle with `invalid_exact_evm_insufficient_balance` until funded |
| On-chain provenance receipt | Real Base Sepolia tx (needs testnet ETH for gas, separate from the USDC payments) | Skipped gracefully; `report_hash`/`provenance_tx_hash` are `null`, job still completes |
| Reputation weighting in the search auction | Always real — success rate computed live from the SQLite ledger's actual payment history for each provider | Defaults to 1.0 (pure cheapest-first) when a provider has no history yet |
| Live AI reasoning stream (`narration`) | LLM call (cheap tier) per decision point | Deterministic templated sentence, still populated |
| Self-critique / trust score | LLM call (cheap tier), independent of report synthesis | Citations returned unreviewed; `trust_score`/`weak_claims_count` are `null`, never a fabricated value |

Nothing ever touches mainnet or real money.

## Budget guard

Before paying for each task, the orchestrator checks cumulative job spend against
`budget_cap_usdc`. `compile_report` is the only task type with two tiers (premium vs
cheap, served by two independent processes); if the premium tier would blow the
budget but the cheap tier wouldn't, the orchestrator downgrades that one task
(`status: tier_downgraded` in the trace) instead of failing the job. If even the
cheapest option would exceed the cap, the job stops as `budget_exceeded`.

## Spend limits and API keys

The budget guard above governs one job against the cap it was given. These limits govern
who may ask for a job at all — because `budget_cap_usdc` arrives in the request, and a
caller can ask for anything, repeatedly. Once the orchestrator is reachable by anyone
(an MCP client, a public URL), the wallet is only as safe as this layer.

Three independent ceilings, all checked before a job is created (`shared/api_keys.py`):

- **per-job** — the largest `budget_cap_usdc` a single job may request
- **per-caller** — rolling UTC-day spend for one API key, or for all anonymous callers pooled
- **global** — rolling UTC-day spend across everything, the last line of defence on the wallet

Spend is counted as *exposure*, not just settled payments: a running job's full budget
counts against its caller until it finishes, so launching fifty jobs in the same second
cannot slip past a cap that only looked at what had already been paid. When a job ends —
completed, failed or crashed — its reservation is released and it counts at what it
actually paid.

A refusal is HTTP **402** naming the limit that was hit and what would still fit. Nothing
is charged: the job is never created.

```bash
python scripts/manage_keys.py create "claude-desktop"   # printed once, only its hash is stored
python scripts/manage_keys.py list                      # keys, today's spend, rate-limit usage
python scripts/manage_keys.py revoke ar_sk_AbCdEfGh     # deactivate by prefix
```

Keys are optional by default: with `REQUIRE_API_KEY` unset, anonymous callers work and
share one pooled quota, so the React frontend and the demo scripts need no changes. Set
`REQUIRE_API_KEY=true` before exposing the orchestrator to the internet — anonymous jobs
are then blocked entirely, and one key's jobs stop being readable with another's key. A
*wrong* key is always rejected, key-required or not; silently downgrading it to anonymous
would hand out anonymous's quota and hide the typo.

`/api/ledger` stays public either way — every row in it is a settlement already public on
BaseScan, and the site's live counters read it without a key.

## Use it from Claude Desktop / Cursor (MCP)

`mcp_server/` exposes the whole agent to any MCP client as five tools — `run_research`,
`get_research_status`, `get_research_report`, `check_research_quota`, `get_payment_ledger`.

```bash
./run_all.sh                                        # orchestrator + 8 services
python scripts/manage_keys.py create "claude-desktop"
python -m mcp_server                                # stdio, for a local MCP client
```

It is a thin adapter with no wallet, no payment code and no research logic of its own —
it calls the same HTTP API the web console calls, so there is exactly one implementation
of a research job and one place where money is spent. x402, the auction and the failover
stay invisible to the caller; the settlement proof does not, since every citation comes
back carrying the transaction hash that paid for it.

To serve many clients from one deployment instead of a local copy each:

```bash
python -m mcp_server --transport streamable-http --host 0.0.0.0 --port 8080
```

Clients then point at `https://your-host/mcp` and install nothing — no repo, no wallet,
no LLM keys. Each sends its own API key per request, which the server forwards to the
orchestrator, so quotas are per-user rather than pooled onto one shared key. `AUTORESEARCH_API_KEY`
stays unset when hosting: it is the single-user fallback for stdio, where a request has
no headers to carry a key.

Client config for Claude Desktop and Claude Code is in [mcp_server/README.md](mcp_server/README.md).

`mcp_server/` ships its own `Dockerfile` and `docker-compose.yml` and deploys
independently of the rest of the repo — it only speaks HTTP to the orchestrator, so the
image carries none of x402/web3/eth-account. See "Deploying with Docker" in
[mcp_server/README.md](mcp_server/README.md) for the full flow, including the reverse
proxy / TLS step a remote MCP client's config requires.

## Fact-check anything you're reading (browser extension)

`extension/` is a Chrome extension for the case the console can't cover: someone reading an
article who wants one sentence verified without leaving the page.

Select any text, right-click, **Fact-check with AutoResearch** — a panel appears over the
page with a verdict, a confidence score, the corroborating source, and the BaseScan link
for the payment that produced it. One x402 settlement, ~$0.003, a few seconds. A second
menu item runs the full research pipeline on the selection instead.

Chrome no longer permits loading unpacked extensions from the command line, so install it
by hand: `chrome://extensions` → Developer mode → **Load unpacked** → pick `extension/`.
Details and the hosted-backend caveat are in [extension/README.md](extension/README.md).

## Resilience / live failover

Independent of the budget guard, every task call is wrapped in a failure-triggered
retry: if a call to a microservice fails for any reason (connection refused because
the process was killed, a timeout, or a non-2xx/402 response), the orchestrator:

1. Logs a `failed` step for that attempt (with the real error).
2. If an alternate tier exists for that task type (currently: `compile_report`
   premium ↔ cheap) *and* it still fits under the remaining budget, retries against
   it immediately, marking the step `provider_switched: true` on success.
3. If no alternate exists, or the alternate would exceed the budget, the step ends
   `failed` with a clear reason - the orchestrator never hangs or crashes. Tasks with
   only one tier (search, fact-check, enrichment, summarize) fall into this case:
   losing that service degrades the report (less context for that branch) rather
   than losing the whole job, since only `compile_report` failing on every available
   tier fails the job itself.

Demo it live: run `./run_all.sh`, submit a job, and `kill -9` the process on port
`4005` (report-gen premium) while it's running - `report-gen-cheap` (port `4006`)
picks it up. `scripts/test_failover.py` automates exactly this.

## Reverse auction (search)

`web_search` is the one task type with a live reverse auction instead of a fixed
tier: `services/search`, `services/search-b`, and `services/search-c` are 3
independent processes, each picking its own price pseudo-randomly at startup
(within a realistic $0.0015-$0.0045 band) and exposing it on an unauthenticated
`GET /quote`. Before paying, the orchestrator queries `/quote` on all 3 in parallel
and pays whichever wins on a **reputation-weighted score**, not just raw price -
`_run_auction()` in `orchestrator/orchestrate.py`.

**Reputation weighting:** `shared/ledger.get_provider_stats()` computes each
provider's historical success rate (paid vs failed, per task type) from every past
job in the SQLite ledger. The score is `price / max(success_rate, 0.05)`, so a
provider that's been failing gets its effective cost inflated - a slightly pricier
but more reliable provider can win over the raw-cheapest one. A provider with no
history yet defaults to a 1.0 success rate, so a fresh ledger behaves exactly like
plain cheapest-first (no cold-start penalty). Both the raw price and the success
rate used are visible per provider in `quotes_considered` on the trace, and the
frontend's "Reverse auction details" expander shows the full breakdown.

A provider's quoted price and its x402-charged price are always the same value
(set once at process startup) - a real x402 payment can't be authorized against a
price that changes between the quote and the charge, so "live" here means
"unpredictable and independently verified before every job," not "changes
mid-request." Restarting a provider is what changes its price, simulating shifting
market conditions - a nice thing to demo (`kill -9` one of the 3, watch a different
provider win the next job's auction).

If the currently-cheapest provider's payment fails for any reason, the same
failure-triggered retry from the resilience section above kicks in, walking down
the remaining providers in ascending price order - `scripts/test_e2e.py`'s output
against an unfunded wallet actually demonstrates this well: every provider fails
(no funds), so you can watch it try all 3 in cheapest-first order.

## Live AI reasoning stream

At every interesting decision point - the auction result, a budget-driven tier
downgrade, a task skipped for exceeding the budget, a failover recovery, a dead-end
failure, the final self-critique summary, and job completion - the orchestrator asks
a cheap-tier LLM call to narrate *why* in one short sentence using the actual numbers
involved (`shared/rationale.py`'s `explain()`), and appends it to the job's
`narration` feed (`GET .../trace`). The Streamlit frontend renders this as a running
feed while the job is in progress, so a viewer sees the agent explain its own
decisions live rather than just a static status table. It's deliberately narrow-scoped
to genuinely interesting moments (not narrated: the common case of a single-provider
task succeeding on the first try) so the feed stays signal, not noise. Always
degrades to a deterministic templated sentence (never blocks or fails the job) if no
LLM key is set or the narration call itself fails/times out - narration is a demo
nicety, not a load-bearing part of the pipeline.

## AI self-critique (trust score)

After the report is compiled, a second, independent cheap-tier LLM call reviews
every citation and flags how well-supported it looks - `strong` / `moderate` / `weak`
plus a short note (e.g. "no source URL", "disputed claim") - before the report is
returned (`shared/self_critique.py`). This is distinct from the mid-pipeline
`fact_check` task (which only verifies one specific claim against context): this is
the agent red-teaming its *own final output*, holistically, right before it ships.
The per-citation `review_confidence`/`review_note` and the report-level `trust_score`
(weighted average: strong=1.0, moderate=0.6, weak=0.2) / `weak_claims_count` are all
`null` rather than a fabricated default if no LLM key is set or the critique call
fails - an unreviewed report is never shown as 100% trustworthy.

## On-chain provenance receipt

Once a report is compiled, the orchestrator computes a SHA-256 hash over the exact
JSON it's about to return (`report_markdown` + `citations` + `payment_ledger`,
canonicalized with sorted keys) and submits one small Base Sepolia transaction from
the buyer wallet to itself, carrying that hash as calldata (`shared/provenance.py`).
Anyone can then:

1. Fetch `GET /api/research/{job_id}/report`.
2. Recompute `SHA-256({report_markdown, citations, payment_ledger})` the same way.
3. Check it matches the calldata on `provenance_tx_hash` (via BaseScan or a node) -
   proving the report wasn't edited after the fact.

This is the one transaction in the whole system that **isn't gasless** - x402's
EIP-3009 `transferWithAuthorization` only covers the USDC payments; a transaction
carrying arbitrary calldata needs normal gas, paid in Base Sepolia ETH. It's
best-effort: if the wallet has no testnet ETH, this step fails and is logged, but
the job still completes normally with `report_hash`/`provenance_tx_hash` left
`null` - a demo without ETH funding loses only this one bonus field, nothing else.

## Troubleshooting

- **`invalid_exact_evm_insufficient_balance`** — buyer wallet has no testnet USDC yet. Fund it (see Setup) and re-run.
- **`insufficient funds for gas * price + value`** — that's the provenance receipt tx failing because the buyer wallet has no testnet ETH; harmless, the job still completes without a `provenance_tx_hash` (see "On-chain provenance receipt").
- **`BUYER_PRIVATE_KEY is missing or malformed`** — run `python wallet/generate_wallet.py`.
- **Port already in use** — another `run_all.sh` is still running; `pkill -f uvicorn` / `pkill -f "services/"` or change the `PORT_*` vars in `.env`.
- **LLM calls failing** — every service catches LLM errors and falls back to its heuristic automatically; check the service's stdout log for the underlying error (bad key, rate limit, etc).
