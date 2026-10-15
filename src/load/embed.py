"""Text chunks and their embeddings, for vector search (entity resolution and the RAG baseline).

What gets embedded (measured budget, about 3.5M tokens, ~17 min on an M4 CPU):
- KEP design docs: only decision sections (Summary, Motivation, Goals, Non-Goals, Proposal,
  User Stories, Risks, Design Details, Drawbacks, Alternatives), capped at 3,000 words per KEP,
  split into 150-word windows with 30 words of overlap. Each chunk starts with "KEP-N Title — Section:".
- Issue and review comments with at least 15 words (first 200 words).
Inline code-review comments are not embedded (mostly code nits); they stay full-text searchable.

Outputs (committed, so a fresh clone needs no recompute):
- data/seed/chunks.jsonl.gz     id, kind, text, source (KEP id or comment id), url, at_ts
- data/seed/chunk_vectors.npz   ids + float16 vectors (384-d), resumable by chunk id

Usage: python -m src.load.embed chunks | vectors | all
"""
import gzip
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from src.brain import config

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data" / "seed"
ENH = ROOT / "data" / "raw" / "repos" / "enhancements"
CHUNKS = SEED / "chunks.jsonl.gz"
VECTORS = SEED / "chunk_vectors.npz"
_KEEP = re.compile(r"^(summary|motivation|goals|non-goals|proposal|user stories|risks and mitigations|"
                   r"design details|drawbacks|alternatives)", re.I)
WINDOW, OVERLAP, KEP_CAP, COMMENT_CAP, COMMENT_MIN = 150, 30, 3000, 200, 15


def _windows(words: list[str]):
    step = WINDOW - OVERLAP
    for start in range(0, max(len(words) - OVERLAP, 1), step):
        yield " ".join(words[start:start + WINDOW])


def kep_chunks():
    for line in (SEED / "keps.jsonl").open():
        k = json.loads(line)
        readme = ENH / k["path"] / "README.md"
        if not readme.exists():
            continue
        budget, n = KEP_CAP, 0
        for section in re.split(r"(?m)^#{2,3} ", readme.read_text(errors="replace")):
            if not _KEEP.match(section) or budget <= 0:
                continue
            title, _, body = section.partition("\n")
            words = re.sub(r"<!--.*?-->", " ", body, flags=re.S).split()[:budget]
            budget -= len(words)
            for text in _windows(words):
                yield {"id": f"chunk:{k['id']}:{n}", "kind": "kep", "source": k["id"], "url": k["readme_url"],
                       "at_ts": None, "text": f"{k['id']} {k['title']} — {title.strip()}: {text}"}
                n += 1


def comment_chunks():
    with gzip.open(SEED / "threads" / "comments.jsonl.gz", "rt") as fh:
        for line in fh:
            c = json.loads(line)
            words = c["body"].split()
            if c["kind"] == "inline" or len(words) < COMMENT_MIN:
                continue
            yield {"id": f"chunk:{c['id']}", "kind": "comment", "source": c["id"], "url": c["url"],
                   "at_ts": c["at_ts"], "text": " ".join(words[:COMMENT_CAP])}


def build_chunks() -> int:
    n = 0
    with gzip.open(CHUNKS, "wt") as fh:
        for row in list(kep_chunks()) + list(comment_chunks()):
            fh.write(json.dumps(row) + "\n")
            n += 1
    print(f"{n} chunks -> {CHUNKS.relative_to(ROOT)}")
    return n


def load_vectors() -> dict[str, np.ndarray]:
    if not VECTORS.exists():
        return {}
    data = np.load(VECTORS)
    return dict(zip(data["ids"].tolist(), data["vectors"]))


def save_vectors(vectors: dict[str, np.ndarray]) -> None:
    ids = np.array(list(vectors), dtype=object)
    np.savez_compressed(VECTORS, ids=ids.astype(str), vectors=np.stack(list(vectors.values())).astype(np.float16))


def build_vectors(save_every: int = 2000) -> None:
    """Embed every chunk not yet in chunk_vectors.npz (resumable: saves progress as it goes)."""
    from fastembed import TextEmbedding

    model = TextEmbedding(config.EMBED_MODEL)
    done = load_vectors()
    todo = [json.loads(line) for line in gzip.open(CHUNKS, "rt")]
    todo = [c for c in todo if c["id"] not in done]
    print(f"{len(done)} cached, {len(todo)} to embed", flush=True)
    t0 = time.time()
    for i in range(0, len(todo), save_every):
        batch = todo[i:i + save_every]
        for c, v in zip(batch, model.embed([c["text"] for c in batch], batch_size=32)):
            done[c["id"]] = v.astype(np.float16)
        save_vectors(done)
        rate = (i + len(batch)) / (time.time() - t0)
        print(f"  {i + len(batch)}/{len(todo)}  {rate:.0f}/s  eta {(len(todo) - i - len(batch)) / rate / 60:.1f} min", flush=True)
    print(f"done: {len(done)} vectors -> {VECTORS.relative_to(ROOT)}")


def embed_query(text: str) -> list[float]:
    """Embed one query string (used by the retrieval tools and the baseline)."""
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(config.EMBED_MODEL)
    return next(iter(_MODEL.embed([text]))).tolist()


_MODEL = None

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("chunks", "all"):
        build_chunks()
    if what in ("vectors", "all"):
        build_vectors()
