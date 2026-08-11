import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException, Request
from x402.fastapi.middleware import require_payment

from shared.config import PRICES, NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.llm import call_llm, has_llm

app = FastAPI(title="Summarizer API")

PAY_TO = get_seller_payto_address()

app.middleware("http")(
    require_payment(
        price=PRICES["summarize"],
        pay_to_address=PAY_TO,
        path="/api/summarize",
        network=NETWORK,
        description="LLM summarization of source text",
        facilitator_config={"url": FACILITATOR_URL},
    )
)


def heuristic_summarize(text: str, max_words: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip())
    out = ""
    for s in sentences:
        candidate = (out + " " + s).strip()
        if len(candidate.split()) > max_words:
            break
        out = candidate
    return out or " ".join(text.split()[:max_words])


@app.post("/api/summarize")
async def summarize(request: Request):
    body = await request.json()
    text = body.get("text")
    max_words = int(body.get("max_words") or 80)
    if not text or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Missing required field: text (string)")

    if not has_llm():
        return {"summary": heuristic_summarize(text, max_words), "method": "heuristic"}

    try:
        summary = await call_llm(
            tier="cheap",
            system=(
                f"Summarize the given text in at most {max_words} words. Be factual and concise. "
                "Output plain text only, no preamble."
            ),
            user=text[:6000],
            max_tokens=300,
        )
        return {"summary": summary.strip(), "method": "llm"}
    except Exception as err:
        print(f"[summarizer] LLM call failed, falling back to heuristic: {err}")
        return {"summary": heuristic_summarize(text, max_words), "method": "heuristic"}


@app.get("/health")
async def health():
    return {"ok": True, "service": "summarizer"}


if __name__ == "__main__":
    import uvicorn

    print(f"[summarizer] listening on :{PORTS['summarizer']} (payTo {PAY_TO})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["summarizer"])
