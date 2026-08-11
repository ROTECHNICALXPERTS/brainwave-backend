"""Search logic shared by the 3 independent search-provider processes (services/search,
services/search-b, services/search-c) that compete in the orchestrator's reverse
auction for web_search tasks (see orchestrate.py `_run_auction`).
"""
import os
import random

import httpx
from fastapi import HTTPException, Request

from .mock_data import mock_search_results


def random_provider_price(low: float = 0.0015, high: float = 0.0045) -> str:
    """Each search provider process picks one price at startup, pseudo-randomly
    within a realistic band, and keeps it for its lifetime. This is what its /quote
    endpoint reports and what its x402 middleware actually charges (they must always
    agree - see services/search*/main.py), so a restart is what changes a provider's
    price, simulating "market conditions changed" for the reverse-auction demo."""
    return f"${round(random.uniform(low, high), 4)}"


async def handle_search(request: Request) -> dict:
    body = await request.json()
    query = body.get("query")
    if not query or not isinstance(query, str):
        raise HTTPException(status_code=400, detail="Missing required field: query (string)")

    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                tv_res = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 5,
                    },
                )
            if tv_res.status_code >= 400:
                raise RuntimeError(f"Tavily API error {tv_res.status_code}")
            tv_data = tv_res.json()
            results = [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": r.get("content"),
                    "published_date": r.get("published_date"),
                }
                for r in tv_data.get("results", [])
            ]
            return {"query": query, "results": results, "provider": "tavily"}
        except Exception as err:
            print(f"[search] Tavily call failed, falling back to mock data: {err}")

    return {"query": query, "results": mock_search_results(query), "provider": "mock"}
