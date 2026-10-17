"""What else changes when one live event lands (called by apply_event(..., live=True) in the worker).

1. Earlier answers that used a touched entity are flagged: (Question)-[:OUTDATED_BY]->(Event).
2. Watches on a touched entity produce a watch alert.
3. New or edited comment text is embedded, so vector search sees it immediately.
4. Decisions marked stale by the handler are queued for re-extraction (src/realtime/reextract.py).
5. Freshness is recorded in Meta {key:'freshness'}: last event, its lag (observed -> applied).
6. A notice goes to the UI over pub/sub (src/realtime/stream.py notify -> SSE).
Bulk history loading does not run any of this (live=False).
"""
import time

from src.realtime.events import iso_to_seconds, iso_to_ts, now_iso


def after_event(g, ev: dict, summary: str, touched: list[tuple]) -> dict:
    from src.realtime import reextract, stream
    graph_name = g.name
    eid = ev["event_id"]
    outdated = [r[0] for r in g.query(
        """MATCH (e:Event {event_id:$id})-[:TOUCHED]->(x)<-[:ASKED_ABOUT]-(q:Question)
           MERGE (q)-[:OUTDATED_BY]->(e) RETURN DISTINCT q.id""", {"id": eid}).result_set]
    watches = [r[0] for r in g.query(
        "MATCH (e:Event {event_id:$id})-[:TOUCHED]->(x)<-[:WATCHES]-(w:Watch) RETURN DISTINCT w.id",
        {"id": eid}).result_set]

    if ev["kind"] in ("comment_added", "comment_edited"):
        _embed_comment(g, ev)
    keps = [v for label, _, v in touched if label == "KEP"]
    stale = [r[0] for r in g.query("MATCH (d:Decision)-[:DECIDED]->(k:KEP) WHERE k.id IN $keps AND d.stale = true "
                                   "RETURN k.id", {"keps": keps}).result_set]
    # only fresh discussion triggers an automatic LLM call; a catch-up of old events leaves the rationale
    # stale (the agent says so) and `python -m src.realtime.reextract --stale` handles that backlog
    if time.time() - iso_to_seconds(ev["occurred_at"]) <= reextract.MAX_AGE_S:
        for kep in stale:
            reextract.enqueue(graph_name, kep)

    lag_ms = int((time.time() - iso_to_seconds(ev["observed_at"])) * 1000) if ev.get("observed_at") else None
    g.query("""MERGE (m:Meta {key:'freshness'})
               SET m.last_event_at = $occurred, m.last_observed_at = $observed, m.last_applied_at = $now,
                   m.last_kind = $kind, m.lag_ms = $lag, m.events = coalesce(m.events, 0) + 1""",
            {"occurred": ev["occurred_at"], "observed": ev.get("observed_at"), "now": now_iso(),
             "kind": ev["kind"], "lag": lag_ms})
    notice = {"type": "event", "event_id": eid, "kind": ev["kind"], "entity": ev["entity_key"], "summary": summary,
              "url": ev["payload"].get("url"), "source": ev["source"], "occurred_at": ev["occurred_at"],
              "observed_at": ev.get("observed_at"), "lag_ms": lag_ms, "touched": [v for _, _, v in touched],
              "outdated_questions": outdated, "watches": watches, "stale_decisions": stale}
    stream.notify(graph_name, notice)
    return notice


def _embed_comment(g, ev: dict) -> None:
    """Same chunk rules as src/load/embed.py: issue/review comments of 15+ words, first 200 words."""
    from src.load import upserts
    from src.load.embed import COMMENT_CAP, COMMENT_MIN, embed_query
    p = ev["payload"]
    words = (p.get("body") or "").split()
    if p.get("kind") == "inline" or len(words) < COMMENT_MIN:
        return
    text = " ".join(words[:COMMENT_CAP])
    upserts.chunks(g, [{"id": f"chunk:{ev['entity_key']}", "kind": "comment", "source": ev["entity_key"],
                        "url": p.get("url"), "at_ts": p.get("at_ts") or iso_to_ts(ev["occurred_at"]), "text": text,
                        "embedding": embed_query(text)}])
