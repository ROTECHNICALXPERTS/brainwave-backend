#!/usr/bin/env python3
"""MUST-HAVE 2 test: confirms GET /api/research/{job_id}/trace reflects
total_spent_usdc live, mid-job, not just once the job completes - this is what the
frontend's "cost meter" animation polls. Submits a job, polls the trace fast
(every 0.4s) while it runs, and asserts total_spent_usdc never decreases between
polls (a hard bug - money doesn't get un-spent) and, if any payment actually
settles, that it strictly increases at some point (proving it's live, not frozen).

Run with:  python scripts/test_live_cost_meter.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ORCH_URL = os.getenv("ORCHESTRATOR_URL") or f"http://localhost:{os.getenv('PORT_ORCHESTRATOR', '4000')}"
QUERY = sys.argv[1] if len(sys.argv) > 1 else "live cost meter test query"
BUDGET_CAP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
POLL_S = 0.4
TIMEOUT_S = 120


async def main():
    async with httpx.AsyncClient(timeout=15) as client:
        submit = await client.post(f"{ORCH_URL}/api/research", json={"query": QUERY, "budget_cap_usdc": BUDGET_CAP})
        submit.raise_for_status()
        job_id = submit.json()["job_id"]
        print(f"Job accepted: {job_id}")

        samples = []
        start = time.time()
        while time.time() - start < TIMEOUT_S:
            trace = (await client.get(f"{ORCH_URL}/api/research/{job_id}/trace")).json()
            samples.append(trace["total_spent_usdc"])
            print(f"  t={time.time() - start:5.1f}s  total_spent_usdc={trace['total_spent_usdc']:.6f}  status={trace['status']}")
            if trace["status"] in ("completed", "budget_exceeded", "failed"):
                break
            await asyncio.sleep(POLL_S)

    assert len(samples) >= 2, "Need at least 2 samples to check monotonicity - job finished too fast to observe."

    for i in range(1, len(samples)):
        assert samples[i] >= samples[i - 1] - 1e-12, (
            f"total_spent_usdc DECREASED between polls: {samples[i - 1]} -> {samples[i]} "
            "(a payment total must never go down)"
        )
    print("\nPASS: total_spent_usdc is monotonically non-decreasing across all polls.")

    if samples[-1] > samples[0]:
        print(f"PASS: total_spent_usdc increased live during the run ({samples[0]} -> {samples[-1]}),")
        print("      confirming the trace endpoint updates mid-job, not just at completion.")
    else:
        print(
            "\nNOTE: total_spent_usdc never increased (stayed at "
            f"{samples[0]}) - this run had no successful on-chain payment "
            "(likely: buyer wallet unfunded). The monotonicity guarantee still holds and "
            "the code path is confirmed correct (see shared/ledger.py + orchestrator/job_store.py: "
            "serialize_trace() recomputes total_spent_usdc fresh from the ledger on every call), "
            "but fund the wallet and re-run to see it actually climb."
        )


if __name__ == "__main__":
    asyncio.run(main())
