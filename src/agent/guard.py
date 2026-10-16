"""Citation guard: checks an agent answer against the tool results it was built from.

Problems it catches (each one is a reason to retry once, then to repair):
- a citation [n] that points to no source
- a raw URL that no tool returned (the model may only cite graph sources)
- facts asserted although every tool said found=false (the answer must say it is not in the graph)
- a KEP that was NOT adopted (withdrawn, rejected, replaced, deferred) described as implemented or adopted

repair() strips bad citations and URLs. If a false claim survives (not-found facts or a not-adopted KEP
called adopted), fallback() replaces the whole answer with plain facts taken from the tool data.
"""
import re

from src.tools.read import NOT_ADOPTED

_CITE = re.compile(r"\[(\d+)\]")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_NOT_FOUND = re.compile(r"not (?:in|found|recorded|part of)|no (?:record|such|kep|decision|match|information|data)|"
                        r"(?:could|can)(?:n't| ?not) find|does(?:n't| not) (?:exist|appear)|unknown|nothing (?:in|about)",
                        re.I)
_ADOPTED = re.compile(r"\b(?:was|is|got|been|has been|were) (?:implemented|adopted|shipped|accepted|merged|graduated)\b|"
                      r"\b(?:went|reached) (?:ga|stable)\b", re.I)
_NEGATION = re.compile(r"\b(?:not|never|n't|no longer|instead of)\b", re.I)


def _urls(text: str) -> list[str]:
    return [u.rstrip(".,;:") for u in _URL.findall(text)]


def check(answer: str, sources: list[str], results: list[dict]) -> list[str]:
    """List of problems; empty means the answer passes."""
    problems = []
    bad = sorted({int(n) for n in _CITE.findall(answer) if not 1 <= int(n) <= len(sources)})
    if bad:
        problems.append(f"citations {bad} do not exist; only [1]..[{len(sources)}] are valid")
    foreign = [u for u in _urls(answer) if u not in sources]
    if foreign:
        problems.append(f"URLs not returned by any tool: {foreign[:3]}; cite sources only as [n]")
    if results and all(r.get("found") is False for r in results) and not _NOT_FOUND.search(answer):
        problems.append("every tool returned found=false, so the answer must say this is not in the graph "
                        "and must not state facts about it")
    for r in results:
        data = r.get("data") or {}
        if r.get("tool") == "trace_decision" and data.get("status") in NOT_ADOPTED:
            kep = data.get("kep", "")
            for sentence in re.split(r"(?<=[.!?])\s+", answer):
                if re.search(rf"\b{re.escape(kep)}\b", sentence) and _ADOPTED.search(sentence) \
                        and not _NEGATION.search(sentence):
                    problems.append(f"{kep} has status '{data['status']}' but the answer says it was adopted: "
                                    f"'{sentence.strip()[:120]}'")
    return problems


def repair(answer: str, sources: list[str]) -> str:
    """Last resort after a failed retry: remove citations and URLs that point to nothing."""
    answer = _CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(sources) else "", answer)
    for u in _urls(answer):
        if u not in sources:
            answer = answer.replace(u, "")
    return re.sub(r"[ \t]{2,}", " ", answer).strip()


def is_claim_problem(problem: str) -> bool:
    return "found=false" in problem or "says it was adopted" in problem


def fallback(results: list[dict], sources: list[str]) -> str:
    """A safe answer built only from tool data, used when the model keeps making a false claim."""
    lines = []
    for r in results:
        d = r.get("data") or {}
        refs = "".join(f"[{sources.index(u) + 1}]" for u in r.get("sources", [])[:2] if u in sources)
        if not r.get("found"):
            lines.append(f"'{d.get('query') or d.get('topic') or 'This'}' is not in the graph.")
        elif r.get("tool") == "trace_decision":
            line = f"{d.get('kep')} {d.get('title')}: status '{d.get('status')}'"
            if d.get("status") in NOT_ADOPTED:
                line += " (not adopted)"
            if d.get("replaced_by"):
                line += "; replaced by " + ", ".join(x["kep"] for x in d["replaced_by"])
            lines.append(f"{line} {refs}".strip())
    return " ".join(dict.fromkeys(lines)) or "I could not produce a reliable answer from the graph for this question."
