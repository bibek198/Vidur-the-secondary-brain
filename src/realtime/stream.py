"""The event log: a Redis Stream inside FalkorDB's own Redis (no extra service).

- publish(): every source (poller, git diff, webhook, inject) appends Event JSON to brain:events:<graph>.
- consume(): the worker reads with a consumer group and acknowledges an event only after its handler
  succeeded, so a crash means redelivery (at-least-once). Handlers are idempotent, so redelivery is safe.
- An event that fails 3 times goes to the dead-letter stream brain:dead:<graph> and is acknowledged.
- notify(): small JSON notices on the pub/sub channel brain:notify:<graph>; the web app streams them to
  the UI over SSE (live feed, "answer outdated", watch alerts).
With AOF on (docker-compose.yml), the stream survives a FalkorDB restart.
"""
import json

import redis

from src.brain import config

GROUP = "brain-workers"
MAX_LEN = 100_000
MAX_TRIES = 3


def r() -> redis.Redis:
    return redis.Redis(host=config.FALKORDB_HOST, port=config.FALKORDB_PORT, password=config.FALKORDB_PASSWORD,
                       decode_responses=True)


def stream_key(graph: str) -> str:
    return f"brain:events:{graph}"


def dead_key(graph: str) -> str:
    return f"brain:dead:{graph}"


def notify_channel(graph: str) -> str:
    return f"brain:notify:{graph}"


def publish(event, graph: str) -> str:
    """Append one Event (or event dict) to the graph's stream; returns the stream entry id."""
    data = event.to_json() if hasattr(event, "to_json") else json.dumps(event, sort_keys=True, default=str)
    return r().xadd(stream_key(graph), {"event": data}, maxlen=MAX_LEN, approximate=True)


def ensure_group(graph: str, conn: redis.Redis | None = None) -> None:
    conn = conn or r()
    try:
        conn.xgroup_create(stream_key(graph), GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):   # the group already exists: fine
            raise


def consume(graph: str, handler, consumer: str = "worker-1", block_ms: int = 1000, count: int = 100) -> int:
    """Handle pending (unacked, e.g. after a crash) then new events once. Returns how many were handled."""
    conn = r()
    ensure_group(graph, conn)
    handled = 0
    for start in ("0", ">"):            # "0" = my pending entries first, ">" = new entries
        resp = conn.xreadgroup(GROUP, consumer, {stream_key(graph): start}, count=count,
                               block=None if start == "0" else block_ms)
        for _, entries in resp or []:
            for entry_id, fields in entries:
                if "event" not in fields:   # pending entry already deleted by trimming
                    conn.xack(stream_key(graph), GROUP, entry_id)
                    continue
                try:
                    handler(json.loads(fields["event"]))
                    conn.xack(stream_key(graph), GROUP, entry_id)
                    conn.hdel(f"brain:tries:{graph}", entry_id)
                    handled += 1
                except Exception as exc:  # noqa: BLE001 - one bad event must not stop the worker
                    tries = conn.hincrby(f"brain:tries:{graph}", entry_id, 1)
                    if tries >= MAX_TRIES:
                        conn.xadd(dead_key(graph), {"event": fields["event"], "error": str(exc)[:500]})
                        conn.xack(stream_key(graph), GROUP, entry_id)
                        conn.hdel(f"brain:tries:{graph}", entry_id)
    return handled


def notify(graph: str, message: dict) -> None:
    r().publish(notify_channel(graph), json.dumps(message, default=str))


def stats(graph: str) -> dict:
    conn = r()
    out = {"stream_length": conn.xlen(stream_key(graph)) if conn.exists(stream_key(graph)) else 0,
           "dead_letters": conn.xlen(dead_key(graph)) if conn.exists(dead_key(graph)) else 0, "pending": 0}
    try:
        out["pending"] = conn.xpending(stream_key(graph), GROUP)["pending"]
    except redis.ResponseError:
        pass
    return out


def reset(graph: str) -> None:
    """Delete a graph's stream, dead letters and retry counters (tests and demo reset)."""
    r().delete(stream_key(graph), dead_key(graph), f"brain:tries:{graph}", f"brain:reextract:{graph}")
