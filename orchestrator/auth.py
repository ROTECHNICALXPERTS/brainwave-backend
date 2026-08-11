"""Caller identification for the orchestrator's HTTP API.

Kept separate from the limit logic in shared/api_keys.py: this module only answers "who
is this request from", and translates a refusal into the right HTTP status. What the
caller is then allowed to spend is decided there.

A key may arrive as either `Authorization: Bearer <key>` or `X-API-Key: <key>` - the
first is what most HTTP clients and the MCP server send, the second is easier to type by
hand when testing with curl.
"""
from fastapi import Header, HTTPException

from shared import api_keys
from shared.api_keys import Caller


def _extract(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        # A bare token with no scheme is a common enough mistake to just accept.
        return authorization.strip()
    return None


async def resolve_caller(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Caller:
    """FastAPI dependency yielding the calling identity.

    A *wrong* key is always rejected, even when keys are optional - silently downgrading
    a bad key to anonymous would hand the caller anonymous's quota and hide the typo.
    A *missing* key is only rejected when REQUIRE_API_KEY is set, which is what keeps the
    local demo and the React frontend working untouched.
    """
    raw = _extract(authorization, x_api_key)

    if raw:
        record = api_keys.lookup_key(raw)
        if record is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        return api_keys.caller_for(record)

    if api_keys.require_api_key():
        raise HTTPException(
            status_code=401,
            detail="API key required. Send it as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
        )

    return api_keys.caller_for(None)


def assert_can_read_job(caller: Caller, job_id: str) -> None:
    """Job ids are unguessable UUIDs, so holding one is treated as permission to read it
    while keys are optional. Once keys are required the ownership check is real: one
    caller's job must not be readable by another's key."""
    if not api_keys.require_api_key():
        return

    owner = api_keys.owner_of(job_id)
    if owner is None:
        return  # predates ownership tracking; the 404 from the job store is the real answer

    if owner["key_id"] != caller.key_id:
        # Deliberately 404, not 403: confirming the job exists would leak that a given
        # job id is real to someone who has no claim to it.
        raise HTTPException(status_code=404, detail="job not found")
