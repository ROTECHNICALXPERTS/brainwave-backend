import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from x402.fastapi.middleware import require_payment

from shared.config import PRICES, NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.report_logic import make_report_handler

app = FastAPI(title="Report-Gen API (cheap)")

PAY_TO = get_seller_payto_address()

# Cheap tier only - fast model, lower price. Independent process/port from the
# premium report-gen service, so the orchestrator's budget guard and its
# failure-triggered fallback both target a service that's still up even if the
# premium one has been killed.
app.middleware("http")(
    require_payment(
        price=PRICES["report_cheap"],
        pay_to_address=PAY_TO,
        path="/api/report",
        network=NETWORK,
        description="Final cited report synthesis (cheap/fast model)",
        facilitator_config={"url": FACILITATOR_URL},
    )
)

app.post("/api/report")(make_report_handler("cheap"))


@app.get("/health")
async def health():
    return {"ok": True, "service": "report-gen-cheap"}


if __name__ == "__main__":
    import uvicorn

    print(f"[report-gen-cheap] listening on :{PORTS['report_gen_cheap']} (payTo {PAY_TO})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["report_gen_cheap"])
