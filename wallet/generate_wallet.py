#!/usr/bin/env python3
"""Generates two Base Sepolia testnet accounts and writes them into .env:
  - BUYER_PRIVATE_KEY / BUYER_ADDRESS: the orchestrator signs x402 payments with this
    one. Fund THIS with testnet USDC.
  - SELLER_PAYTO_ADDRESS: the address all microservices receive payment to. No
    funding needed, no private key required.
Never uses mainnet. Safe to re-run - refuses to overwrite an existing populated
.env unless --force is passed.
"""
import re
import shutil
import sys
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
FORCE = "--force" in sys.argv


def set_env_var(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{key}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text)
    return text + f"\n{key}={value}"


def main():
    if not ENV_PATH.exists():
        shutil.copyfile(EXAMPLE_PATH, ENV_PATH)

    env_text = ENV_PATH.read_text()

    already_has_wallet = re.search(r"BUYER_PRIVATE_KEY=0x[0-9a-fA-F]{64}", env_text)
    if already_has_wallet and not FORCE:
        print("A wallet is already configured in .env (BUYER_PRIVATE_KEY is set).")
        print("Re-run with --force to overwrite it with a brand new one.")
        addr_match = re.search(r"BUYER_ADDRESS=(0x[0-9a-fA-F]{40})", env_text)
        print("Existing buyer address:", addr_match.group(1) if addr_match else "(unknown)")
        return

    buyer = Account.create()
    seller = Account.create()

    key_hex = buyer.key.hex()
    if not key_hex.startswith("0x"):
        key_hex = "0x" + key_hex

    env_text = set_env_var(env_text, "BUYER_PRIVATE_KEY", key_hex)
    env_text = set_env_var(env_text, "BUYER_ADDRESS", buyer.address)
    env_text = set_env_var(env_text, "SELLER_PAYTO_ADDRESS", seller.address)

    ENV_PATH.write_text(env_text)

    print("Generated Base Sepolia testnet wallets and wrote them to .env\n")
    print("BUYER (orchestrator / pays for API calls):")
    print("  address:     ", buyer.address)
    print("  private key: ", key_hex, "(also saved to .env - keep this file out of git)")
    print("\nSELLER (payTo address for all microservices - receives USDC, no key needed):")
    print("  address:     ", seller.address)
    print("\nNEXT STEP (manual, required):")
    print("  Fund the BUYER address with Base Sepolia testnet USDC:")
    print("  1. Get Base Sepolia ETH (for gas - only needed if you ever do non-gasless txs):")
    print("     https://www.alchemy.com/faucets/base-sepolia")
    print("  2. Get Base Sepolia testnet USDC from Circle's faucet:")
    print("     https://faucet.circle.com  (select network 'Base Sepolia', paste address:")
    print("    ", buyer.address, ")")
    print("  x402 payments use EIP-3009 transferWithAuthorization and are gasless for the buyer,")
    print("  so you only strictly need testnet USDC in the BUYER address, not ETH.")


if __name__ == "__main__":
    main()
