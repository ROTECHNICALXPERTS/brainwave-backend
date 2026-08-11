#!/usr/bin/env python3
"""Prints the buyer wallet's Base Sepolia ETH and testnet USDC balance so you can
confirm faucet funding worked."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


def main():
    address = os.getenv("BUYER_ADDRESS")
    if not address:
        print("BUYER_ADDRESS not set in .env - run `python wallet/generate_wallet.py` first.")
        sys.exit(1)

    rpc_url = os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    address = Web3.to_checksum_address(address)

    eth_balance = w3.eth.get_balance(address)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE_SEPOLIA), abi=ERC20_ABI)
    usdc_balance = usdc.functions.balanceOf(address).call()

    print("Buyer address:", address)
    print("Base Sepolia ETH:", Web3.from_wei(eth_balance, "ether"))
    print("Base Sepolia USDC:", usdc_balance / 1_000_000)

    if usdc_balance == 0:
        print("\nNo testnet USDC yet. Fund at https://faucet.circle.com (network: Base Sepolia).")


if __name__ == "__main__":
    main()
