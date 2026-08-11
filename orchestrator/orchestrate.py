import asyncio
import re

import httpx

from shared import api_keys, ledger
from shared.config import SERVICE_URLS, TASK_SERVICE_MAP, SERVICE_LABELS, AUCTION_PROVIDERS
from shared.x402_client import pay_and_fetch_json, price_to_number

from shared.provenance import compute_report_hash, submit_provenance_tx
from shared.rationale import explain, summarize_error
from shared.self_critique import critique_citations, summarize_trust

from . import job_store
from .planner import plan_query
from .citations import compile_citations


async def _narrate(job_id: str, event: str, ctx: dict, task_id: str | None = None) -> None:
    text = await explain(event, ctx)
    job_store.append_narration(job_id, event, text, task_id=task_id)


def _topo_layers(tasks: list[dict]) -> list[list[dict]]:
    by_id = {t["task_id"]: t for t in tasks}
    remaining = {t["task_id"]: set(t["depends_on"]) for t in tasks}
    done: set[str] = set()
    layers: list[list[dict]] = []

    while remaining:
        ready = [tid for tid, deps in remaining.items() if deps.issubset(done)]
        if not ready:
            raise RuntimeError("Cycle detected in planner task graph")
        layers.append([by_id[tid] for tid in ready])
        for tid in ready:
            del remaining[tid]
            done.add(tid)

    return layers


def _flatten_output(task_type: str, output: dict | None) -> str:
    if not output:
        return ""
    if task_type == "web_search":
        return "\n".join(f"{r['title']}: {r['snippet']} ({r['url']})" for r in output.get("results", []))
    if task_type == "data_enrichment":
        parts = []
        if output.get("entities"):
            parts.append(f"Entities: {', '.join(output['entities'])}")
        if output.get("dates"):
            parts.append(f"Dates: {', '.join(output['dates'])}")
        if output.get("stats"):
            parts.append(f"Stats: {', '.join(output['stats'])}")
        return "\n".join(parts)
    if task_type == "fact_check":
        return f"Claim: {output.get('claim')}\nVerdict: {output.get('verdict')} (confidence {output.get('confidence')})\n{output.get('explanation', '')}"
    if task_type == "summarize":
        return output.get("summary", "")
    return str(output)


def _first_url(output: dict | None) -> str | None:
    if not output:
        return None
    results = output.get("results")
    if isinstance(results, list) and results and results[0].get("url"):
        return results[0]["url"]
    if output.get("evidence_url"):
        return output["evidence_url"]
    return None


def _build_request_body(task: dict, graph: dict, task_outputs_by_id: dict, original_query: str) -> dict:
    by_id = {t["task_id"]: t for t in graph["tasks"]}

    def deps_text() -> str:
        return "\n\n".join(
            _flatten_output(by_id[dep]["type"], task_outputs_by_id.get(dep)) for dep in task["depends_on"]
        )

    task_type = task["type"]
    if task_type == "web_search":
        return {"query": task.get("inputs", {}).get("query") or original_query}
    if task_type == "data_enrichment":
        return {"text": deps_text() or original_query}
    if task_type == "fact_check":
        return {"claim": task.get("inputs", {}).get("claim") or original_query, "context": deps_text()}
    if task_type == "summarize":
        return {"text": deps_text() or original_query, "max_words": 150}
    if task_type == "compile_report":
        sections = [
            {
                "task_id": t["task_id"],
                "task_type": t["type"],
                "source_service": SERVICE_LABELS.get(t["type"], t["type"]),
                "content": _flatten_output(t["type"], task_outputs_by_id.get(t["task_id"])),
                "url": _first_url(task_outputs_by_id.get(t["task_id"])),
            }
            for t in graph["tasks"]
            if t["task_id"] != task["task_id"] and t["type"] != "compile_report"
        ]
        return {"query": original_query, "sections": sections}
    return {}


async def _run_auction(task_type: str) -> tuple[list[dict], list[dict]]:
    """Queries GET /quote on every provider configured for this task type (in
    parallel, tolerating individual failures) and returns (candidates, quotes_considered):
      - candidates: providers that quoted successfully, sorted by a reputation-weighted
        score (price adjusted by historical success rate, not raw price alone), each
        as {"name", "price", "url", "success_rate", "score"} ready to pay.
      - quotes_considered: every attempt (including failures), for the trace.

    Reputation weighting: a provider with no history defaults to a 1.0 success rate
    (so a fresh ledger picks purely cheapest, same as before this existed), but one
    that's been failing gets its effective cost inflated (price / success_rate) so a
    slightly pricier, more reliable provider can win - a real tradeoff, not just a
    price-sort.
    """
    provider_keys = AUCTION_PROVIDERS[task_type]
    path = TASK_SERVICE_MAP[task_type]["tiers"]["standard"]["path"]
    stats = ledger.get_provider_stats(task_type)

    async def quote_one(name: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{SERVICE_URLS[name]}/quote")
            resp.raise_for_status()
            price = float(resp.json()["price_usdc"])
            return {"provider": name, "price_usdc": price, "error": None}
        except Exception as err:
            return {"provider": name, "price_usdc": None, "error": str(err)}

    quotes = await asyncio.gather(*(quote_one(name) for name in provider_keys))
    for q in quotes:
        q["success_rate"] = round(stats.get(q["provider"], {}).get("success_rate", 1.0), 3)

    def score(price: float, success_rate: float) -> float:
        return price / max(success_rate, 0.05)  # floor so one bad run doesn't make it unpickable forever

    candidates = sorted(
        (
            {
                "name": q["provider"],
                "price": q["price_usdc"],
                "url": f"{SERVICE_URLS[q['provider']]}{path}",
                "success_rate": q["success_rate"],
                "score": round(score(q["price_usdc"], q["success_rate"]), 6),
            }
            for q in quotes
            if q["price_usdc"] is not None
        ),
        key=lambda c: c["score"],
    )
    return candidates, quotes


def _pick_first_affordable(candidates: list[dict], committed_spend: float, budget_cap: float) -> dict | None:
    for c in candidates:
        if committed_spend + c["price"] <= budget_cap:
            return c
    return None


"""Faults worth trying the *same* provider again for, rather than failing over.

The public x402 facilitator is the shared dependency every paid call goes through, and it
is intermittently slow. Two of its failure modes look like provider failures but are not:

  * HTTP 5xx - the service's own x402 middleware raised while calling facilitator/verify
    (httpcore.ConnectTimeout, since the SDK builds its client with no timeout override).
  * HTTP 402 *after* payment was attached - verification came back invalid, so the server
    re-issued the payment requirement. No money moved; a fresh authorization usually
    goes through.

Neither says anything about the provider, and for task types with a single provider
(fact_check, data_enrichment) failing over is not even an option - the task would simply
be dropped over a network blip.

A refused connection is excluded deliberately: that means the process is down, which is a
real provider failure and exactly the case where failing over immediately is correct
(and what the kill-a-service demo depends on).
"""
_RETRYABLE_STATUS = re.compile(r"failed with (5\d\d|402)\b")


def _is_transient(err: Exception) -> bool:
    if isinstance(err, (httpx.TimeoutException, httpx.RemoteProtocolError)):
        return True
    if isinstance(err, httpx.ConnectError):
        return False  # connection refused - the provider really is gone
    return bool(_RETRYABLE_STATUS.search(str(err)))


async def _build_candidates(task: dict) -> tuple[list[dict], list[dict] | None]:
    """Returns (candidates, quotes_considered) for a task, in priority order: for an
    auction task type that's cheapest-first (a live reverse auction); otherwise it's
    preferred-tier-first (premium before cheap), same as before the auction existed.
    quotes_considered is only non-None for auction task types.
    """
    task_type = task["type"]
    svc_info = TASK_SERVICE_MAP[task_type]

    if task_type in AUCTION_PROVIDERS:
        candidates, quotes_considered = await _run_auction(task_type)
        if not candidates:
            # every provider failed to quote - fall back to the single default provider/price
            # rather than stalling the whole task type.
            default_tier = svc_info["tiers"]["standard"]
            candidates = [
                {
                    "name": "standard",
                    "price": price_to_number(default_tier["price"]),
                    "url": f"{SERVICE_URLS[svc_info['service']]}{default_tier['path']}",
                }
            ]
        return candidates, quotes_considered

    tiers = svc_info["tiers"]
    primary_name = "premium" if "premium" in tiers else "standard"
    ordered_names = [primary_name] + sorted(
        (n for n in tiers if n != primary_name), key=lambda n: price_to_number(tiers[n]["price"])
    )
    candidates = [
        {
            "name": n,
            "price": price_to_number(tiers[n]["price"]),
            "url": f"{SERVICE_URLS[tiers[n].get('service', svc_info['service'])]}{tiers[n].get('path', svc_info['path'])}",
        }
        for n in ordered_names
    ]
    return candidates, None


async def run_job(job_id: str) -> None:
    job = job_store.get_job(job_id)
    if not job:
        return

    try:
        job_store.update_job(job_id, status="running")
        graph = await plan_query(job["query"])
        job_store.update_job(job_id, graph=graph)
        await _narrate(
            job_id, "planned",
            {"n_tasks": len(graph["tasks"]), "task_types": ", ".join(sorted({t["type"] for t in graph["tasks"]}))},
        )

        layers = _topo_layers(graph["tasks"])
        task_outputs_by_id: dict[str, dict] = {}
        committed_spend = 0.0
        budget_exceeded = False

        for layer in layers:
            if budget_exceeded:
                break

            to_run = []
            for task in layer:
                candidates, quotes_considered = await _build_candidates(task)

                chosen = _pick_first_affordable(candidates, committed_spend, job["budget_cap_usdc"])
                if chosen is None:
                    job_store.upsert_step(
                        job_id,
                        task["task_id"],
                        task_type=task["type"],
                        status="skipped_budget_exceeded",
                        amount_usdc=candidates[0]["price"],
                    )
                    await _narrate(
                        job_id, "skipped_budget_exceeded",
                        {"task_type": task["type"], "price": candidates[0]["price"]},
                        task_id=task["task_id"],
                    )
                    budget_exceeded = True
                    continue

                if chosen is not candidates[0] and quotes_considered is None:
                    # Only meaningful for the tier-preference path (premium -> cheap); an
                    # auction's candidates[0] is already the cheapest, so this never fires there.
                    job_store.upsert_step(
                        job_id,
                        task["task_id"],
                        task_type=task["type"],
                        status="tier_downgraded",
                        note="Switched to the cheaper provider tier to stay within the budget cap",
                    )
                    await _narrate(
                        job_id, "tier_downgraded",
                        {"task_type": task["type"], "chosen": chosen["name"], "budget_cap": job["budget_cap_usdc"]},
                        task_id=task["task_id"],
                    )
                elif quotes_considered is not None:
                    # A live reverse auction ran for this task - narrate the winner (reputation-
                    # weighted, see _run_auction: not always the raw-cheapest quote).
                    await _narrate(
                        job_id, "auction_won",
                        {
                            "n_quoted": len(quotes_considered),
                            "chosen": chosen["name"],
                            "price": chosen["price"],
                            "success_rate": chosen.get("success_rate", 1.0),
                        },
                        task_id=task["task_id"],
                    )

                committed_spend += chosen["price"]
                remaining = [c for c in candidates if c is not chosen]
                to_run.append((task, chosen, remaining, quotes_considered))

            async def run_one(task, chosen, remaining, quotes_considered):
                # `remaining` candidates are retried in order if the current one fails
                # (connection refused / timeout / non-2xx-non-402). For most task types
                # this is empty (a single fixed provider). compile_report has a real
                # alternate (premium <-> cheap, each its own process, so killing one
                # genuinely leaves the other reachable); web_search has a live reverse
                # auction across 3 provider processes.
                candidate = chosen
                fallback_candidates = list(remaining)
                original_name = candidate["name"]
                attempt = 0
                last_error = None
                # Reset whenever we move to a different provider, so each one gets its
                # own allowance rather than sharing a pool with the one that just failed.
                transient_retries_left = 1

                while True:
                    url = candidate["url"]
                    price = candidate["price"]
                    name = candidate["name"]
                    body = _build_request_body(task, graph, task_outputs_by_id, job["query"])

                    # upsert_step merges onto the existing step dict, so a fresh attempt must
                    # explicitly clear fields left over from a previous failed attempt (error,
                    # tx_hash, ...) - otherwise a retry would briefly display stale error text
                    # from the provider it's falling back FROM.
                    step_patch = dict(
                        task_type=task["type"],
                        status="paying",
                        endpoint=url,
                        amount_usdc=price,
                        tier=name,
                        error=None,
                        tx_hash=None,
                        explorer_url=None,
                        note=None,
                        provider_switched=None,
                        switched_from=None,
                        switched_to=None,
                    )
                    if quotes_considered is not None:
                        step_patch["quotes_considered"] = quotes_considered
                        step_patch["chosen_provider"] = name
                    job_store.upsert_step(job_id, task["task_id"], **step_patch)

                    try:
                        result = await pay_and_fetch_json(
                            job_id=job_id,
                            task_id=task["task_id"],
                            task_type=task["type"],
                            url=url,
                            price_usdc=f"${price}",
                            tier=name,
                            body=body,
                        )
                        task_outputs_by_id[task["task_id"]] = result["data"]
                        completion_patch = {
                            "status": "completed",
                            "tx_hash": result["tx_hash"],
                            "explorer_url": result["explorer_url"],
                        }
                        if attempt > 0:
                            completion_patch.update(
                                provider_switched=True,
                                switched_from=original_name,
                                switched_to=name,
                                note=f"Recovered by switching from '{original_name}' to '{name}' after a failure",
                            )
                            await _narrate(
                                job_id, "provider_switched",
                                {
                                    "from_name": original_name,
                                    "to_name": name,
                                    # Condensed: the raw x402 refusal embeds amounts in
                                    # atomic USDC, which the narrator reported as dollars.
                                    "error": summarize_error(last_error),
                                },
                                task_id=task["task_id"],
                            )
                        job_store.upsert_step(job_id, task["task_id"], **completion_patch)
                        return
                    except Exception as err:
                        last_error = str(err)
                        job_store.upsert_step(job_id, task["task_id"], status="failed", error=last_error)

                        # A shared-infrastructure hiccup, not this provider's fault - give
                        # the same one another go before writing it off. Costs nothing: the
                        # payment never settled, which is why the call failed at all.
                        if transient_retries_left > 0 and _is_transient(err):
                            transient_retries_left -= 1
                            print(f"[orchestrator] transient failure on {name}, retrying once: {last_error[:120]}")
                            await asyncio.sleep(1.5)
                            continue

                        if not fallback_candidates:
                            # No alternate provider for this task type - clean stop. The job
                            # itself doesn't crash: downstream tasks just see less context for
                            # this one, and the job overall only ends up `failed` if
                            # compile_report never manages to produce output.
                            await _narrate(
                                job_id, "failed_no_fallback",
                                {"task_type": task["type"]},
                                task_id=task["task_id"],
                            )
                            return

                        spent_so_far = ledger.get_total_spent_for_job(job_id)
                        next_candidate = None
                        while fallback_candidates:
                            c = fallback_candidates.pop(0)
                            if spent_so_far + c["price"] <= job["budget_cap_usdc"]:
                                next_candidate = c
                                break
                        if next_candidate is None:
                            job_store.upsert_step(
                                job_id,
                                task["task_id"],
                                status="failed",
                                note="An alternate provider exists but was skipped: it would exceed the budget cap",
                            )
                            return

                        candidate = next_candidate
                        attempt += 1
                        transient_retries_left = 1
                        continue

            await asyncio.gather(*(run_one(*args) for args in to_run))

            if budget_exceeded:
                break

        compile_task = next((t for t in graph["tasks"] if t["type"] == "compile_report"), None)
        report_output = task_outputs_by_id.get(compile_task["task_id"]) if compile_task else None

        if report_output:
            citations = compile_citations(
                job_id=job_id,
                graph=graph,
                task_outputs_by_id=task_outputs_by_id,
                report_citations=report_output.get("citations"),
            )

            # Self-critique: an independent LLM pass red-teams the report's own citations
            # before it's returned (see shared/self_critique.py). Best-effort - citations
            # come back unreviewed (not wrongly marked "strong") if this fails.
            citations = await critique_citations(job["query"], citations)
            trust_score, weak_claims_count = summarize_trust(citations)
            if trust_score is not None:
                await _narrate(job_id, "self_critique", {"weak": weak_claims_count, "total": len(citations)})

            report = {
                "report_markdown": report_output["report_markdown"],
                "citations": citations,
                "trust_score": trust_score,
                "weak_claims_count": weak_claims_count,
            }

            # On-chain provenance receipt (best-effort - a judge/verifier can refetch this
            # report, recompute the hash the same way, and check it against the tx below).
            # Needs real testnet ETH for gas (unlike the gasless x402 USDC payments above),
            # so this is skipped gracefully - not a job failure - if that's unavailable.
            try:
                payment_ledger = job_store.serialize_trace(job_id)["steps"]
                report_hash = compute_report_hash(
                    {
                        "report_markdown": report["report_markdown"],
                        "citations": citations,
                        "payment_ledger": payment_ledger,
                    }
                )
                provenance = submit_provenance_tx(report_hash)
                report["report_hash"] = report_hash
                report["provenance_tx_hash"] = provenance["tx_hash"]
                report["provenance_explorer_url"] = provenance["explorer_url"]
            except Exception as err:
                print(f"[orchestrator] provenance receipt skipped for job {job_id}: {err}")
                report["report_hash"] = None
                report["provenance_tx_hash"] = None
                report["provenance_explorer_url"] = None

            job_store.update_job(job_id, status="completed", report=report)
            await _narrate(
                job_id, "completed",
                {"n_citations": len(citations), "total_spent": ledger.get_total_spent_for_job(job_id)},
            )
        elif budget_exceeded:
            job_store.update_job(job_id, status="budget_exceeded")
        else:
            job_store.update_job(job_id, status="failed", error="compile_report task did not produce output")

    except Exception as err:
        job_store.update_job(job_id, status="failed", error=str(err))

    finally:
        # The job can no longer spend, however it ended - release the budget reserved for
        # it at launch so the caller's remaining quota reflects what was actually paid.
        # In a finally block on purpose: a crash that skipped this would silently burn
        # that caller's daily allowance until midnight UTC.
        api_keys.settle_job(job_id)
