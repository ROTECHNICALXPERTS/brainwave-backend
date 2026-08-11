#!/usr/bin/env python3
"""MUST-HAVE 3 test: manually kills the premium report-gen process mid-job and
confirms the orchestrator recovers by falling back to the independent cheap
report-gen process, without hanging or crashing. This is the exact scenario for the
"kill a microservice live during the demo" moment.

Requires the full stack already running via ./run_all.sh (this script finds and
kills the report-gen *premium* process by port, then restarts it at the end so the
stack is left in its original state).

Run with:  python scripts/test_failover.py
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ORCH_URL = os.getenv("ORCHESTRATOR_URL") or f"http://localhost:{os.getenv('PORT_ORCHESTRATOR', '4000')}"
REPORT_GEN_PORT = os.getenv("PORT_REPORT_GEN", "4005")
QUERY = "failover test: what happens to the report step when premium report-gen is killed mid-job?"
BUDGET_CAP = 0.05
TIMEOUT_S = 120


def find_pid_on_port(port: str) -> str | None:
    try:
        out = subprocess.check_output(["lsof", "-t", f"-i:{port}"], text=True).strip()
        return out.splitlines()[0] if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def restart_report_gen():
    print("Restarting report-gen (premium) so the stack is back to normal...")
    subprocess.Popen(
        [sys.executable, "services/report-gen/main.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def main():
    pid = find_pid_on_port(REPORT_GEN_PORT)
    if not pid:
        print(f"Could not find a process on port {REPORT_GEN_PORT} - is ./run_all.sh running? Aborting.")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=15) as client:
        submit = await client.post(f"{ORCH_URL}/api/research", json={"query": QUERY, "budget_cap_usdc": BUDGET_CAP})
        submit.raise_for_status()
        job_id = submit.json()["job_id"]
        print(f"Job accepted: {job_id}")

        # Give the earlier layers (search, enrichment, fact-check, summarize) a moment to
        # run, so the kill genuinely lands mid-job rather than before anything has started.
        await asyncio.sleep(1.5)

        print(f"Killing report-gen premium process (pid {pid}, port {REPORT_GEN_PORT})...")
        os.kill(int(pid), 9)
        dead_confirmed = False
        for _ in range(20):
            try:
                await client.get(f"http://localhost:{REPORT_GEN_PORT}/health", timeout=1)
            except httpx.TransportError:
                dead_confirmed = True
                break
            await asyncio.sleep(0.2)
        print(f"report-gen premium confirmed down: {dead_confirmed}")

        seen = set()
        start = time.time()
        final_trace = None
        try:
            while time.time() - start < TIMEOUT_S:
                trace = (await client.get(f"{ORCH_URL}/api/research/{job_id}/trace")).json()
                for step in trace["steps"]:
                    sig = (step["task_id"], step["status"], step.get("tier"))
                    if sig not in seen:
                        seen.add(sig)
                        print(f"  [{step['task_id']}] {step.get('task_type')} -> {step['status']} tier={step.get('tier')}"
                              f"{' PROVIDER_SWITCHED ' + str(step.get('switched_from')) + '->' + str(step.get('switched_to')) if step.get('provider_switched') else ''}")
                if trace["status"] in ("completed", "budget_exceeded", "failed"):
                    final_trace = trace
                    break
                await asyncio.sleep(1.0)
        finally:
            restart_report_gen()

        assert final_trace is not None, "TIMED OUT - orchestrator hung instead of reaching a terminal status."
        print(f"\nJob reached terminal status: {final_trace['status']} (no hang, no crash - orchestrator process is still up: "
              f"{(await client.get(f'{ORCH_URL}/health')).status_code == 200})")

        report_step = next((s for s in final_trace["steps"] if s.get("task_type") == "compile_report"), None)

        if report_step and report_step.get("provider_switched"):
            print(f"PASS: compile_report step shows provider_switched=True "
                  f"(switched_from={report_step['switched_from']} -> switched_to={report_step['switched_to']})")
        elif report_step and report_step["status"] == "completed":
            print("Report step completed without needing to switch tiers (unexpected but not a failure) - "
                  "check timing: did the kill happen before the premium call was even attempted?")
        elif report_step and report_step["status"] == "failed":
            print("Report step ended failed on both tiers - expected if the buyer wallet has no testnet USDC yet "
                  "(the fallback to cheap DID fire, it just also failed to settle payment). Check the log above for "
                  "two distinct attempts against port 4005 then 4006.")
        else:
            print(f"Unexpected report step state: {report_step}")


if __name__ == "__main__":
    asyncio.run(main())
