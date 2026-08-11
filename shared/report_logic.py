"""Report synthesis logic shared by the two independent report-gen processes
(premium on its own port, cheap on its own port - see services/report-gen and
services/report-gen-cheap). Splitting them into separate processes, rather than two
routes on one app, means killing one during a demo genuinely leaves the other
reachable, so the orchestrator's tier-fallback is a real failover, not a no-op.
"""
import json
import re

from fastapi import HTTPException, Request

from .llm import call_llm, extract_json, has_llm


def heuristic_report(query: str, sections: list[dict], *, reason: str = "no LLM key configured") -> dict:
    by_type: dict[str, list[dict]] = {}
    for s in sections:
        by_type.setdefault(s["task_type"], []).append(s)

    md = f"# Research Report: {query}\n\n"
    citations = []
    for task_type, group in by_type.items():
        md += f"## {task_type.replace('_', ' ')}\n\n"
        for s in group:
            content = str(s.get("content") or "")
            claim = re.split(r"(?<=[.!?])\s+", content)[0][:220] if content else "(no content)"
            md += f"- {claim} [{s['task_id']}]\n"
            citations.append(
                {
                    "claim": claim,
                    "source_service": s.get("source_service"),
                    "source_url": s.get("url"),
                    "task_id": s["task_id"],
                }
            )
        md += "\n"
    md += f"\n_Generated with a deterministic template ({reason})._\n"
    return {"report_markdown": md, "citations": citations}


_REPORT_MARKER = "===REPORT==="
_CITATIONS_MARKER = "===CITATIONS==="


def _salvage_citations(raw: str) -> list[dict]:
    """Parse the citations array, tolerating one that was cut off by the token limit.

    The citations block is the last thing the model writes, so it is the first thing lost
    when a response runs long - and `extract_json` cannot help, because no prefix of an
    unterminated array is valid JSON. Discarding the whole response over a clipped tail
    would throw away a complete, well-formed report and fall back to the template, which
    is a far worse outcome than a report carrying one fewer citation.

    So: try the array as written, and if that fails, keep whatever complete `{...}`
    objects made it out and drop the partial one.
    """
    try:
        parsed = extract_json(raw)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass

    start = raw.find("[")
    if start == -1:
        return []

    objects: list[dict] = []
    depth = 0
    obj_start = -1
    in_string = False
    escaped = False

    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            # A quote only closes the string if it isn't itself escaped.
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                try:
                    objects.append(json.loads(raw[obj_start : i + 1], strict=False))
                except json.JSONDecodeError:
                    pass
                obj_start = -1

    return objects


async def llm_report(query: str, sections: list[dict], tier: str) -> dict:
    sections_text = "\n\n---\n\n".join(
        f"[task_id={s['task_id']} type={s['task_type']} source={s.get('source_service')}"
        f"{' url=' + s['url'] if s.get('url') else ''}]\n{str(s.get('content'))[:1500]}"
        for s in sections
    )

    # Deliberately NOT JSON mode: forcing an entire multi-paragraph markdown report to
    # live inside one JSON string value made models (Gemini in particular) frequently
    # emit unescaped newlines/quotes that broke parsing outright, or get truncated
    # mid-string with no valid closing. A plain-text delimiter format sidesteps that
    # class of bug entirely - the markdown is raw text needing no escaping, and only
    # the short citations list needs to parse as JSON (much lower risk of malformed
    # output at that size).
    system = (
        "You are a research report writer. Given a user query and a set of source sections (each tagged "
        "with a task_id and source service), write a well-organized, cited markdown research report, "
        "then list every citation.\n\n"
        f"Respond in EXACTLY this format and nothing else:\n\n{_REPORT_MARKER}\n"
        "<the markdown report, with headings - every substantive claim should reference its task_id in "
        f"brackets like [t2]>\n\n{_CITATIONS_MARKER}\n"
        '<a JSON array only, e.g. [{"claim": "...", "source_service": "...", "source_url": "..."|null, '
        '"task_id": "..."}] - one entry per claim you attributed, matched to the task_id and source_service '
        "it came from>"
    )
    user = f"Query: {query}\n\nSource sections:\n\n{sections_text}"

    async def attempt() -> str:
        return await call_llm(
            tier=tier,
            system=system,
            user=user,
            json_mode=False,
            # The report and its citations share one budget, and the citations come last,
            # so too tight a cap silently clips them. Gemini 3.x is also markedly more
            # verbose than the 2.x models this was originally tuned for - 2200 was losing
            # the tail of the citations array.
            max_tokens=4000 if tier == "strong" else 1400,
            # Raised with it: a longer response legitimately takes longer to generate, and
            # a timeout here throws away the whole report.
            timeout=60,
            # Gemini 3.x thinks by default and intermittently spills that reasoning into
            # the content field, which cannot satisfy a strict output format. Nothing here
            # benefits from chain-of-thought - the analysis already happened upstream, this
            # call only has to write it up.
            reasoning_effort="none",
        )

    result_text = await attempt()

    # One retry, because the failure is intermittent rather than deterministic and the
    # x402 payment for this call has already been made - falling straight through to the
    # template would mean paying the premium tier and shipping a deterministic stub.
    if _REPORT_MARKER not in result_text or _CITATIONS_MARKER not in result_text:
        print(f"[report-gen:{tier}] malformed response, retrying once: {result_text[:120]!r}")
        result_text = await attempt()

    if _REPORT_MARKER not in result_text or _CITATIONS_MARKER not in result_text:
        raise ValueError(f"LLM report response missing required section markers: {result_text[:200]}")

    _, rest = result_text.split(_REPORT_MARKER, 1)
    report_markdown, citations_raw = rest.split(_CITATIONS_MARKER, 1)
    report_markdown = report_markdown.strip()
    citations = _salvage_citations(citations_raw.strip())

    # The report is the deliverable; citations can survive being short a clipped entry.
    # An empty report cannot be salvaged, so that still falls back to the template.
    if not report_markdown:
        raise ValueError("LLM report response contained no report body")
    return {"report_markdown": report_markdown, "citations": citations}


def make_report_handler(tier: str):
    async def handler(request: Request):
        body = await request.json()
        query = body.get("query")
        sections = body.get("sections")
        if not query or not isinstance(sections, list):
            raise HTTPException(status_code=400, detail="Missing required fields: query (string), sections (array)")

        if not has_llm(tier):
            return {
                **heuristic_report(query, sections, reason=f"no LLM key configured for the '{tier}' tier"),
                "tier": tier,
                "method": "heuristic",
            }

        try:
            result = await llm_report(query, sections, tier)
            return {**result, "tier": tier, "method": "llm"}
        except Exception as err:
            print(f"[report-gen:{tier}] LLM call failed, falling back to heuristic: {err}")
            short_reason = " ".join(str(err).split())[:180]
            return {
                **heuristic_report(query, sections, reason=f"the '{tier}' LLM call failed this run: {short_reason}"),
                "tier": tier,
                "method": "heuristic",
            }

    return handler
