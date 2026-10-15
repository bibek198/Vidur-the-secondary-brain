"""Change events: the one format every source produces and every handler consumes.

The git-history backfill, the GitHub poller, the git diff job and the sandbox
webhook all emit these. event_id is deterministic, so the same change seen twice
(a replay, or a poller and a webhook overlapping) has the same id and is a no-op.
"""
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Event:
    source: str          # git | github | webhook | inject
    kind: str            # kep_created, kep_status_changed, sig_lead_added, comment_added, ...
    entity_key: str      # e.g. "KEP-753", "sig-node", "comment:123456"
    occurred_at: str     # ISO 8601 UTC: when it happened in the company
    payload: dict = field(default_factory=dict)
    observed_at: str = ""  # ISO 8601 UTC: when the brain learned about it
    event_id: str = ""

    def __post_init__(self):
        if not self.observed_at:
            self.observed_at = now_iso()
        if not self.event_id:
            self.event_id = make_event_id(self.source, self.kind, self.entity_key,
                                          self.occurred_at, self.payload)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(**d)


def make_event_id(source: str, kind: str, entity_key: str, occurred_at: str, payload: dict) -> str:
    raw = json.dumps([source, kind, entity_key, occurred_at, payload], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc_iso(value: str) -> str:
    """Normalize any ISO timestamp (with offset or Z) to UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_ts(value: str) -> int:
    """ISO timestamp to epoch seconds (for fast range filters in Cypher)."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
