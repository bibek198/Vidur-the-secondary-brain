from src.brain.db import client


def test_cypher_roundtrip():
    g = client().select_graph("pytest_smoke")
    try:
        g.query("CREATE (:Person {name:'Ada'})-[:KNOWS]->(:Person {name:'Linus'})")
        rows = g.query("MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN a.name, b.name").result_set
        assert rows == [["Ada", "Linus"]]
    finally:
        g.delete()
