"""Run the rationale extraction with Claude Haiku: a direct pilot or the Batch API (50% cheaper).

Each KEP's raw model output is cached in data/seed/extractions/KEP-N.json together with the
hash of the exact input, so nothing is paid for twice and a changed input is re-extracted.
src/extract/validate.py then checks every quote and writes data/seed/decisions.jsonl.

Usage:
  python -m src.extract.run pilot KEP-3063 KEP-753 ...   # direct calls, a few KEPs
  python -m src.extract.run submit [--limit N]           # one batch with every KEP not yet cached
  python -m src.extract.run collect                      # check the batch; save results when it has ended
  python -m src.extract.run cost                         # tokens and USD spent so far (from the cache)
"""
import json
import sys
import time
from pathlib import Path

import anthropic

from src.brain import config
from src.brain.db import graph
from src.extract.prompt import build_input, cutoff_commit, request_params

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "seed" / "extractions"
BATCH_FILE = CACHE / "_batch.json"
# USD per million tokens (input, output) for Haiku 5.5; the Batch API halves both
PRICE = {"direct": (0.10, 0.50), "batch": (0.05, 0.25)}


def cache_path(kid: str) -> Path:
    return CACHE / f"{kid}.json"


def cached(kid: str) -> dict | None:
    p = cache_path(kid)
    return json.loads(p.read_text()) if p.exists() else None


def save(inp: dict, message, mode: str) -> dict:
    tool_use = next((b for b in message.content if b.type == "tool_use"), None)
    row = {"kep": inp["kep"], "input_hash": inp["input_hash"], "model": message.model, "mode": mode,
           "stop_reason": message.stop_reason, "output": tool_use.input if tool_use else None,
           "usage": {"input": message.usage.input_tokens, "output": message.usage.output_tokens},
           "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path(inp["kep"]).write_text(json.dumps(row, indent=1, ensure_ascii=False))
    return row


def _commit() -> str:
    cutoff = graph("brain_eval").query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff").result_set[0][0]
    return cutoff_commit(cutoff)


def pending_inputs(keps: list[str] | None = None) -> list[dict]:
    """Inputs for the KEPs that have no cached output for this exact input."""
    g, commit = graph("brain_eval"), _commit()
    if keps is None:
        keps = [r[0] for r in g.query("MATCH (k:KEP) WHERE k.status IS NOT NULL RETURN k.id ORDER BY k.number").result_set]
    out = []
    for kid in keps:
        inp = build_input(kid, commit=commit)
        c = cached(kid)
        if inp and not (c and c["input_hash"] == inp["input_hash"] and c["output"]):
            out.append(inp)
    return out


def pilot(keps: list[str]) -> None:
    client = anthropic.Anthropic()
    for inp in pending_inputs(keps):
        msg = client.messages.create(**request_params(inp, config.LLM_MODEL))
        row = save(inp, msg, "direct")
        print(f"{inp['kep']}: {row['usage']} stop={row['stop_reason']}")


def submit(limit: int | None) -> None:
    if BATCH_FILE.exists() and json.loads(BATCH_FILE.read_text()).get("status") != "collected":
        sys.exit(f"a batch is already open: {BATCH_FILE.read_text()}  (run collect first)")
    inputs = pending_inputs()[:limit]
    if not inputs:
        sys.exit("nothing to extract: every KEP has a cached output for its current input")
    words = sum(len(i["text"].split()) for i in inputs)
    print(f"submitting {len(inputs)} KEPs, ~{words * 1.4 / 1e6:.2f}M input tokens")
    batch = anthropic.Anthropic().messages.batches.create(requests=[
        {"custom_id": i["kep"], "params": request_params(i, config.LLM_MODEL)} for i in inputs])
    BATCH_FILE.write_text(json.dumps({"id": batch.id, "keps": len(inputs), "status": "submitted",
                                      "input_hashes": {i["kep"]: i["input_hash"] for i in inputs}}, indent=1))
    print(f"batch {batch.id} submitted; run `python -m src.extract.run collect` later")


def collect() -> None:
    info = json.loads(BATCH_FILE.read_text())
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(info["id"])
    print(f"batch {batch.id}: {batch.processing_status} {batch.request_counts}")
    if batch.processing_status != "ended":
        return
    ok = failed = 0
    for r in client.messages.batches.results(info["id"]):
        if r.result.type == "succeeded":
            inp = {"kep": r.custom_id, "input_hash": info["input_hashes"][r.custom_id]}
            save(inp, r.result.message, "batch")
            ok += 1
        else:
            failed += 1
            print(f"  {r.custom_id}: {r.result.type}")
    info["status"] = "collected"
    info["result"] = {"succeeded": ok, "failed": failed}
    BATCH_FILE.write_text(json.dumps(info, indent=1))
    print(f"saved {ok} results, {failed} failed (failed ones are retried by the next submit)")


def cost() -> None:
    total, tokens = 0.0, [0, 0]
    for p in CACHE.glob("KEP-*.json"):
        row = json.loads(p.read_text())
        pin, pout = PRICE[row["mode"]]
        tokens[0] += row["usage"]["input"]
        tokens[1] += row["usage"]["output"]
        total += (row["usage"]["input"] * pin + row["usage"]["output"] * pout) / 1e6
    print(f"{len(list(CACHE.glob('KEP-*.json')))} KEPs, {tokens[0]:,} input + {tokens[1]:,} output tokens, ${total:.3f}")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "pilot":
        pilot(args)
    elif cmd == "submit":
        submit(int(args[args.index("--limit") + 1]) if "--limit" in args else None)
    elif cmd == "collect":
        collect()
    elif cmd == "cost":
        cost()
    else:
        sys.exit(__doc__)
