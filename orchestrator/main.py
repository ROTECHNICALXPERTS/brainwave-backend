import asyncio
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared import api_keys
from shared.api_keys import Caller
from shared.config import PORTS, PRICES, SERVICE_URLS, TASK_SERVICE_MAP
from shared.ledger import get_all_ledger
from shared.schemas import (
    FactCheckRequest,
    FactCheckResponse,
    ResearchRequest,
    ResearchAccepted,
    TraceResponse,
    ReportResponse,
)
from shared.x402_client import pay_and_fetch_json, price_to_number

from . import job_store
from .auth import assert_can_read_job, resolve_caller
from .orchestrate import run_job

app = FastAPI(title="AutoResearch Orchestrator")

# Basic CORS so a local frontend dev server can call this without a proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.post("/api/research", response_model=ResearchAccepted, status_code=202)
async def submit_research(req: ResearchRequest, caller: Caller = Depends(resolve_caller)):
    if not req.query:
        raise HTTPException(status_code=400, detail="Missing required field: query (string)")
    if req.budget_cap_usdc <= 0:
        raise HTTPException(status_code=400, detail="Missing or invalid field: budget_cap_usdc (positive number)")

    # Every job spends real testnet USDC from one shared wallet, so what the caller may
    # spend is decided here rather than taken from the request. 402 (not 403) because the
    # refusal is specifically about money: the request is well-formed and authenticated,
    # there is simply no budget left for it.
    refusal = api_keys.authorize_launch(caller, req.budget_cap_usdc)
    if refusal:
        raise HTTPException(status_code=402, detail=refusal)

    job_id = str(uuid.uuid4())
    # Recorded before the job starts: the reservation is what stops a burst of concurrent
    # launches from each seeing a spend of zero and all being approved.
    api_keys.record_job(job_id, caller.key_id, req.budget_cap_usdc)
    job_store.create_job(job_id, req.query, req.budget_cap_usdc)

    # Fire and forget; progress is polled via /trace.
    asyncio.create_task(run_job(job_id))

    return ResearchAccepted(job_id=job_id)


@app.post("/api/fact-check", response_model=FactCheckResponse)
async def fact_check_claim(req: FactCheckRequest, caller: Caller = Depends(resolve_caller)):
    """Check a single claim against a second source, paid for with one x402 settlement.

    This is the browser extension's entry point: a reporter highlights a sentence on any
    page and gets back a verdict plus the transaction that paid for it. It runs the one
    paid call directly rather than going through the planner, because the full research
    pipeline costs ~$0.019 and takes a minute - far too heavy for checking one sentence.

    Synchronous on purpose. The whole thing takes a few seconds, and a job id to poll
    would make the caller's side harder for no benefit.
    """
    claim = (req.claim or "").strip()
    if not claim:
        raise HTTPException(status_code=400, detail="Missing required field: claim (string)")
    if len(claim) > 1200:
        raise HTTPException(
            status_code=400,
            detail="claim is too long (max 1200 characters) - select a single sentence or passage",
        )

    price = PRICES["fact_check"]
    amount = price_to_number(price)

    # Same gate as a full job: this spends real USDC from the same wallet, so it is
    # subject to the same daily cap, per-job ceiling and rate limit. A fact-check costs a
    # fraction of a research job but counts as one call against the hourly limit, which
    # is the conservative reading and keeps one accounting story rather than two.
    refusal = api_keys.authorize_launch(caller, amount)
    if refusal:
        raise HTTPException(status_code=402, detail=refusal)

    job_id = f"fc-{uuid.uuid4()}"
    api_keys.record_job(job_id, caller.key_id, amount)

    service = TASK_SERVICE_MAP["fact_check"]
    url = f"{SERVICE_URLS[service['service']]}{service['path']}"

    try:
        result = await pay_and_fetch_json(
            job_id=job_id,
            task_id="fc1",
            task_type="fact_check",
            url=url,
            price_usdc=price,
            tier="standard",
            body={"claim": claim, "context": req.context},
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"Fact-check service unavailable: {err}") from err
    finally:
        # Released whether the call paid, failed or raised - otherwise a failed check
        # would hold its reservation against the caller's quota until midnight UTC.
        api_keys.settle_job(job_id)

    data = result["data"] or {}
    return FactCheckResponse(
        claim=data.get("claim", claim),
        verdict=data.get("verdict", "unverified"),
        confidence=data.get("confidence"),
        explanation=data.get("explanation"),
        evidence_url=data.get("evidence_url"),
        method=data.get("method"),
        cost_usdc=amount,
        tx_hash=result["tx_hash"],
        explorer_url=result["explorer_url"],
    )


@app.get("/api/quota")
async def get_quota(caller: Caller = Depends(resolve_caller)):
    """What this caller has left today. Exposed so an agent can check its own headroom
    instead of finding out by being refused mid-conversation."""
    return api_keys.quota_snapshot(caller)


@app.get("/api/research/{job_id}/trace", response_model=TraceResponse)
async def get_trace(job_id: str, caller: Caller = Depends(resolve_caller)):
    assert_can_read_job(caller, job_id)
    trace = job_store.serialize_trace(job_id)
    if not trace:
        raise HTTPException(status_code=404, detail="job not found")
    return trace


@app.get("/api/research/{job_id}/report", response_model=ReportResponse)
async def get_report(job_id: str, caller: Caller = Depends(resolve_caller)):
    assert_can_read_job(caller, job_id)
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"job is not completed yet (status: {job['status']})")

    trace = job_store.serialize_trace(job_id)
    return ReportResponse(
        job_id=job["job_id"],
        query=job["query"],
        report_markdown=job["report"]["report_markdown"],
        citations=job["report"]["citations"],
        payment_ledger=trace["steps"],
        total_spent_usdc=trace["total_spent_usdc"],
        report_hash=job["report"].get("report_hash"),
        provenance_tx_hash=job["report"].get("provenance_tx_hash"),
        provenance_explorer_url=job["report"].get("provenance_explorer_url"),
        trust_score=job["report"].get("trust_score"),
        weak_claims_count=job["report"].get("weak_claims_count"),
    )


# Full raw payment ledger across all jobs. Deliberately left unauthenticated even when
# REQUIRE_API_KEY is set: every row is a settlement that is already public on BaseScan,
# and the marketing site's live counters poll this without holding a key. It exposes no
# query text, no report content and no key identity - only amounts and tx hashes.
@app.get("/api/ledger")
async def get_ledger():
    return {"ledger": get_all_ledger()}


@app.get("/health")
async def health():
    return {"ok": True, "service": "orchestrator"}


if __name__ == "__main__":
    import uvicorn

    print(f"[orchestrator] listening on :{PORTS['orchestrator']}")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["orchestrator"])
