"""Planner agent: turns a raw user query into a task graph (nodes + dependencies).
Uses the cheap/fast LLM tier with structured JSON output; falls back to a fixed
(but still parallel, still multi-branch) topology if no LLM key is configured or
the model's output doesn't validate.
"""
from shared.llm import call_llm, has_llm

VALID_TYPES = {"web_search", "fact_check", "data_enrichment", "summarize", "compile_report"}


def _validate_graph(graph: dict) -> bool:
    if not graph or not isinstance(graph.get("tasks"), list) or len(graph["tasks"]) == 0:
        return False
    tasks = graph["tasks"]
    ids = {t.get("task_id") for t in tasks}
    if len(ids) != len(tasks):
        return False
    for t in tasks:
        if not t.get("task_id") or t.get("type") not in VALID_TYPES:
            return False
        if not isinstance(t.get("depends_on"), list):
            return False
        for dep in t["depends_on"]:
            if dep not in ids:
                return False
    if not any(t["type"] == "compile_report" for t in tasks):
        return False
    if not any(t["type"] == "web_search" for t in tasks):
        return False
    return True


def _build_default_graph(query: str) -> dict:
    claim = query.rstrip("?") + " is a well-documented, verifiable topic"
    return {
        "tasks": [
            {"task_id": "t1", "type": "web_search", "depends_on": [], "inputs": {"query": query}},
            {
                "task_id": "t2",
                "type": "web_search",
                "depends_on": [],
                "inputs": {"query": f"{query} statistics data"},
            },
            {"task_id": "t3", "type": "data_enrichment", "depends_on": ["t1", "t2"], "inputs": {}},
            {
                "task_id": "t4",
                "type": "fact_check",
                "depends_on": ["t1", "t2"],
                "inputs": {"claim": claim},
            },
            {
                "task_id": "t5",
                "type": "summarize",
                "depends_on": ["t1", "t2", "t3", "t4"],
                "inputs": {},
            },
            {"task_id": "t6", "type": "compile_report", "depends_on": ["t5"], "inputs": {}},
        ]
    }


async def plan_query(query: str) -> dict:
    if not has_llm():
        return _build_default_graph(query)

    try:
        graph = await call_llm(
            tier="cheap",
            system="""You are a research planner. Decompose the user's query into a task graph for a multi-agent research pipeline.

Allowed task types (exactly these strings): "web_search", "fact_check", "data_enrichment", "summarize", "compile_report".

Rules:
- Produce 1-2 "web_search" tasks with no dependencies (depends_on: []), each with inputs.query being a good search query string.
- Produce exactly 1 "data_enrichment" task depending on all web_search task_ids.
- Produce exactly 1 "fact_check" task depending on all web_search task_ids, with inputs.claim being one specific, checkable factual claim relevant to the query (your best guess of a key claim worth verifying - a real search will corroborate or refute it later).
- Produce exactly 1 "summarize" task depending on the data_enrichment and fact_check task_ids (and web_search ones too).
- Produce exactly 1 "compile_report" task depending on the summarize task_id. It must be the only task with no other task depending on it.
- task_id values are short strings like "t1", "t2", etc, each unique.

Output ONLY JSON: {"tasks": [{"task_id": string, "type": string, "depends_on": string[], "inputs": object}]}""",
            user=f"User query: {query}",
            json_mode=True,
            max_tokens=800,
        )

        if _validate_graph(graph):
            return graph
        print("[planner] LLM produced an invalid task graph, falling back to default topology")
        return _build_default_graph(query)
    except Exception as err:
        print(f"[planner] LLM planning failed, falling back to default topology: {err}")
        return _build_default_graph(query)
