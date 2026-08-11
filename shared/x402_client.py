"""Reusable x402 payment client: given a target URL, makes the request, and if the
server responds 402, parses the payment requirements, signs a gasless EIP-3009 USDC
payment authorization via the official x402 Python SDK, retries with payment
attached, and returns the final response. Every attempt is logged to the ledger.
"""
from x402.clients.httpx import x402HttpxClient
from x402.clients.base import decode_x_payment_response

from .wallet import get_buyer_account
from . import ledger
from .config import NETWORK


def price_to_atomic_usdc(price_str: str) -> int:
    dollars = float(str(price_str).replace("$", ""))
    return round(dollars * 1_000_000)


def price_to_number(price_str: str) -> float:
    return float(str(price_str).replace("$", ""))


async def pay_and_fetch_json(
    *,
    job_id: str,
    task_id: str,
    task_type: str,
    url: str,
    price_usdc: str,
    tier: str,
    body: dict,
) -> dict:
    account = get_buyer_account()
    max_value = price_to_atomic_usdc(price_usdc)
    amount = price_to_number(price_usdc)

    ledger.append_payment(
        job_id=job_id,
        task_id=task_id,
        task_type=task_type,
        endpoint=url,
        amount_usdc=amount,
        tier=tier,
        status="paying",
    )

    try:
        async with x402HttpxClient(account=account, max_value=max_value, timeout=60) as client:
            response = await client.post(url, json=body or {})

            if response.status_code >= 400:
                text = response.text
                ledger.append_payment(
                    job_id=job_id,
                    task_id=task_id,
                    task_type=task_type,
                    endpoint=url,
                    amount_usdc=amount,
                    tier=tier,
                    status="failed",
                    error=f"HTTP {response.status_code}: {text[:300]}",
                )
                raise RuntimeError(f"Request to {url} failed with {response.status_code}: {text[:300]}")

            tx_hash = None
            network = NETWORK
            payment_response_header = response.headers.get("x-payment-response")
            if payment_response_header:
                try:
                    decoded = decode_x_payment_response(payment_response_header)
                    tx_hash = decoded.get("transaction")
                    network = decoded.get("network") or network
                except Exception:
                    pass  # no payment was actually required for this call - not fatal

            data = response.json()

        explorer_url = f"https://sepolia.basescan.org/tx/{tx_hash}" if tx_hash else None
        ledger.append_payment(
            job_id=job_id,
            task_id=task_id,
            task_type=task_type,
            endpoint=url,
            amount_usdc=amount,
            tier=tier,
            status="paid",
            tx_hash=tx_hash,
            explorer_url=explorer_url,
        )

        return {"data": data, "tx_hash": tx_hash, "explorer_url": explorer_url}

    except Exception as err:
        if not isinstance(err, RuntimeError):
            ledger.append_payment(
                job_id=job_id,
                task_id=task_id,
                task_type=task_type,
                endpoint=url,
                amount_usdc=amount,
                tier=tier,
                status="failed",
                error=str(err),
            )
        raise
