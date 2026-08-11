# AutoResearch MCP server

Exposes AutoResearch to any MCP client — Claude Desktop, Claude Code, Cursor — as five
tools. The client asks a research question; the agent plans the work, runs a live price
auction between competing services, pays each one in USDC over x402 on Base Sepolia, and
returns a report where every claim carries the transaction hash that paid for it.

The server itself is a thin adapter. It holds no wallet, no payment code and no research
logic — it calls the same orchestrator HTTP API the web frontend calls, so there is one
implementation of a research job and one place where money is spent.

```
Claude / Cursor  ⇄  MCP server  ⇄  orchestrator  ⇄  8 x402-gated microservices
```

## Tools

| Tool | What it does |
|---|---|
| `run_research` | Starts a job. Returns a `job_id` immediately — the work runs in the background. |
| `get_research_status` | Poll this until `status` is `completed`. Shows progress, spend, and a live narration of the orchestrator's decisions. |
| `get_research_report` | The finished write-up, plus every citation with its payment tx hash. |
| `check_research_quota` | How much budget this caller has left today. |
| `get_payment_ledger` | Recent x402 settlements with BaseScan links — the audit trail. |

Jobs are not instant. A typical run takes 30–90 seconds and settles about **$0.019** in
testnet USDC across six paid service calls.

## Setup

**1. Run the backend.** From the repo root:

```bash
./run_all.sh
```

This starts the orchestrator on `:4000` and all eight microservices.

**2. Mint an API key.** Research spends real testnet USDC, so callers are capped:

```bash
python scripts/manage_keys.py create "claude-desktop"
```

The key is printed once and never stored — only its hash goes to the database. If you
lose it, revoke it and mint another. Defaults are 0.50 USDC/day (~25 jobs), 0.10 USDC
max per job, 10 jobs/hour; override with `--daily-cap`, `--max-job-budget` and
`--max-jobs-per-hour`.

A key is optional while `REQUIRE_API_KEY` is unset — anonymous callers work and share one
pooled quota, which is what keeps local development friction-free. Set `REQUIRE_API_KEY=true`
before exposing the orchestrator to the internet.

**3. Point your MCP client at it.**

<details open>
<summary><b>Claude Desktop</b> — <code>claude_desktop_config.json</code></summary>

```json
{
  "mcpServers": {
    "autoresearch": {
      "command": "/absolute/path/to/AutoAgent/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/absolute/path/to/AutoAgent",
      "env": {
        "AUTORESEARCH_API_URL": "http://localhost:4000",
        "AUTORESEARCH_API_KEY": "ar_sk_your_key_here"
      }
    }
  }
}
```

The config file lives at `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS and `%APPDATA%\Claude\claude_desktop_config.json` on Windows. Restart Claude
Desktop after editing it.
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add autoresearch \
  --env AUTORESEARCH_API_URL=http://localhost:4000 \
  --env AUTORESEARCH_API_KEY=ar_sk_your_key_here \
  -- /absolute/path/to/AutoAgent/.venv/bin/python -m mcp_server
```
</details>

<details>
<summary><b>Anything else (manual stdio)</b></summary>

```bash
AUTORESEARCH_API_KEY=ar_sk_your_key_here python -m mcp_server
```

Speaks MCP over stdio, so stdout carries the protocol — don't pipe anything else into it.
</details>

## Serving many clients from one deployment

Instead of every user running their own copy over stdio, run one HTTP endpoint:

```bash
python -m mcp_server --transport streamable-http --host 0.0.0.0 --port 8080
```

Clients then point at `http://your-host:8080/mcp` and install nothing — no repo, no
Python, no wallet, no LLM keys of their own:

```json
{
  "mcpServers": {
    "autoresearch": {
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer ar_sk_their_own_key" }
    }
  }
}
```

Each client sends **its own** key on every request and the server forwards that key to
the orchestrator, so quotas are genuinely per-user rather than pooled onto one shared
key. Leave `AUTORESEARCH_API_KEY` unset when hosting — it exists as the single-user
fallback for stdio, where there are no request headers to read a key from, and setting
it in a hosted deployment would silently hand your key to any client that sends none.

Mint one key per consumer so you can see and revoke them individually:

```bash
python scripts/manage_keys.py create "acme-corp" --daily-cap 0.25
```

One thing worth being deliberate about: **you** run the wallet, so you are paying for
every client's research. The caps are what make that safe — they are not decoration.

### Deploying with Docker

`mcp_server/` ships its own `Dockerfile` and is independently deployable — it imports
nothing from the rest of this repo (see `server.py`; it only speaks HTTP to the
orchestrator), so the image never needs x402/web3/eth-account or the 8 microservices.
Copying just this folder to a host is enough.

```bash
# from mcp_server/
docker build -t autoresearch-mcp .
docker run -d --name autoresearch-mcp --restart unless-stopped \
  -e AUTORESEARCH_API_URL=https://your-deployed-orchestrator \
  -p 8080:8080 \
  autoresearch-mcp
```

Or with the included `docker-compose.yml`:

```bash
AUTORESEARCH_API_URL=https://your-deployed-orchestrator docker compose up -d
```

**This container needs a domain and TLS in front of it.** MCP clients (Claude Desktop's
remote-server config, etc.) expect an `https://` URL — port 8080 with a bare IP will not
work as a client-facing address. Put a reverse proxy in front and point it at
`localhost:8080` (or the container's published port). The two common choices:

- **Caddy** — simplest, gets a certificate automatically:
  ```
  mcp.your-domain.com {
      reverse_proxy localhost:8080
  }
  ```
- **nginx + certbot** — if that's already your standard setup, a normal
  `proxy_pass http://localhost:8080;` server block works; the streamable-http transport
  is plain HTTP/1.1 POST + SSE, nothing exotic for a proxy to handle. Just make sure
  buffering doesn't sit between the proxy and the client — SSE responses need to stream
  through as they're written, not wait to be buffered in full.

Once that's up, the client config's `url` becomes `https://mcp.your-domain.com/mcp`
(the SDK always serves under a `/mcp` path — see `--streamable-http-path` if you need to
change it).

**Where the orchestrator itself runs is a separate decision.** `AUTORESEARCH_API_URL` can
point anywhere reachable from this container: another container on the same Docker
network (`http://orchestrator:4000`), a different host, or a tunnel. The orchestrator
needs a platform that keeps a process running continuously with a persistent disk —
its job state is in-memory and the payment ledger is a SQLite file, so a serverless /
cold-start platform will lose both between requests.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUTORESEARCH_API_URL` | `http://localhost:4000` | Orchestrator base URL |
| `AUTORESEARCH_API_KEY` | *(none)* | Fallback key, used only when the request carries none (i.e. stdio) |
| `AUTORESEARCH_TIMEOUT` | `30` | Per-request timeout, seconds |

## Spend limits

Three independent ceilings, all enforced server-side in the orchestrator — a client's
requested `budget_cap_usdc` is a ceiling for one job, never a grant:

- **per-job** — the largest budget a single job may request
- **per-caller** — rolling UTC-day spend for one key (or for all anonymous callers pooled)
- **global** — rolling UTC-day spend across everything, the last line of defence on the wallet

Spend is counted as *exposure*: a running job's full budget counts against its caller
until it finishes, so launching fifty jobs at once cannot slip past a cap that only
looked at settled payments. When a job ends, its reservation is released and it counts
at what it actually paid.

A refusal comes back as HTTP **402** with the limit that was hit and what would fit.
Nothing is charged — the job is never created.

```bash
python scripts/manage_keys.py list           # keys, today's spend, rate-limit usage
python scripts/manage_keys.py revoke ar_sk_… # deactivate by prefix
```

## Testnet only

Every payment is Base Sepolia testnet USDC (chain 84532). No mainnet funds are involved
anywhere in this project.
