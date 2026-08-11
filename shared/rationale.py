"""Best-effort natural-language narration of orchestrator decisions - the "live AI
reasoning stream" shown in the frontend. Only called at genuinely interesting
decision points (auction results, tier downgrades, budget skips, failover
recoveries) - not on every routine task, so the feed stays signal not noise.

Always degrades to a deterministic templated sentence if no LLM key is set or the
call fails/times out - narration is a demo nicety, never something the pipeline
should wait long on or fail a job over.
"""
import re

from .llm import call_llm, has_llm

_X402_BODY = re.compile(r"\{.*", re.S)


def summarize_error(err: str | None) -> str:
    """Condense a provider error to something safe to hand the narrator.

    A raw x402 refusal carries the whole payment-requirements JSON, in which amounts are
    in *atomic* USDC - `"maxAmountRequired":"6000"` means $0.006. Passed through verbatim,
    the narrator read that number as dollars and told the demo audience the report cost
    "$6000". Amounts belong in the caller's own context fields, already formatted; the
    error only needs to say what went wrong.
    """
    if not err:
        return "unknown error"
    text = _X402_BODY.sub("", str(err)).strip().rstrip(":").strip()
    if "402" in str(err):
        return "payment was not accepted (402)"
    return text[:120] or "request failed"

_FALLBACKS = {
    "planned": "Planned {n_tasks} tasks for this query: {task_types}.",
    "auction_won": "Queried {n_quoted} search providers live; picked '{chosen}' at ${price:.4f} "
    "(reliability {success_rate:.0%}), the best price/reliability tradeoff.",
    "tier_downgraded": "Switched {task_type} to the cheaper '{chosen}' tier to stay within the "
    "${budget_cap:.2f} budget cap.",
    "skipped_budget_exceeded": "Skipped {task_type}: even the cheapest option (${price:.4f}) would "
    "exceed the remaining budget.",
    "provider_switched": "'{from_name}' failed ({error}); retried with '{to_name}' and it went through.",
    "failed_no_fallback": "'{task_type}' failed with no alternate provider available; continuing without it.",
    "self_critique": "Self-review flagged {weak} of {total} claims as weak before finalizing the report.",
    "completed": "Job complete: {n_citations} cited claims for ${total_spent:.4f}.",
}


def _fallback(event: str, ctx: dict) -> str:
    template = _FALLBACKS.get(event, "{event}")
    try:
        return template.format(event=event, **ctx)
    except Exception:
        return event


async def explain(event: str, ctx: dict) -> str:
    if not has_llm("aux"):
        return _fallback(event, ctx)
    try:
        text = await call_llm(
            # "aux", not "cheap": narration is a nicety and must never eat the token
            # budget the paid microservices need. See _GEMINI_TIERS in llm.py.
            tier="aux",
            reasoning_effort="none",
            system=(
                "You narrate an autonomous AI research agent's live decisions for a technical demo "
                "audience, in ONE short punchy sentence (max 22 words). Describe the concrete outcome and "
                "WHY it happened using the specific numbers given (price, percentages, provider/tier "
                "names) - never describe the event mechanically (do not say things like 'processing X "
                "event' or restate the event name/field names verbatim). Present tense, active voice, no "
                "preamble, no quotes, no markdown, no leading dash. "
                "Example good output for an auction win: \"Picked search_c at $0.002 over pricier rivals - "
                "cheapest quote with a solid reliability record.\""
            ),
            user=f"Event: {event}\nDetails: {ctx}",
            max_tokens=60,
            timeout=8,
        )
        line = text.strip().splitlines()[0].strip().strip('"')
        return line[:240] if line else _fallback(event, ctx)
    except Exception:
        return _fallback(event, ctx)
