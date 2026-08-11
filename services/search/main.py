import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from x402.fastapi.middleware import require_payment

from shared.config import NETWORK, FACILITATOR_URL, PORTS
from shared.wallet import get_seller_payto_address
from shared.search_logic import handle_search, random_provider_price
from shared.x402_client import price_to_number

PROVIDER_NAME = "search"
app = FastAPI(title=f"Search API ({PROVIDER_NAME})")

PAY_TO = get_seller_payto_address()
PRICE = random_provider_price()

app.middleware("http")(
    require_payment(
        price=PRICE,
        pay_to_address=PAY_TO,
        path="/api/search",
        network=NETWORK,
        description="Web search results (up to 5 sources)",
        facilitator_config={"url": FACILITATOR_URL},
    )
)

app.post("/api/search")(handle_search)


@app.get("/quote")
async def quote():
    """Unauthenticated - lets the orchestrator's reverse auction compare providers'
    prices before paying any of them."""
    return {"provider": PROVIDER_NAME, "price_usdc": price_to_number(PRICE)}


@app.get("/health")
async def health():
    return {"ok": True, "service": PROVIDER_NAME, "price_usdc": price_to_number(PRICE)}


if __name__ == "__main__":
    import uvicorn

    print(f"[{PROVIDER_NAME}] listening on :{PORTS['search']} (payTo {PAY_TO}, price {PRICE})")
    uvicorn.run(app, host="0.0.0.0", port=PORTS["search"])
