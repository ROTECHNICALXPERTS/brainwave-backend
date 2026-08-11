"""SQLite-backed payment ledger. Every x402 payment attempt (paying/paid/failed) is
recorded here so the trace/report endpoints and the demo's payment-ledger view have
a durable, queryable record independent of in-memory job state."""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_type TEXT,
                endpoint TEXT,
                amount_usdc REAL,
                tier TEXT,
                status TEXT NOT NULL,
                tx_hash TEXT,
                explorer_url TEXT,
                error TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.commit()


_init_db()


def append_payment(
    *,
    job_id: str,
    task_id: str,
    task_type: str | None = None,
    endpoint: str | None = None,
    amount_usdc: float | None = None,
    tier: str | None = None,
    status: str,
    tx_hash: str | None = None,
    explorer_url: str | None = None,
    error: str | None = None,
) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO payments
                (job_id, task_id, task_type, endpoint, amount_usdc, tier, status, tx_hash, explorer_url, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, task_id, task_type, endpoint, amount_usdc, tier, status, tx_hash, explorer_url, error, timestamp),
        )
        conn.commit()
    return {
        "job_id": job_id,
        "task_id": task_id,
        "task_type": task_type,
        "endpoint": endpoint,
        "amount_usdc": amount_usdc,
        "tier": tier,
        "status": status,
        "tx_hash": tx_hash,
        "explorer_url": explorer_url,
        "error": error,
        "timestamp": timestamp,
    }


def get_ledger_for_job(job_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_total_spent_for_job(job_id: str) -> float:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) AS total FROM payments "
            "WHERE job_id = ? AND status IN ('paid', 'completed')",
            (job_id,),
        ).fetchone()
        return float(row["total"])


def get_latest_paid_entries_by_task(job_id: str) -> dict[str, dict]:
    """Latest 'paid'/'completed' ledger row per task_id for a job. When a task is
    retried against a fallback tier after a failure (see orchestrate.py), it can have
    several rows (failed, then paid) - this returns the one that actually succeeded,
    which is what the cost-per-claim breakdown should be attributed to."""
    latest: dict[str, dict] = {}
    for entry in get_ledger_for_job(job_id):
        if entry["status"] in ("paid", "completed"):
            latest[entry["task_id"]] = entry  # rows are id ASC, so later overwrites earlier
    return latest


def get_provider_stats(task_type: str) -> dict[str, dict]:
    """Success rate per provider (the 'tier' column) for a task type, across ALL past
    jobs - used by the reputation-weighted reverse auction (see orchestrate.py
    `_run_auction`) so a provider that's been failing gets weighted down even though
    its /quote price alone looks cheapest. 'paying' rows (a payment attempt still in
    flight) are ignored - only resolved paid/completed/failed outcomes count."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tier AS provider, status, COUNT(*) AS n FROM payments "
            "WHERE task_type = ? AND tier IS NOT NULL AND status IN ('paid', 'completed', 'failed') "
            "GROUP BY tier, status",
            (task_type,),
        ).fetchall()

    stats: dict[str, dict] = {}
    for r in rows:
        d = stats.setdefault(r["provider"], {"paid": 0, "failed": 0})
        if r["status"] in ("paid", "completed"):
            d["paid"] += r["n"]
        else:
            d["failed"] += r["n"]

    for d in stats.values():
        total = d["paid"] + d["failed"]
        d["success_rate"] = d["paid"] / total if total else 1.0
    return stats


def get_all_ledger() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM payments ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]
