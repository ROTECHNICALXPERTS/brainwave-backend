"""API keys and server-side spend limits.

Every research job spends real testnet USDC from one shared buyer wallet, so once the
orchestrator is reachable by anyone (an MCP server, a public URL) the wallet is only as
safe as the limits enforced here. The client's requested `budget_cap_usdc` is a ceiling
for one job and is *not* trustworthy on its own - a caller can ask for anything, and can
ask repeatedly. These limits are the part the server decides.

Three independent ceilings, checked before a job is ever created:

  * per-job    - the largest budget_cap a single job may request
  * per-caller - rolling UTC-day spend for one API key (or for anonymous callers)
  * global     - rolling UTC-day spend across every caller, the last line of defence
                 on the wallet no matter how many keys exist

Spend is measured as *exposure*, not just settled payments: a job that is still running
has not spent its budget yet but may be about to, so its full budget_cap counts against
the caller until the job finishes. Without that, a caller could launch fifty jobs in the
same second and every one of them would see a spend of zero.

Keys live in the same SQLite file as the payment ledger, because exposure is a join
against it. Raw keys are never stored - only their SHA-256 - so a leaked database still
cannot be used to spend.
"""
import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

KEY_PREFIX = "ar_sk_"
_PREFIX_DISPLAY_LEN = len(KEY_PREFIX) + 8


def _num(v, fallback: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _int(v, fallback: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


def _bool(v, fallback: bool = False) -> bool:
    if v is None:
        return fallback
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# Anonymous (no key) callers keep working by default so the local demo and the React
# frontend need no changes - but they are still capped. Set REQUIRE_API_KEY=true before
# exposing the orchestrator publicly and anonymous launches stop entirely.
def require_api_key() -> bool:
    return _bool(os.getenv("REQUIRE_API_KEY"), False)


def anon_limits() -> "Limits":
    return Limits(
        daily_cap_usdc=_num(os.getenv("ANON_DAILY_CAP_USDC"), 1.0),
        max_job_budget_usdc=_num(os.getenv("ANON_MAX_JOB_BUDGET_USDC"), 0.30),
        # The hourly limit is burst control, not the money guard - the daily cap is, and
        # at ~$0.02 a job it binds first anyway (~50 jobs). Kept well above that so a
        # demo or a test loop trips the meaningful limit rather than an arbitrary one.
        max_jobs_per_hour=_int(os.getenv("ANON_MAX_JOBS_PER_HOUR"), 60),
    )


def global_daily_cap_usdc() -> float:
    return _num(os.getenv("GLOBAL_DAILY_CAP_USDC"), 5.0)


def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                label TEXT,
                daily_cap_usdc REAL NOT NULL,
                max_job_budget_usdc REAL NOT NULL,
                max_jobs_per_hour INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        # Which caller launched which job, and how much they reserved for it. Separate
        # from the in-memory job store on purpose: this has to survive a restart, since
        # it is what the spend limits are computed from.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_owners (
                job_id TEXT PRIMARY KEY,
                key_id INTEGER,
                budget_cap_usdc REAL NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_owners_key ON job_owners(key_id, created_at)")
        conn.commit()


_init_db()


@dataclass(frozen=True)
class Limits:
    daily_cap_usdc: float
    max_job_budget_usdc: float
    max_jobs_per_hour: int


@dataclass(frozen=True)
class Caller:
    """Who is asking. `key_id` None means anonymous - a real caller identity in local
    dev, not an error, but one that shares a single pooled quota with every other
    anonymous request."""

    key_id: int | None
    label: str
    limits: Limits

    @property
    def is_anonymous(self) -> bool:
        return self.key_id is None


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _utc_day_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _hour_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------- key management

def create_key(
    label: str,
    *,
    daily_cap_usdc: float = 0.50,
    max_job_budget_usdc: float = 0.10,
    max_jobs_per_hour: int = 10,
) -> tuple[str, dict]:
    """Mint a key. The raw value is returned exactly once and never stored; only its
    hash goes to the database, so it cannot be recovered or displayed later."""
    raw = KEY_PREFIX + secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO api_keys
                (key_hash, key_prefix, label, daily_cap_usdc, max_job_budget_usdc, max_jobs_per_hour, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (_hash(raw), raw[:_PREFIX_DISPLAY_LEN], label, daily_cap_usdc, max_job_budget_usdc, max_jobs_per_hour, now),
        )
        conn.commit()
        key_id = cur.lastrowid
    return raw, {
        "id": key_id,
        "key_prefix": raw[:_PREFIX_DISPLAY_LEN],
        "label": label,
        "daily_cap_usdc": daily_cap_usdc,
        "max_job_budget_usdc": max_job_budget_usdc,
        "max_jobs_per_hour": max_jobs_per_hour,
        "created_at": now,
    }


def lookup_key(raw_key: str) -> dict | None:
    """Resolve a raw key to its active record, or None if unknown or revoked."""
    if not raw_key:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1", (_hash(raw_key),)
        ).fetchone()
        return dict(row) if row else None


def list_keys() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def revoke_key(prefix: str) -> bool:
    """Deactivate by displayed prefix - the only identifier still visible after minting."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET active = 0, revoked_at = ? WHERE key_prefix = ? AND active = 1",
            (now, prefix),
        )
        conn.commit()
        return cur.rowcount > 0


def caller_for(record: dict | None) -> Caller:
    if record is None:
        return Caller(key_id=None, label="anonymous", limits=anon_limits())
    return Caller(
        key_id=record["id"],
        label=record.get("label") or record["key_prefix"],
        limits=Limits(
            daily_cap_usdc=record["daily_cap_usdc"],
            max_job_budget_usdc=record["max_job_budget_usdc"],
            max_jobs_per_hour=record["max_jobs_per_hour"],
        ),
    )


# ---------------------------------------------------------------- job attribution

def record_job(job_id: str, key_id: int | None, budget_cap_usdc: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_owners (job_id, key_id, budget_cap_usdc, settled, created_at) VALUES (?, ?, ?, 0, ?)",
            (job_id, key_id, budget_cap_usdc, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def settle_job(job_id: str) -> None:
    """Release a job's reservation once it can no longer spend. From here on the job
    counts toward the caller's quota at whatever it actually paid, not what it reserved."""
    with _connect() as conn:
        conn.execute("UPDATE job_owners SET settled = 1 WHERE job_id = ?", (job_id,))
        conn.commit()


def owner_of(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM job_owners WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- spend accounting

def _settled_spend_today(conn, key_id: int | None, scope_all: bool = False) -> float:
    """USDC actually paid out today for a caller's finished jobs. 'paying' rows are
    excluded - they are superseded by a 'paid' row for the same amount, so counting
    both would double every payment."""
    if scope_all:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) AS total FROM payments "
            "WHERE status IN ('paid', 'completed') AND timestamp >= ?",
            (_utc_day_start(),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(p.amount_usdc), 0) AS total FROM payments p "
            "JOIN job_owners jo ON p.job_id = jo.job_id "
            "WHERE jo.key_id IS ? AND p.status IN ('paid', 'completed') AND p.timestamp >= ?",
            (key_id, _utc_day_start()),
        ).fetchone()
    return float(row["total"])


def _reserved_today(conn, key_id: int | None, scope_all: bool = False) -> float:
    """Budget still committed to jobs that are running and could yet spend it."""
    if scope_all:
        row = conn.execute(
            "SELECT COALESCE(SUM(budget_cap_usdc), 0) AS total FROM job_owners "
            "WHERE settled = 0 AND created_at >= ?",
            (_utc_day_start(),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(budget_cap_usdc), 0) AS total FROM job_owners "
            "WHERE key_id IS ? AND settled = 0 AND created_at >= ?",
            (key_id, _utc_day_start()),
        ).fetchone()
    return float(row["total"])


def exposure_today(key_id: int | None) -> float:
    """What this caller has spent today plus what their in-flight jobs could still spend."""
    with _connect() as conn:
        return _settled_spend_today(conn, key_id) + _reserved_today(conn, key_id)


def global_exposure_today() -> float:
    with _connect() as conn:
        return _settled_spend_today(conn, None, scope_all=True) + _reserved_today(conn, None, scope_all=True)


def jobs_last_hour(key_id: int | None) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_owners WHERE key_id IS ? AND created_at >= ?",
            (key_id, _hour_ago()),
        ).fetchone()
        return int(row["n"])


# ---------------------------------------------------------------- the actual gate

def authorize_launch(caller: Caller, requested_budget_usdc: float) -> str | None:
    """Decide whether this caller may start a job at this budget. Returns None to allow,
    or a human-readable reason to refuse - the caller-facing message, so it says what the
    limit was and what would fit."""
    limits = caller.limits

    if requested_budget_usdc > limits.max_job_budget_usdc:
        return (
            f"budget_cap_usdc {requested_budget_usdc:.4f} exceeds the per-job maximum of "
            f"{limits.max_job_budget_usdc:.4f} USDC for this caller"
        )

    if jobs_last_hour(caller.key_id) >= limits.max_jobs_per_hour:
        return f"rate limit reached: {limits.max_jobs_per_hour} jobs per hour for this caller"

    exposure = exposure_today(caller.key_id)
    if exposure + requested_budget_usdc > limits.daily_cap_usdc:
        remaining = max(0.0, limits.daily_cap_usdc - exposure)
        return (
            f"daily spend cap reached: {exposure:.4f} of {limits.daily_cap_usdc:.4f} USDC "
            f"already committed today, {remaining:.4f} USDC remaining"
        )

    global_cap = global_daily_cap_usdc()
    global_exposure = global_exposure_today()
    if global_exposure + requested_budget_usdc > global_cap:
        return (
            f"service-wide daily spend cap reached ({global_exposure:.4f} of {global_cap:.4f} USDC "
            f"committed today); try again tomorrow"
        )

    return None


def quota_snapshot(caller: Caller) -> dict:
    """What's left for this caller - surfaced to MCP clients so an agent can see its own
    remaining budget instead of discovering it by getting refused."""
    exposure = exposure_today(caller.key_id)
    return {
        "caller": caller.label,
        "anonymous": caller.is_anonymous,
        "spent_or_committed_today_usdc": round(exposure, 6),
        "daily_cap_usdc": caller.limits.daily_cap_usdc,
        "remaining_today_usdc": round(max(0.0, caller.limits.daily_cap_usdc - exposure), 6),
        "max_job_budget_usdc": caller.limits.max_job_budget_usdc,
        "jobs_last_hour": jobs_last_hour(caller.key_id),
        "max_jobs_per_hour": caller.limits.max_jobs_per_hour,
    }
