#!/usr/bin/env python3
"""End-to-end smoke test: submits one research query, polls the trace until the job
finishes, then prints the final report and total spend. Run with:
    python scripts/test_e2e.py
    python scripts/test_e2e.py "your query here" 0.05
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
QUERY = sys.argv[1] if len(sys.argv) > 1 else "What is the current state of quantum computing error correction?"
BUDGET_CAP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
POLL_S = 1.5
TIMEOUT_S = 180


def fmt(step: dict) -> str:
    bits = [f"  [{step['task_id']}] {step.get('task_type')} -> {step['status']}"]
    if step.get("tier"):
        bits.append(f"tier={step['tier']}")
    if step.get("amount_usdc") is not None:
        bits.append(f"${step['amount_usdc']}")
    if step.get("tx_hash"):
        bits.append(f"tx={step['tx_hash']}")
    if step.get("note"):
        bits.append(f'note="{step["note"]}"')
    if step.get("error"):
        bits.append(f'error="{step["error"]}"')
    return " ".join(bits)


async def main():
    print(f'Submitting research job:\n  query: "{QUERY}"\n  budget_cap_usdc: {BUDGET_CAP}\n')

    async with httpx.AsyncClient(timeout=30) as client:
        submit_res = await client.post(
            f"{ORCH_URL}/api/research", json={"query": QUERY, "budget_cap_usdc": BUDGET_CAP}
        )
        if submit_res.status_code != 202:
            print("Failed to submit job:", submit_res.status_code, submit_res.text)
            sys.exit(1)
        job_id = submit_res.json()["job_id"]
        print(f"Job accepted: {job_id}\n")

        seen = set()
        start = time.time()
        final_trace = None

        while time.time() - start < TIMEOUT_S:
            trace_res = await client.get(f"{ORCH_URL}/api/research/{job_id}/trace")
            trace = trace_res.json()

            for step in trace["steps"]:
                sig = f"{step['task_id']}:{step['status']}:{step.get('tx_hash') or ''}"
                if sig not in seen:
                    seen.add(sig)
                    print(fmt(step))
                    if step.get("explorer_url"):
                        print(f"      explorer: {step['explorer_url']}")

            if trace["status"] in ("completed", "budget_exceeded", "failed"):
                final_trace = trace
                break
            await asyncio.sleep(POLL_S)

        if not final_trace:
            print(f"\nTimed out after {TIMEOUT_S}s waiting for job to finish.")
            sys.exit(1)

        print(f"\nJob finished with status: {final_trace['status']}")
        print(f"Total spent: ${final_trace['total_spent_usdc']} (cap: ${final_trace['budget_cap_usdc']})\n")

        if final_trace["status"] != "completed":
            print("No report was generated (job did not complete successfully).")
            sys.exit(0 if final_trace["status"] == "budget_exceeded" else 1)

        report_res = await client.get(f"{ORCH_URL}/api/research/{job_id}/report")
        report = report_res.json()

        print("=" * 72)
        print("FINAL REPORT")
        print("=" * 72)
        print(report["report_markdown"])
        print("\n" + "=" * 72)
        print("CITATIONS")
        print("=" * 72)
        for c in report["citations"]:
            print(f"- [{c['task_id']}] ({c['source_service']}) {c['claim']}")
            if c.get("source_url"):
                print(f"  {c['source_url']}")

        print("\n" + "=" * 72)
        print("PAYMENT LEDGER")
        print("=" * 72)
        for p in report["payment_ledger"]:
            print(fmt(p))
            if p.get("explorer_url"):
                print(f"      explorer: {p['explorer_url']}")

        print(f"\nTotal spent: ${report['total_spent_usdc']}")

        with_tx = next((p for p in report["payment_ledger"] if p.get("tx_hash")), None)
        if with_tx:
            print("\nAt least one real on-chain payment settled:")
            print(f"  tx hash: {with_tx['tx_hash']}")
            print(f"  explorer: {with_tx['explorer_url']}")
        else:
            print("\nNo on-chain tx hash was recorded - check that the buyer wallet is funded with testnet USDC.")


if __name__ == "__main__":
    asyncio.run(main())
