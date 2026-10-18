"""Record vector-only baseline answers for the example questions (eval/questions.jsonl) on brain_eval,
so replay mode (no API key) can show the graph-vs-vector comparison. Usage: python eval/record_baseline.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent import replay  # noqa: E402
from src.baseline import answer_baseline  # noqa: E402

QUESTIONS = Path(__file__).resolve().parent / "questions.jsonl"

if __name__ == "__main__":
    total = 0.0
    for q in map(json.loads, QUESTIONS.open()):
        out = answer_baseline(q["question"], "brain_eval")
        replay.record(out)
        total += out["cost_usd"]
        print(f"{q['id']:3} {len(out['evidence'])} passages, {len(out['citations'])} cited, ${out['cost_usd']:.4f}")
    print(f"recorded {sum(1 for _ in QUESTIONS.open())} baseline answers, ${total:.3f}")
