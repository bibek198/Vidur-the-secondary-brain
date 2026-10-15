"""Phase 0 smoke test: basic Cypher round trip and a vector index query on a throwaway graph."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.brain.db import client  # noqa: E402

g = client().select_graph("smoke_test")
try:
    g.query("CREATE (:Person {name:$a})-[:KNOWS]->(:Person {name:$b})", {"a": "Ada", "b": "Linus"})
    print(g.query("MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN a.name, b.name").result_set)

    g.query("CREATE VECTOR INDEX FOR (d:Doc) ON (d.emb) OPTIONS {dimension:3, similarityFunction:'cosine'}")
    for name, vec in [("north", [0.0, 1.0, 0.0]), ("east", [1.0, 0.0, 0.0]), ("north-east", [0.7, 0.7, 0.0])]:
        g.query("CREATE (:Doc {name:$n, emb: vecf32($v)})", {"n": name, "v": vec})
    rows = g.query(
        "CALL db.idx.vector.queryNodes('Doc', 'emb', 2, vecf32($q)) YIELD node, score "
        "RETURN node.name, score ORDER BY score",
        {"q": [0.1, 0.9, 0.0]},
    ).result_set
    print("vector:", rows)
finally:
    g.delete()
