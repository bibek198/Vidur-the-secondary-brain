"""Citation guard: checks an agent answer against the tool results it was built from.

Problems it catches (each one is a reason to retry once, then to repair):
- a citation [n] that points to no source
- a raw URL that no tool returned (the model may only cite graph sources)
- facts asserted although every tool said found=false (the answer must say it is not in the graph)
- a KEP that was NOT adopted (withdrawn, rejected, replaced, deferred) described as implemented or adopted
- a question as of a past date answered with a tool that only sees the current graph, without saying so
- a KEP id, @login or SIG/WG name that no tool returned and the question did not mention (an invented name)

repair() strips bad citations and URLs, and drops sentences that still name something no tool returned.
If a false claim survives (not-found facts or a not-adopted KEP
called adopted), fallback() replaces the whole answer with plain facts taken from the tool data.
"""
import json
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


_KEP_ID = re.compile(r"\bKEP[- ]?(\d{1,5})\b", re.I)
_LOGIN = re.compile(r"(?<![\w.@/])@([A-Za-z0-9][A-Za-z0-9-]{0,38})\b")
_GROUP = re.compile(r"\b((?:sig|wg)-[a-z0-9]+(?:-[a-z0-9]+)*)\b", re.I)
_INVENTED = "not in any tool result"


def names(text: str) -> set[str]:
    """KEP ids, @logins and SIG/WG names mentioned in a text, normalized (KEP-753, @login, sig-node)."""
    return ({f"KEP-{int(n)}" for n in _KEP_ID.findall(text)} | {f"@{l.lower()}" for l in _LOGIN.findall(text)}
            | {g.lower() for g in _GROUP.findall(text)})


def invented_names(answer: str, results: list[dict], question: str = "") -> list[str]:
    """Names in the answer that appear neither in any tool result nor in the question."""
    known_text = question + " " + json.dumps([{k: r.get(k) for k in ("data", "notes", "path")} for r in results],
                                             default=str, ensure_ascii=False)
    known = names(known_text) | {f"@{w.lower()}" for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", known_text)}
    return sorted(n for n in names(answer) if n not in known)


def _urls(text: str) -> list[str]:
    return [u.rstrip(".,;:") for u in _URL.findall(text)]


_CURRENT = re.compile(r"\b(current|currently|today|now|latest|present)\b", re.I)


def check(answer: str, sources: list[str], results: list[dict], as_of: str | None = None,
          question: str = "") -> list[str]:
    """List of problems; empty means the answer passes. Pure: it never changes the answer."""
    problems = []
    invented = invented_names(answer, results, question)
    if invented:
        problems.append(f"names {invented} are {_INVENTED}: mention only KEPs, people and teams the tools returned")
    if as_of and any(r.get("historical") is False for r in results) and not _CURRENT.search(answer):
        problems.append(f"a tool without a historical view was used for a question as of {as_of}: say that part "
                        "of the answer reflects the current graph, not that date")
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


def repair(answer: str, sources: list[str], results: list[dict] | None = None, question: str = "") -> str:
    """Last resort after a failed retry: remove citations and URLs that point to nothing, and every sentence or
    bullet that still names something no tool returned."""
    invented = set(invented_names(answer, results or [], question))
    if invented:
        lines = []
        for line in answer.split("\n"):
            kept = [x for x in re.split(r"(?<=[.!?])\s+", line) if not names(x) & invented]
            if kept or not line.strip():
                lines.append(" ".join(kept))
        answer = "\n".join(lines)
    answer = _CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(sources) else "", answer)
    for u in _urls(answer):
        if u not in sources:
            answer = answer.replace(u, "")
    return re.sub(r"[ \t]{2,}", " ", answer).strip() or "I could not produce a reliable answer from the graph for this question."


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
