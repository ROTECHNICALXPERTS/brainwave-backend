# Deploying the backend on Coolify

One repo, one image, one container, one domain. The container runs the orchestrator plus
all 8 x402-gated microservices; only the orchestrator is ever reachable from outside.

## Coolify settings

| Setting | Value |
| --- | --- |
| Build Pack | **Dockerfile** — *not* Nixpacks |
| Port Exposes | `4000` |
| Domain | `https://api.yourdomain.com` |
| Health Check Path | `/health` |
| Persistent Storage | `/app/data` |

**Build Pack must be Dockerfile.** Nixpacks will detect Python, start only the
orchestrator, and report a successful deploy — then every research job fails the moment it
tries to reach the 8 services, because they were never started.

**The `/app/data` volume is not optional.** `data/ledger.db` holds the payment ledger and
the API key store. Without a persistent mount, every redeploy resets both: the ledger goes
empty and every API key you issued stops working.

## Environment variables

Set these in Coolify's environment variable UI, not in a committed file. `shared/__init__.py`
calls `load_dotenv()` without `override=True`, so real environment variables always take
precedence — the image ships an empty `.env` so nothing can shadow them.

Required:

```
BUYER_PRIVATE_KEY        buyer wallet key - signs x402 payments
BUYER_ADDRESS
SELLER_PAYTO_ADDRESS     where the services receive USDC
GROQ_API_KEY             cheap/fast tier: planner, fact-check, enrichment, summarizer
GEMINI_API_KEY           strong tier: final report synthesis
TAVILY_API_KEY           real web search (falls back to mock data if unset)
REQUIRE_API_KEY=true     see below
```

Everything else has a working default in `shared/config.py` — the service ports and the
`http://localhost:400X` service URLs are already correct for this single-container layout
and should be left alone.

### Set `REQUIRE_API_KEY=true`

It defaults to **false** (`shared/api_keys.py:64`). Deployed with the default, anyone who
finds the URL can spend the buyer wallet. The global ceiling (`GLOBAL_DAILY_CAP_USDC`,
default 5.0) bounds the damage, but your demo is dead for the rest of the day.

With it on, mint yourself a key:

```bash
# inside the running container
python scripts/manage_keys.py create "frontend"
```

### Lock down CORS

`orchestrator/main.py:30` currently allows all origins. Once the frontend has a real URL,
restrict `allow_origins` to it.

## Verify

```
https://api.yourdomain.com/health   ->   {"ok":true,"service":"orchestrator"}
```

If that returns in a browser, the deploy is good. Then point the frontend at it by setting
`VITE_API_BASE_URL=https://api.yourdomain.com` in Vercel and redeploying.

## Wallet

Base Sepolia **testnet USDC** funds the payments; x402 settlements are gasless (EIP-3009),
so no ETH is needed for them. A small amount of testnet **ETH** in the buyer wallet unlocks
the one non-gasless feature, the on-chain provenance receipt — without it reports still
work, they just come back with `provenance_tx_hash: null`.

```bash
python wallet/check_balance.py       # inside the container
```

## MCP server (optional, later)

`mcp_server/` deploys as a **separate Coolify app from this same repo**: Build Pack
`Dockerfile`, Base Directory `/mcp_server`, port `8080`, domain `mcp.yourdomain.com`, and
`AUTORESEARCH_API_URL=https://api.yourdomain.com`.

Do **not** set `AUTORESEARCH_API_KEY` on that app. In streamable-http mode each connecting
client sends its own key per request; setting one server-side makes it a shared fallback
that every client's usage silently pools onto — all billed to your one wallet.

## Local check before you deploy

```bash
docker build -t brainwave-backend .
docker run -p 4000:4000 --env-file .env brainwave-backend
curl localhost:4000/health
```
