"""Extraction checks that need no LLM: the quote validator and the Decision layer in trace_decision."""
import pytest

from src.brain.db import client
from src.extract.validate import norm, quote_ok, validate_one
from src.tools.base import reset_trace
from src.tools.registry import call_tool

SOURCES = {"readme": "In Kubernetes 1.32, this KEP has been **withdrawn** and all code\nrelated to it gets removed.",
           "comment:1": "> My concern is that   there has not been any formal communication."}


def test_quote_check_tolerates_markdown_and_whitespace():
    assert quote_ok("this KEP has been withdrawn and all code related to it", SOURCES, "readme")
    assert quote_ok("my concern is that there has not been any formal", SOURCES, "comment:1")
    assert norm("A  **b**\n`c`") == "a b c"


def test_quote_check_rejects_paraphrase_wrong_ref_and_short_quotes():
    assert not quote_ok("this KEP was withdrawn and its code was removed", SOURCES, "readme")   # paraphrase
    assert not quote_ok("this KEP has been withdrawn and all code", SOURCES, "comment:1")       # wrong source
    assert not quote_ok("this KEP has been withdrawn and all code", SOURCES, "comment:999")     # unknown ref
    assert not quote_ok("been withdrawn", SOURCES, "readme")                                    # too short


def test_validate_drops_failed_quotes_and_bad_claims():
    row = {"kep": "KEP-1", "model": "m", "input_hash": "h", "extracted_at": "t", "output": {
        "summary": "s", "rationale": "r", "outcome_reason": None, "alternatives": [], "confidence": "high",
        "evidence": [{"ref": "readme", "quote": "this KEP has been withdrawn and all code"},
                     {"ref": "readme", "quote": "a sentence the model made up about this KEP"}],
        "supersession_claims": [{"kep": "KEP-2", "relation": "replaced_by", "ref": "readme",
                                 "quote": "this KEP has been withdrawn and all code"},
                                {"kep": "KEP-404", "relation": "replaced_by", "ref": "readme",
                                 "quote": "this KEP has been withdrawn and all code"}]}}
    d = validate_one(row, {"status": "withdrawn", "sources": SOURCES}, known_keps={"KEP-1", "KEP-2"})
    assert d["evidence_passed"] == 1 and d["evidence_total"] == 2 and d["verified"] is False
    assert d["outcome"] == "withdrawn"                                   # outcome comes from the graph, not the LLM
    assert [c["kep"] for c in d["supersession_claims"]] == ["KEP-2"]    # unknown KEP-404 dropped


@pytest.mark.skipif("brain_eval" not in client().list_graphs(), reason="brain_eval not loaded")
def test_trace_decision_returns_checked_rationale():
    reset_trace()
    r = call_tool("trace_decision", {"kep_or_query": "KEP-3063"}, graph_name="brain_eval")
    if r["data"]["rationale"] is None:
        pytest.skip("no Decision for KEP-3063 loaded yet")
    assert r["data"]["status"] == "withdrawn"
    assert r["data"]["rationale"]["quotes"] and all(q["url"].startswith("http") for q in r["data"]["rationale"]["quotes"])
    assert any("checked word for word" in n for n in r["notes"])
    assert not any(n.startswith("Matched by") for n in r["notes"])      # an exact KEP id needs no match warning


@pytest.mark.skipif("brain_eval" not in client().list_graphs(), reason="brain_eval not loaded")
def test_llm_supersessions_never_feed_back_into_the_input():
    from src.brain.db import graph
    from src.extract.prompt import build_input
    g = graph("brain_eval")
    llm = g.query("MATCH (a:KEP)-[s:SUPERSEDES]->(b:KEP) WHERE s.source = 'llm' RETURN a.id, b.id LIMIT 1").result_set
    if not llm:
        pytest.skip("no LLM-proposed supersession loaded")
    newer, older = llm[0]
    assert f"is replaced by {newer}" not in build_input(older)["text"]
