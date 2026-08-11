"""Final-report self-critique pass: after the report is written, a second (cheap-tier)
LLM call independently reviews every citation's claim and flags weak/unsupported ones -
the agent red-teaming its own output before it's returned to the caller. This is
distinct from the mid-pipeline fact_check task (which verifies one specific claim
against context): this is a holistic pass over everything that made it into the final
report.

Best-effort: if no LLM key is set or the call fails, citations are returned unreviewed
(review_confidence/review_note left None) rather than blocking the job.
"""
from .llm import call_llm, has_llm

_CONFIDENCE_WEIGHTS = {"strong": 1.0, "moderate": 0.6, "weak": 0.2}


async def critique_citations(query: str, citations: list[dict]) -> list[dict]:
    if not citations or not has_llm("aux"):
        return citations

    items_text = "\n".join(
        f"- id={c['task_id']} source={c['source_service']} url={c.get('source_url') or 'none'}\n  claim: {c['claim']}"
        for c in citations
    )

    try:
        result = await call_llm(
            # Orchestrator-side review, not paid-service work - kept off Groq so its
            # citation-sized prompt can't starve the microservices. See llm.py.
            tier="aux",
            reasoning_effort="none",
            system=(
                "You are a rigorous fact-checking critic reviewing a research report's citations before "
                "publication. For EACH citation, judge how well-supported it looks from what's given (a "
                "claim, its source service, and an optional URL) - treat claims with no source URL, "
                "single-source claims, or vague/unquantified claims as weaker. "
                'Output JSON only: {"reviews": [{"task_id": string, "confidence": "strong"|"moderate"|'
                '"weak", "note": string (max 15 words)}]}. One review per citation given, matched by '
                "task_id (if the same task_id repeats, review it once - it'll be applied to all)."
            ),
            user=f"Report topic: {query}\n\nCitations:\n{items_text}",
            json_mode=True,
            max_tokens=1200,
            timeout=15,
        )
        reviews = {r["task_id"]: r for r in result.get("reviews", []) if r.get("task_id")}
    except Exception as err:
        print(f"[self-critique] review failed, leaving citations unreviewed: {err}")
        return citations

    for c in citations:
        review = reviews.get(c["task_id"])
        if review and review.get("confidence") in _CONFIDENCE_WEIGHTS:
            c["review_confidence"] = review["confidence"]
            c["review_note"] = review.get("note")
    return citations


def summarize_trust(citations: list[dict]) -> tuple[float | None, int | None]:
    """(trust_score, weak_claims_count) - None/None if nothing was reviewed (no LLM key
    or the critique call failed), so an unreviewed report never shows a misleading
    100% trust score."""
    reviewed = [c for c in citations if c.get("review_confidence") in _CONFIDENCE_WEIGHTS]
    if not reviewed:
        return None, None
    trust_score = round(sum(_CONFIDENCE_WEIGHTS[c["review_confidence"]] for c in reviewed) / len(reviewed), 3)
    weak_count = sum(1 for c in reviewed if c["review_confidence"] == "weak")
    return trust_score, weak_count
