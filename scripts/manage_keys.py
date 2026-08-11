#!/usr/bin/env python3
"""Mint, list and revoke orchestrator API keys.

    python scripts/manage_keys.py create "claude-desktop" --daily-cap 0.50
    python scripts/manage_keys.py list
    python scripts/manage_keys.py revoke ar_sk_AbCdEfGh

A key is shown exactly once, at creation. Only its SHA-256 is stored, so there is no
command to print it again - if it is lost, revoke it and mint another.

Defaults are deliberately small. Each research job costs roughly $0.019 in testnet USDC,
so the default 0.50 USDC/day is about 25 jobs - enough for a developer to try the tool
properly, not enough for one leaked key to drain the wallet.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import api_keys  # noqa: E402


def cmd_create(args: argparse.Namespace) -> int:
    raw, record = api_keys.create_key(
        args.label,
        daily_cap_usdc=args.daily_cap,
        max_job_budget_usdc=args.max_job_budget,
        max_jobs_per_hour=args.max_jobs_per_hour,
    )
    print()
    print(f"  API key for {record['label']!r} — copy it now, it is not stored and cannot be shown again:")
    print()
    print(f"    {raw}")
    print()
    print(f"  daily cap        {record['daily_cap_usdc']:.4f} USDC")
    print(f"  max job budget   {record['max_job_budget_usdc']:.4f} USDC")
    print(f"  rate limit       {record['max_jobs_per_hour']} jobs/hour")
    print(f"  prefix           {record['key_prefix']}   (use this to revoke)")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    keys = api_keys.list_keys()
    if not keys:
        print("No API keys yet. Create one with:  python scripts/manage_keys.py create <label>")
        return 0

    print(f"{'PREFIX':<16} {'LABEL':<22} {'DAILY CAP':>10} {'SPENT TODAY':>12} {'JOBS/HR':>8}  STATUS")
    for k in keys:
        spent = api_keys.exposure_today(k["id"])
        used = api_keys.jobs_last_hour(k["id"])
        status = "active" if k["active"] else "revoked"
        print(
            f"{k['key_prefix']:<16} {(k['label'] or '-'):<22} "
            f"{k['daily_cap_usdc']:>10.4f} {spent:>12.4f} "
            f"{used:>3}/{k['max_jobs_per_hour']:<4}  {status}"
        )

    anon = api_keys.caller_for(None)
    print()
    print(
        f"anonymous callers: {api_keys.exposure_today(None):.4f} / {anon.limits.daily_cap_usdc:.4f} USDC today"
        f"   (blocked entirely when REQUIRE_API_KEY=true)"
    )
    print(
        f"service-wide:      {api_keys.global_exposure_today():.4f} / {api_keys.global_daily_cap_usdc():.4f} USDC today"
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if api_keys.revoke_key(args.prefix):
        print(f"Revoked {args.prefix}.")
        return 0
    print(f"No active key with prefix {args.prefix!r}. Run 'list' to see the current keys.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="mint a new API key")
    p_create.add_argument("label", help="who this key is for, e.g. 'claude-desktop'")
    p_create.add_argument("--daily-cap", type=float, default=0.50, help="max USDC per UTC day (default: 0.50)")
    p_create.add_argument(
        "--max-job-budget", type=float, default=0.10, help="max budget_cap_usdc for one job (default: 0.10)"
    )
    p_create.add_argument("--max-jobs-per-hour", type=int, default=10, help="rate limit (default: 10)")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="show all keys and today's spend")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="deactivate a key by its prefix")
    p_revoke.add_argument("prefix", help="the key prefix shown by 'list', e.g. ar_sk_AbCdEfGh")
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
