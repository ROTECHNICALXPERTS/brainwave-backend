"""Curated fallback results used by the Search API when TAVILY_API_KEY is not set,
so the whole pipeline still runs end-to-end without any search API key."""
from urllib.parse import quote


def mock_search_results(query: str) -> list[dict]:
    topic = " ".join(query.lower().split()[:6])
    return [
        {
            "title": f"{query} — Overview and background",
            "url": f"https://en.wikipedia.org/wiki/{quote(topic.replace(' ', '_'))}",
            "snippet": (
                f'A general overview of "{query}", covering its origins, key developments, '
                "and current relevance. This curated result stands in for a live search API."
            ),
            "published_date": "2025-01-15",
        },
        {
            "title": f"Recent developments in {topic}",
            "url": f"https://www.reuters.com/search/?q={quote(topic)}",
            "snippet": (
                f'Recent reporting and analysis related to "{query}", summarizing notable events '
                "and expert commentary from the past year."
            ),
            "published_date": "2026-03-02",
        },
        {
            "title": f"{topic}: data and statistics",
            "url": f"https://ourworldindata.org/search?q={quote(topic)}",
            "snippet": (
                f'Quantitative data, charts, and statistics relevant to "{query}", useful for '
                "grounding claims with numbers."
            ),
            "published_date": "2025-11-20",
        },
    ]
