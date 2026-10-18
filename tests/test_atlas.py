"""The Atlas: the whole-graph skeleton for the UI (src/atlas.py, GET /api/atlas). Read-only on brain_eval."""
import pytest
from fastapi.testclient import TestClient

from src.atlas import MAX_PEOPLE, build_atlas
from src.brain.db import client, graph

needs_graph = pytest.mark.skipif("brain_eval" not in client().list_graphs(), reason="brain_eval not loaded")


@pytest.fixture(scope="module")
def atlas():
    return build_atlas(graph("brain_eval"))


@needs_graph
def test_atlas_is_the_skeleton_only(atlas):
    types = {n["type"] for n in atlas["nodes"]}
    assert types == {"Team", "KEP", "CodeArea", "Person"}
    assert len(atlas["nodes"]) <= 2500
    assert sum(n["type"] == "Person" for n in atlas["nodes"]) <= MAX_PEOPLE
    ids = [n["id"] for n in atlas["nodes"]]
    assert len(ids) == len(set(ids))


@needs_graph
def test_atlas_links_and_known_nodes(atlas):
    ids = {n["id"] for n in atlas["nodes"]}
    assert all(l["source"] in ids and l["target"] in ids for l in atlas["links"])
    assert {"sig-node", "KEP-753"} <= ids
    by_id = {n["id"]: n for n in atlas["nodes"]}
    assert by_id["KEP-753"]["team"] == "sig-node"
    assert by_id["sig-node"]["type"] == "Team"   # an account named "sig-node" must not replace the team
    assert all(l["source"] != l["target"] for l in atlas["links"])
    assert ("sig-node", "KEP-753", "OWNS") in {(l["source"], l["target"], l["type"]) for l in atlas["links"]}


@needs_graph
def test_atlas_history_is_sorted_and_matches_keps(atlas):
    keps = {n["id"] for n in atlas["nodes"] if n["type"] == "KEP"}
    assert atlas["history"] and set(atlas["history"]) <= keps
    for steps in atlas["history"].values():
        assert [s[0] for s in steps] == sorted(s[0] for s in steps)
    assert [r[1] for r in atlas["releases"]] == sorted(r[1] for r in atlas["releases"])


@needs_graph
def test_atlas_endpoint_is_cached(monkeypatch):
    from web import app as web
    web._atlas_cache.clear()
    calls = []
    real = build_atlas
    monkeypatch.setattr("src.atlas.build_atlas", lambda g: calls.append(1) or real(g))
    tc = TestClient(web.app)
    first = tc.get("/api/atlas", params={"graph_name": "brain_eval"})
    second = tc.get("/api/atlas", params={"graph_name": "brain_eval"})
    assert first.status_code == second.status_code == 200
    assert first.json()["counts"] == second.json()["counts"]
    assert len(calls) == 1
    assert tc.get("/api/atlas", params={"graph_name": "nope"}).status_code == 400


def test_page_has_the_atlas_view():
    from web import app as web
    tc = TestClient(web.app)
    page = tc.get("/").text
    assert 'id="viewpick"' in page and 'id="atlas"' in page and "/static/atlas.js" in page
    assert tc.get("/static/atlas.js").status_code == 200
