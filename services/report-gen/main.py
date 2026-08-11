import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from x402.fastapi.middleware import require_payment

from shared.config import PRICES, NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.report_logic import make_report_handler

app = FastAPI(title="Report-Gen API (premium)")

PAY_TO = get_seller_payto_address()

# Premium tier only - strong model, higher price. Runs as its own process on its own
# port so that killing it (e.g. to demo failover) genuinely takes only this tier
# down; the cheap tier (services/report-gen-cheap) keeps running independently.
app.middleware("http")(
    require_payment(
        price=PRICES["report_premium"],
        pay_to_address=PAY_TO,
        path="/api/report",
        network=NETWORK,
        description="Final cited report synthesis (premium/strong model)",
        facilitator_config={"url": FACILITATOR_URL},
    )
)

app.post("/api/report")(make_report_handler("strong"))


@app.get("/health")
async def health():
    return {"ok": True, "service": "report-gen-premium"}


if __name__ == "__main__":
    import uvicorn

    print(f"[report-gen] listening on :{PORTS['report_gen']} (payTo {PAY_TO})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["report_gen"])
