"""FalkorDB connection helpers. Every query goes through run() so it can be traced."""
from falkordb import FalkorDB

from src.brain import config

_db = None


def client() -> FalkorDB:
    global _db
    if _db is None:
        _db = FalkorDB(host=config.FALKORDB_HOST, port=config.FALKORDB_PORT,
                       password=config.FALKORDB_PASSWORD)
    return _db


def graph(name: str | None = None):
    return client().select_graph(name or config.GRAPH_NAME)
