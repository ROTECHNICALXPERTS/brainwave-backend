import os


def _num(v, fallback):
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
RPC_URL = os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
NETWORK = "base-sepolia"

PORTS = {
    "search": _num(os.getenv("PORT_SEARCH"), 4001),
    "fact_check": _num(os.getenv("PORT_FACT_CHECK"), 4002),
    "enrichment": _num(os.getenv("PORT_ENRICHMENT"), 4003),
    "summarizer": _num(os.getenv("PORT_SUMMARIZER"), 4004),
    "report_gen": _num(os.getenv("PORT_REPORT_GEN"), 4005),
    "report_gen_cheap": _num(os.getenv("PORT_REPORT_GEN_CHEAP"), 4006),
    "orchestrator": _num(os.getenv("PORT_ORCHESTRATOR"), 4000),
    "search_b": _num(os.getenv("PORT_SEARCH_B"), 4007),
    "search_c": _num(os.getenv("PORT_SEARCH_C"), 4008),
}

SERVICE_URLS = {
    "search": os.getenv("SEARCH_URL", "http://localhost:4001"),
    "fact_check": os.getenv("FACT_CHECK_URL", "http://localhost:4002"),
    "enrichment": os.getenv("ENRICHMENT_URL", "http://localhost:4003"),
    "summarizer": os.getenv("SUMMARIZER_URL", "http://localhost:4004"),
    "report_gen": os.getenv("REPORT_GEN_URL", "http://localhost:4005"),
    "report_gen_cheap": os.getenv("REPORT_GEN_CHEAP_URL", "http://localhost:4006"),
    "search_b": os.getenv("SEARCH_B_URL", "http://localhost:4007"),
    "search_c": os.getenv("SEARCH_C_URL", "http://localhost:4008"),
}

# Task types with a live reverse auction: before paying, the orchestrator calls
# GET /quote on each of these SERVICE_URLS keys and pays whichever quotes cheapest
# (see orchestrate.py `_run_auction`). Each entry must serve the same path as the
# task's default (TASK_SERVICE_MAP[...]["path"]).
AUCTION_PROVIDERS = {
    "web_search": ["search", "search_b", "search_c"],
}

# Per-service USDC pricing. report_gen has two tiers so the orchestrator can
# demonstrate switching to a cheaper provider when it's near the budget cap.
PRICES = {
    "search": "$0.002",
    "fact_check": "$0.003",
    "enrichment": "$0.002",
    "summarize": "$0.004",
    "report_premium": "$0.006",
    "report_cheap": "$0.003",
}

SERVICE_LABELS = {
    "web_search": "Search API",
    "fact_check": "Fact-Check API",
    "data_enrichment": "Enrichment API",
    "summarize": "Summarizer API",
    "compile_report": "Report-Gen API",
}

# Task type -> which service handles it, and which price tier(s) exist for it.
TASK_SERVICE_MAP = {
    "web_search": {
        "service": "search",
        "path": "/api/search",
        "tiers": {"standard": {"price": PRICES["search"], "path": "/api/search"}},
    },
    "fact_check": {
        "service": "fact_check",
        "path": "/api/fact-check",
        "tiers": {"standard": {"price": PRICES["fact_check"], "path": "/api/fact-check"}},
    },
    "data_enrichment": {
        "service": "enrichment",
        "path": "/api/enrich",
        "tiers": {"standard": {"price": PRICES["enrichment"], "path": "/api/enrich"}},
    },
    "summarize": {
        "service": "summarizer",
        "path": "/api/summarize",
        "tiers": {"standard": {"price": PRICES["summarize"], "path": "/api/summarize"}},
    },
    "compile_report": {
        "service": "report_gen",
        "path": "/api/report",
        # Each tier can name its own "service" (SERVICE_URLS key) - premium and cheap are two
        # independent processes/ports, not two routes on one app, so failure of one (e.g. the
        # process being killed) never takes the other down with it.
        "tiers": {
            "premium": {"price": PRICES["report_premium"], "path": "/api/report", "service": "report_gen"},
            "cheap": {"price": PRICES["report_cheap"], "path": "/api/report", "service": "report_gen_cheap"},
        },
    },
}
