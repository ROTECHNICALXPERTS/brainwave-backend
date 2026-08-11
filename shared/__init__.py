"""Shared package: config, schemas, wallet, payment ledger, x402 client, LLM wrapper.

Importing `shared` (or any `shared.*` submodule) always loads the repo root .env
first, before any submodule reads os.environ, since this __init__ runs first.
"""
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
