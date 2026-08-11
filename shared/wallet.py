import os
from typing import Optional
from eth_account import Account
from eth_account.signers.local import LocalAccount

_cached_account: Optional[LocalAccount] = None


def get_buyer_account() -> LocalAccount:
    """Loads the buyer's local account from BUYER_PRIVATE_KEY (lazy, so this module
    can be imported before dotenv has loaded env vars)."""
    global _cached_account
    if _cached_account is not None:
        return _cached_account

    key = os.getenv("BUYER_PRIVATE_KEY")
    if not key or not key.startswith("0x") or len(key) != 66:
        raise RuntimeError(
            "BUYER_PRIVATE_KEY is missing or malformed in .env. "
            "Run `python wallet/generate_wallet.py` first."
        )
    _cached_account = Account.from_key(key)
    return _cached_account


def get_seller_payto_address() -> str:
    addr = os.getenv("SELLER_PAYTO_ADDRESS")
    if not addr:
        raise RuntimeError(
            "SELLER_PAYTO_ADDRESS is missing in .env. Run `python wallet/generate_wallet.py` first."
        )
    return addr
