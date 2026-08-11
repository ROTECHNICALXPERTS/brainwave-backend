"""In-memory job store. Fine for a single-process hackathon demo; a restart loses
in-flight jobs (the payment ledger itself is still persisted to SQLite separately)."""
from datetime import datetime, timezone

from shared import ledger

_jobs: dict[str, dict] = {}


def create_job(job_id: str, query: str, budget_cap_usdc: float) -> dict:
    job = {
        "job_id": job_id,
        "query": query,
        "budget_cap_usdc": budget_cap_usdc,
        "status": "queued",  # queued | running | completed | budget_exceeded | failed
        "steps": {},  # task_id -> step dict
        "narration": [],  # ordered list of {timestamp, task_id, event, text} - live AI reasoning feed
        "graph": None,
        "report": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def update_job(job_id: str, **patch) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    job.update(patch)
    return job


def upsert_step(job_id: str, task_id: str, **patch) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    existing = job["steps"].get(task_id, {"task_id": task_id})
    merged = {**existing, **patch, "timestamp": datetime.now(timezone.utc).isoformat()}
    job["steps"][task_id] = merged
    return merged


def append_narration(job_id: str, event: str, text: str, task_id: str | None = None) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    entry = {"event": event, "text": text, "task_id": task_id, "timestamp": datetime.now(timezone.utc).isoformat()}
    job["narration"].append(entry)
    return entry


def serialize_trace(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "steps": list(job["steps"].values()),
        "narration": job["narration"],
        "total_spent_usdc": round(ledger.get_total_spent_for_job(job_id), 6),
        "budget_cap_usdc": job["budget_cap_usdc"],
    }
