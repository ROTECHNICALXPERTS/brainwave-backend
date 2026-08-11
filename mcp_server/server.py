"""AutoResearch as an MCP server.

A thin adapter over the orchestrator's HTTP API - it holds no research logic, no wallet
and no payment code of its own. Everything it can do, it does by calling the same
endpoints the web frontend calls, so there is exactly one implementation of a research
job and one place where money is spent.

    LLM client (Claude, Cursor, ...)  ⇄  this server  ⇄  orchestrator  ⇄  8 x402 services

What the caller never sees is deliberate: x402, the wallet, the reverse auction and the
per-provider failover are implementation details of "run this research". What they do
see is the settlement proof - every claim carries the transaction hash that paid for it,
which is the part that cannot be faked and the reason this is worth exposing as a tool
at all.

Configuration (environment):

    AUTORESEARCH_API_URL   orchestrator base URL   (default http://localhost:4000)
    AUTORESEARCH_API_KEY   API key, if the orchestrator requires one
    AUTORESEARCH_TIMEOUT   per-request timeout in seconds (default 30)

Run it over stdio (for Claude Desktop / Claude Code):

    python -m mcp_server

or over HTTP, to serve many clients from one deployment:

    python -m mcp_server --transport streamable-http --port 8080
"""
import os
from typing import Any

import httpx

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

API_URL = os.getenv("AUTORESEARCH_API_URL", "http://localhost:4000").rstrip("/")
API_KEY = os.getenv("AUTORESEARCH_API_KEY", "").strip()


def _timeout() -> float:
    try:
        return float(os.getenv("AUTORESEARCH_TIMEOUT", "30"))
    except ValueError:
        return 30.0


server = MCPServer(
    name="autoresearch",
    title="AutoResearch",
    version="1.0.0",
    instructions=(
        "AutoResearch runs multi-step research through a network of independent services "
        "that are paid per call in USDC over the x402 protocol (HTTP 402) on Base Sepolia. "
        "Every factual claim in a finished report carries the on-chain transaction hash of "
        "the payment that produced it.\n\n"
        "Typical flow: call run_research to start a job, then poll get_research_status every "
        "few seconds until it reports status 'completed', then call get_research_report for "
        "the write-up. Jobs are not instant - budget roughly 30-90 seconds.\n\n"
        "Research costs real (testnet) money and is capped per caller. Use check_research_quota "
        "if you need to know how much is left before starting a job."
    ),
)


def _caller_key(ctx: Context | None) -> str:
    """Which API key to present to the orchestrator for this particular call.

    Over stdio there is one user - the key comes from the environment, set by whoever
    configured the MCP client. Over HTTP there are many, so the key travels on each
    request and is forwarded as-is: that is what gives every client its own quota
    instead of pooling them all onto one shared key. `ctx.headers` is None on stdio,
    which makes the environment the natural fallback rather than a special case.
    """
    headers = ctx.headers if ctx is not None else None
    if headers:
        forwarded = headers.get("authorization") or headers.get("x-api-key")
        if forwarded:
            return forwarded.split(None, 1)[1] if forwarded.lower().startswith("bearer ") else forwarded
    return API_KEY


async def _request(method: str, path: str, ctx: Context | None = None, **kwargs: Any) -> dict:
    """Call the orchestrator and normalise every failure into a dict the model can act on.

    Errors are returned rather than raised: a refusal like "daily budget spent" is a real
    answer that the model should relay to the user, not a crash. The orchestrator's own
    `detail` message is passed through verbatim - it already explains which limit was hit
    and what would fit.
    """
    url = f"{API_URL}{path}"
    key = _caller_key(ctx)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
    except httpx.ConnectError:
        return {
            "ok": False,
            "error": (
                f"Cannot reach the AutoResearch orchestrator at {API_URL}. "
                "It may not be running, or AUTORESEARCH_API_URL may be pointing somewhere else."
            ),
        }
    except httpx.TimeoutException:
        return {"ok": False, "error": f"The orchestrator at {API_URL} did not respond in time."}

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        out = {"ok": False, "status": resp.status_code, "error": detail}
        if resp.status_code == 401:
            out["hint"] = "Set AUTORESEARCH_API_KEY to a valid key for this orchestrator."
        elif resp.status_code == 402:
            out["hint"] = "This is a spending limit, not a failure - the job never started, so nothing was charged."
        return out

    try:
        return {"ok": True, "data": resp.json()}
    except Exception:
        return {"ok": False, "error": f"Orchestrator returned a non-JSON response: {resp.text[:200]}"}


@server.tool(
    title="Run research",
    description=(
        "Start an autonomous research job on a question. Returns immediately with a job_id - "
        "the work happens in the background, so you must poll get_research_status until it "
        "reports 'completed' (usually 30-90 seconds), then fetch the write-up with "
        "get_research_report.\n\n"
        "The agent plans the work itself, runs a live price auction between competing search "
        "providers, pays each service in USDC over x402, and produces a report where every "
        "claim is traceable to the payment that sourced it.\n\n"
        "budget_cap_usdc is a hard ceiling for this one job, in USDC. A typical job settles "
        "for about 0.019 USDC, so 0.05 is comfortable headroom. The server enforces its own "
        "per-caller maximum on top of this and will refuse anything above it."
    ),
)
async def run_research(query: str, budget_cap_usdc: float = 0.05, ctx: Context | None = None) -> dict:
    if not query or not query.strip():
        return {"ok": False, "error": "query is required - pass the research question as a string."}
    if budget_cap_usdc <= 0:
        return {"ok": False, "error": "budget_cap_usdc must be greater than 0."}

    result = await _request(
        "POST", "/api/research", ctx=ctx, json={"query": query.strip(), "budget_cap_usdc": budget_cap_usdc}
    )
    if not result["ok"]:
        return result

    job_id = result["data"]["job_id"]
    return {
        "ok": True,
        "job_id": job_id,
        "query": query.strip(),
        "budget_cap_usdc": budget_cap_usdc,
        "next_step": (
            f"Research started. Poll get_research_status(job_id='{job_id}') every ~5 seconds. "
            "When status is 'completed', call get_research_report to read it."
        ),
    }


@server.tool(
    title="Check research status",
    description=(
        "Check how a running research job is progressing. Poll this every few seconds after "
        "run_research.\n\n"
        "status is one of: queued, running, completed, budget_exceeded, failed. Only "
        "'completed' means a report is available. 'budget_exceeded' means the job stopped "
        "early because it hit its budget cap - any partial work it did pay for is still on "
        "the ledger.\n\n"
        "Also returns what has been spent so far and a short live narration of the "
        "orchestrator's own decisions (which provider won an auction, when it fell back to a "
        "cheaper tier, and why)."
    ),
)
async def get_research_status(job_id: str, ctx: Context | None = None) -> dict:
    result = await _request("GET", f"/api/research/{job_id}/trace", ctx=ctx)
    if not result["ok"]:
        return result

    trace = result["data"]
    steps = trace.get("steps") or []
    done = [s for s in steps if s.get("status") in ("completed", "paid")]

    return {
        "ok": True,
        "job_id": trace.get("job_id"),
        "status": trace.get("status"),
        "is_finished": trace.get("status") in ("completed", "budget_exceeded", "failed"),
        "report_available": trace.get("status") == "completed",
        "tasks_done": len(done),
        "tasks_total": len(steps),
        "spent_usdc": trace.get("total_spent_usdc"),
        "budget_cap_usdc": trace.get("budget_cap_usdc"),
        # Trimmed hard on purpose: a full trace carries every quote from every auction and
        # would swamp the model's context for no benefit while it is only waiting.
        "steps": [
            {
                "task": s.get("task_type") or s.get("task_id"),
                "status": s.get("status"),
                "cost_usdc": s.get("amount_usdc"),
                "provider": s.get("chosen_provider") or s.get("tier"),
                "tx_hash": s.get("tx_hash"),
            }
            for s in steps
        ],
        "recent_activity": [n.get("text") for n in (trace.get("narration") or [])[-5:]],
    }


@server.tool(
    title="Get research report",
    description=(
        "Fetch the finished report for a completed research job: the write-up in markdown, "
        "plus every claim's citation with the x402 transaction hash and BaseScan link for the "
        "payment that sourced it.\n\n"
        "Only works once get_research_status reports 'completed' - calling it earlier returns "
        "an error saying the job is still running.\n\n"
        "trust_score, when present, is an independent LLM review of how well-supported the "
        "report's claims are (0-1). weak_claims_count is how many claims that review flagged "
        "as thin. Both are absent rather than guessed if the review could not run."
    ),
)
async def get_research_report(job_id: str, ctx: Context | None = None) -> dict:
    result = await _request("GET", f"/api/research/{job_id}/report", ctx=ctx)
    if not result["ok"]:
        return result

    report = result["data"]
    return {
        "ok": True,
        "job_id": report.get("job_id"),
        "query": report.get("query"),
        "report_markdown": report.get("report_markdown"),
        "total_spent_usdc": report.get("total_spent_usdc"),
        "trust_score": report.get("trust_score"),
        "weak_claims_count": report.get("weak_claims_count"),
        "citations": [
            {
                "claim": c.get("claim"),
                "source": c.get("source_service"),
                "source_url": c.get("source_url"),
                "cost_usdc": c.get("cost_usdc"),
                "tx_hash": c.get("tx_hash"),
                "review_confidence": c.get("review_confidence"),
            }
            for c in (report.get("citations") or [])
        ],
        "provenance": {
            "report_hash": report.get("report_hash"),
            "tx_hash": report.get("provenance_tx_hash"),
            "explorer_url": report.get("provenance_explorer_url"),
        },
    }


@server.tool(
    title="Check research quota",
    description=(
        "Show how much research budget this caller has left today, before starting a job. "
        "Research spends real testnet USDC, so every caller has a daily cap, a per-job "
        "maximum and an hourly rate limit. Use this if a job was refused for budget reasons, "
        "or to check headroom before starting an expensive run."
    ),
)
async def check_research_quota(ctx: Context | None = None) -> dict:
    result = await _request("GET", "/api/quota", ctx=ctx)
    if not result["ok"]:
        return result
    return {"ok": True, **result["data"]}


@server.tool(
    title="Get payment ledger",
    description=(
        "Show recent x402 settlements across all research jobs: which service was paid, how "
        "much, and the Base Sepolia transaction hash for each. This is the audit trail - "
        "every entry is independently verifiable on BaseScan. Useful for showing what the "
        "agent has actually paid for rather than what it claims to have done."
    ),
)
async def get_payment_ledger(limit: int = 20, ctx: Context | None = None) -> dict:
    result = await _request("GET", "/api/ledger", ctx=ctx)
    if not result["ok"]:
        return result

    entries = result["data"].get("ledger") or []
    # Only settled rows: a 'paying' row is an attempt still in flight and is superseded by
    # a 'paid' row for the same payment, so including both would show every payment twice.
    settled = [e for e in entries if e.get("status") in ("paid", "completed") and e.get("tx_hash")]
    recent = settled[-max(1, min(limit, 100)) :]

    return {
        "ok": True,
        "total_settlements": len(settled),
        "total_paid_usdc": round(sum(e.get("amount_usdc") or 0 for e in settled), 6),
        "network": "base-sepolia",
        "recent": [
            {
                "service": e.get("task_type"),
                "amount_usdc": e.get("amount_usdc"),
                "provider": e.get("tier"),
                "tx_hash": e.get("tx_hash"),
                "explorer_url": e.get("explorer_url"),
                "timestamp": e.get("timestamp"),
            }
            for e in reversed(recent)
        ],
    }
