import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException, Request
from x402.fastapi.middleware import require_payment

from shared.config import PRICES, NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.llm import call_llm, has_llm

app = FastAPI(title="Enrichment API")

PAY_TO = get_seller_payto_address()

app.middleware("http")(
    require_payment(
        price=PRICES["enrichment"],
        pay_to_address=PAY_TO,
        path="/api/enrich",
        network=NETWORK,
        description="Structured metadata extraction (entities, dates, stats)",
        facilitator_config={"url": FACILITATOR_URL},
    )
)

DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}|\d{4})\b"
)
STAT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent|million|billion|trillion|thousand|x)\b", re.IGNORECASE)
ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b")
STOPWORDS = {"The", "This", "That", "These", "Those", "It", "In", "On", "At", "A", "An"}


def heuristic_enrich(text: str) -> dict:
    dates = list(dict.fromkeys(DATE_RE.findall(text)))[:10]
    stats = list(dict.fromkeys(STAT_RE.findall(text)))[:10]
    entities = [e for e in dict.fromkeys(ENTITY_RE.findall(text)) if e not in STOPWORDS and len(e) > 2][:10]
    return {"entities": entities, "dates": dates, "stats": stats, "method": "heuristic"}


@app.post("/api/enrich")
async def enrich(request: Request):
    body = await request.json()
    text = body.get("text")
    if not text or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Missing required field: text (string)")

    if not has_llm():
        return heuristic_enrich(text)

    try:
        result = await call_llm(
            tier="cheap",
            system=(
                "Extract structured metadata from the given text. Output JSON: "
                '{"entities": string[] (people, organizations, places), "dates": string[], '
                '"stats": string[] (numeric facts/statistics, kept as written)}. '
                "Keep each array to at most 8 items, most important first."
            ),
            user=text[:4000],
            json_mode=True,
            max_tokens=400,
        )
        return {**result, "method": "llm"}
    except Exception as err:
        print(f"[enrichment] LLM call failed, falling back to heuristic: {err}")
        return heuristic_enrich(text)


@app.get("/health")
async def health():
    return {"ok": True, "service": "enrichment"}


if __name__ == "__main__":
    import uvicorn

    print(f"[enrichment] listening on :{PORTS['enrichment']} (payTo {PAY_TO})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["enrichment"])
