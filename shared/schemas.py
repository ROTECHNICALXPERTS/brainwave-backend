from typing import Any, Optional
from pydantic import BaseModel


class ResearchRequest(BaseModel):
    query: str
    budget_cap_usdc: float


class FactCheckRequest(BaseModel):
    claim: str
    # Where the claim was read - a page title/URL when it comes from the browser
    # extension. Passed to the fact-check service as disambiguating context, never used
    # for retrieval on its own.
    context: Optional[str] = None


class FactCheckResponse(BaseModel):
    """One claim, one paid call, one settlement. Deliberately flat and synchronous: this
    is what the browser extension shows in a popover, so a job id to poll would be worse
    than simply waiting the few seconds the payment and the check actually take."""

    claim: str
    verdict: str
    confidence: Optional[float] = None
    explanation: Optional[str] = None
    evidence_url: Optional[str] = None
    method: Optional[str] = None
    cost_usdc: float
    tx_hash: Optional[str] = None
    explorer_url: Optional[str] = None


class ResearchAccepted(BaseModel):
    job_id: str


class PlannerTask(BaseModel):
    task_id: str
    type: str
    depends_on: list[str] = []
    inputs: dict[str, Any] = {}


class TaskGraph(BaseModel):
    tasks: list[PlannerTask]


class TraceStep(BaseModel):
    task_id: str
    task_type: Optional[str] = None
    status: str
    endpoint: Optional[str] = None
    amount_usdc: Optional[float] = None
    tier: Optional[str] = None
    tx_hash: Optional[str] = None
    explorer_url: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None
    timestamp: Optional[str] = None
    # Set on the step that succeeded after an earlier tier/attempt failed mid-job
    # (see orchestrate.py's retry-on-failure loop).
    provider_switched: Optional[bool] = None
    switched_from: Optional[str] = None
    switched_to: Optional[str] = None
    # Set only for task types with a live reverse auction (currently web_search) -
    # see orchestrate.py `_run_auction`.
    quotes_considered: Optional[list[dict]] = None
    chosen_provider: Optional[str] = None


class NarrationEntry(BaseModel):
    event: str
    text: str
    task_id: Optional[str] = None
    timestamp: Optional[str] = None


class TraceResponse(BaseModel):
    job_id: str
    status: str
    steps: list[TraceStep]
    # Live "AI reasoning stream" - one-line narrations of interesting orchestrator
    # decisions (auction picks, tier downgrades, failover recoveries), in order. See
    # shared/rationale.py.
    narration: list[NarrationEntry] = []
    total_spent_usdc: float
    budget_cap_usdc: float


class Citation(BaseModel):
    claim: str
    source_service: str
    source_url: Optional[str] = None
    task_id: str
    # Cost-per-claim breakdown: which payment produced this claim, and its share of
    # that payment's cost. See orchestrator/citations.py for how cost_usdc is split
    # when a task backs multiple claims, and how orchestration-only costs (summarize,
    # report synthesis) are folded in so sum(cost_usdc) always reconciles with
    # total_spent_usdc.
    cost_usdc: Optional[float] = None
    tx_hash: Optional[str] = None
    paid_at: Optional[str] = None
    # Final-report self-critique pass (see shared/self_critique.py): an independent
    # LLM review of how well-supported this specific claim looks. None if no LLM key
    # was available to run the critique - never a fabricated "strong" default.
    review_confidence: Optional[str] = None
    review_note: Optional[str] = None


class ReportResponse(BaseModel):
    job_id: str
    query: str
    report_markdown: str
    citations: list[Citation]
    payment_ledger: list[TraceStep]
    total_spent_usdc: float
    # On-chain provenance receipt (best-effort - see shared/provenance.py). None if it
    # couldn't be submitted (e.g. the buyer wallet has no testnet ETH for gas).
    report_hash: Optional[str] = None
    provenance_tx_hash: Optional[str] = None
    provenance_explorer_url: Optional[str] = None
    # Aggregate of the self-critique pass across all citations. None if the critique
    # didn't run (no LLM key) rather than a misleading default.
    trust_score: Optional[float] = None
    weak_claims_count: Optional[int] = None
