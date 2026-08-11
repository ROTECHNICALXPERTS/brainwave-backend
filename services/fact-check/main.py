import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException, Request
from x402.fastapi.middleware import require_payment

from shared.config import PRICES, NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.mock_data import mock_search_results
from shared.llm import call_llm, has_llm

app = FastAPI(title="Fact-Check API")

PAY_TO = get_seller_payto_address()

app.middleware("http")(
    require_payment(
        price=PRICES["fact_check"],
        pay_to_address=PAY_TO,
        path="/api/fact-check",
        network=NETWORK,
        description="Cross-reference a claim against a second source",
        facilitator_config={"url": FACILITATOR_URL},
    )
)


def heuristic_verdict(claim: str, corroboration: list[dict]) -> dict:
    claim_words = {w for w in re.split(r"\W+", claim.lower()) if len(w) > 4}
    best_overlap = 0
    best_source = corroboration[0]
    for source in corroboration:
        text = f"{source['title']} {source['snippet']}".lower()
        overlap = sum(1 for w in claim_words if w in text)
        if overlap > best_overlap:
            best_overlap = overlap
            best_source = source
    confidence = min(0.9, best_overlap / max(4, len(claim_words)))
    return {
        "verdict": "likely_supported" if confidence > 0.35 else "unverified",
        "confidence": round(confidence, 2),
        "explanation": (
            f'Heuristic keyword overlap with a second source ("{best_source["title"]}") was '
            f"{confidence * 100:.0f}%. No LLM key configured, so this is a shallow lexical check, "
            "not semantic verification."
        ),
        "evidence_url": best_source["url"],
        "method": "heuristic",
    }


@app.post("/api/fact-check")
async def fact_check(request: Request):
    body = await request.json()
    claim = body.get("claim")
    context = body.get("context")
    if not claim or not isinstance(claim, str):
        raise HTTPException(status_code=400, detail="Missing required field: claim (string)")

    corroboration = mock_search_results(claim)

    if not has_llm():
        return {"claim": claim, **heuristic_verdict(claim, corroboration)}

    try:
        sources_text = "\n".join(
            f"[{i + 1}] {s['title']} — {s['snippet']} ({s['url']})" for i, s in enumerate(corroboration)
        )
        result = await call_llm(
            tier="cheap",
            system=(
                "You are a fact-checking assistant. Given a claim and candidate corroborating sources, "
                'output JSON: {"verdict": "supported"|"disputed"|"unverified", "confidence": number between 0 and 1, '
                '"explanation": string (1-2 sentences), "evidence_url": string (best matching source url, or null)}. '
                'Be conservative: if sources don\'t clearly address the claim, use "unverified".'
            ),
            user=f'Claim: "{claim}"\n{f"Context: {context}" if context else ""}\n\nCandidate sources:\n{sources_text}',
            json_mode=True,
            max_tokens=400,
        )
        return {"claim": claim, **result, "method": "llm"}
    except Exception as err:
        print(f"[fact-check] LLM call failed, falling back to heuristic: {err}")
        return {"claim": claim, **heuristic_verdict(claim, corroboration)}


@app.get("/health")
async def health():
    return {"ok": True, "service": "fact-check"}


if __name__ == "__main__":
    import uvicorn

    print(f"[fact-check] listening on :{PORTS['fact_check']} (payTo {PAY_TO})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["fact_check"])
