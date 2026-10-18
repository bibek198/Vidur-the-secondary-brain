"""Build the 50 scored evaluation questions with their expected answers -> eval/eval_questions.jsonl.

Truth comes from eval/truth.py (git files at a commit, read independently of the graph) and from the
hand-verified gold labels. KEPs are picked by fixed rules and a fixed seed, so the set is reproducible.
Categories (about 7 each): A who-knows, B why/status, C impact, D lure, E supersession, F ownership, G as-of.
Each question carries a `score` rule that eval/run_eval.py applies the same way to both modes.

Usage: python eval/build_questions.py
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import gold  # noqa: E402
from eval.truth import code_owner, commit_at, kep_at, kep_files, kep_numbers, logins, status_of  # noqa: E402

OUT = Path(__file__).resolve().parent / "eval_questions.jsonl"
CUTOFF = "2026-09-15T00:00:00Z"
SIGS = {"sig-node", "sig-scheduling", "sig-storage", "sig-auth", "sig-api-machinery", "sig-cli"}
RELEASES = {"v1.24": "2022-05-03", "v1.26": "2022-12-08", "v1.28": "2023-08-15", "v1.30": "2024-04-17",
            "v1.32": "2024-12-11", "v1.34": "2025-08-27"}


def title(d: dict) -> str:
    return str(d.get("title", "")).strip().rstrip(".")


def components(d: dict) -> list[str]:
    out = set()
    for fg in d.get("feature-gates") or []:
        if isinstance(fg, dict):
            out |= {str(c).strip() for c in fg.get("components") or [] if str(c).strip()}
    return sorted(out)


def main() -> None:
    rng = random.Random(7)
    c = commit_at("enhancements", CUTOFF)
    cc = commit_at("community", CUTOFF)
    keps = {n: kep_at(n, c) for n in kep_files(c)}
    keps = {n: d for n, d in keps.items() if d and status_of(d)}
    scoped = sorted(n for n, d in keps.items() if d.get("owning-sig") in SIGS)
    gold_rows = {int(r["kep"].split("-")[1]): r for r in gold.load()}
    qs = []

    def add(cat, question, score, **truth):
        qs.append({"id": f"{cat}{sum(1 for q in qs if q['id'][0] == cat) + 1}", "category": cat,
                   "question": question, "score": score, **truth})

    # B: why / status of a decision (truth: hand-verified gold labels)
    for n in (281, 600, 4706, 3156, 3130, 753, 2400, 1040):
        r = gold_rows[n]
        add("B", f"What was decided about KEP-{n} ({title(keps[n])}), and was it adopted?", "status",
            expected_status=r["graph_status"], truth_source="eval/gold_decisions.csv (hand-verified)")

    # D: lures. Invented features must be refused; false premises about not-adopted KEPs must be corrected
    for q in ("Who approved the quantum teleport scheduler?", "Why was blockchain-based pod billing rejected?",
              "What is the status of KEP-99999?", "Which SIG owns the holographic volume driver?"):
        add("D", q, "abstain", truth_source="invented: nothing like it exists")
    for n, q in ((4706, "When did KEP-4706 finish removing kustomize from kubectl?"),
                 (600, "Which release made Dynamic Audit Configuration (KEP-600) generally available?"),
                 (3156, "How do I turn on HTTP/3 in the apiserver now that KEP-3156 has shipped?"),
                 (2718, "When did Client Executable Proxy (KEP-2718) graduate to stable?")):
        add("D", q, "false_premise", expected_status=gold_rows[n]["graph_status"], kep=f"KEP-{n}",
            truth_source="eval/gold_decisions.csv (hand-verified)")

    # E: supersession, from `replaces:` in the newer KEP's kep.yaml at the cutoff
    pairs = sorted({(new, old) for new, d in keps.items() for old in kep_numbers(d.get("replaces")) if old in keps and old != new})
    replaced_by: dict[int, list[int]] = {}
    for new, old in pairs:                      # one question per replaced KEP; any of its replacements counts
        replaced_by.setdefault(old, []).append(new)
    for old in rng.sample(sorted(replaced_by), min(7, len(replaced_by))):
        news = sorted(replaced_by[old])
        add("E", f"Is KEP-{old} ({title(keps[old])}) still the current approach, or was it replaced?", "mentions_kep",
            expected_kep=[f"KEP-{n}" for n in news],
            truth_source=f"kep.yaml replaces: {', '.join(f'KEP-{n}' for n in news)} -> KEP-{old} (git {c[:8]})")

    # F: ownership: KEP owning-sig (4) and code paths via sigs.yaml subproject OWNERS (3)
    for n in rng.sample([n for n in scoped if n not in gold_rows], 4):
        add("F", f"Which SIG owns KEP-{n} ({title(keps[n])})?", "mentions_sig", expected_sig=[keps[n]["owning-sig"]],
            truth_source=f"kep.yaml owning-sig (git {c[:8]})")
    for path in ("pkg/kubelet", "pkg/scheduler", "pkg/kubectl"):
        add("F", f"Which SIG owns the code in {path}?", "mentions_sig", expected_sig=code_owner(path, cc),
            truth_source=f"community sigs.yaml subproject OWNERS (git {cc[:8]})")

    # G: status as of a past release (truth: kep.yaml at the first-parent commit on the release date)
    changed = []
    for n in scoped:
        for rel, day in RELEASES.items():
            then = status_of(kep_at(n, commit_at("enhancements", day + "T23:59:59Z")))
            if then and then != status_of(keps[n]):
                changed.append((n, rel, then))
    for n, rel, then in rng.sample(changed, 7):
        add("G", f"What was the status of KEP-{n} ({title(keps[n])}) at release {rel}?", "status",
            as_of=rel, expected_status=then, now_status=status_of(keps[n]),
            truth_source=f"kep.yaml at the last commit on {RELEASES[rel]}")

    # C: impact: components named in kep.yaml feature-gates
    with_comps = [n for n in scoped if len(components(keps[n])) >= 2 and n not in gold_rows]
    for n in rng.sample(with_comps, 6):
        add("C", f"Which Kubernetes components does KEP-{n} ({title(keps[n])}) affect?", "components",
            expected_components=components(keps[n]), truth_source="kep.yaml feature-gates components")

    # A: who knows: any of the KEP's authors, approvers or reviewers named
    candidates = [n for n in scoped if n not in gold_rows and status_of(keps[n]) == "implemented"
                  and len(title(keps[n]).split()) >= 3]
    for n in rng.sample(candidates, 7):
        d = keps[n]
        people = sorted(logins(d.get("authors")) | logins(d.get("approvers")) | logins(d.get("reviewers")))
        add("A", f"Who knows the most about {title(d)}?", "mentions_person", expected_people=people, kep=f"KEP-{n}",
            truth_source="kep.yaml authors, approvers and reviewers")

    OUT.write_text("".join(json.dumps(q) + "\n" for q in qs))
    print(f"{len(qs)} questions -> {OUT.relative_to(OUT.parents[1])}",
          {k: sum(1 for q in qs if q["category"] == k) for k in "ABCDEFG"}, f"({len(pairs)} supersession pairs available)")


if __name__ == "__main__":
    main()
