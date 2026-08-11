"""On-chain provenance receipt: after the citation compiler produces the final
report, we hash its canonical JSON and submit ONE small Base Sepolia transaction
carrying that hash in its calldata. Anyone can independently verify the report
wasn't altered after the fact by refetching /report, recomputing the hash the same
way, and checking it matches what's on that transaction.

Unlike x402 payments (gasless EIP-3009 USDC transfers), this is a plain
self-send transaction with custom data, so it costs a small amount of real Base
Sepolia testnet ETH for gas - the buyer wallet needs both testnet USDC (for x402
payments) and a little testnet ETH (for this one receipt tx) to see this feature
actually settle on-chain.
"""
import hashlib
import json

from web3 import Web3

from .config import RPC_URL
from .wallet import get_buyer_account

BASE_SEPOLIA_CHAIN_ID = 84532


def compute_report_hash(payload: dict) -> str:
    """SHA-256 over a canonical (sorted-key, compact) JSON encoding, so the same
    logical content always hashes the same way regardless of dict ordering."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_provenance_tx(report_hash_hex: str) -> dict:
    """Submits the hash on-chain. Raises on failure (e.g. no testnet ETH for gas) -
    callers should treat this as best-effort and not fail the whole job over it."""
    account = get_buyer_account()
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    data = report_hash_hex if report_hash_hex.startswith("0x") else "0x" + report_hash_hex

    tx = {
        "from": account.address,
        "to": account.address,
        "value": 0,
        "gas": 60_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": BASE_SEPOLIA_CHAIN_ID,
        "data": data,
    }
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    return {"tx_hash": tx_hash, "explorer_url": f"https://sepolia.basescan.org/tx/{tx_hash}"}
