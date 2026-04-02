#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_json(path: str) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: mem0_50q_score.py <qa-50.json> <expected-50.json>", file=sys.stderr)
        return 2

    qa = load_json(sys.argv[1])
    exp = load_json(sys.argv[2])

    total = min(len(qa), len(exp))
    passed = 0
    failures: list[dict] = []
    for i in range(total):
        qrow = qa[i]
        erow = exp[i]
        kind = erow.get("kind", qrow.get("kind", ""))
        extracted = (qrow.get("extracted") or "").lower()
        must = [m.lower() for m in erow.get("must_contain", [])]
        must_any = [m.lower() for m in erow.get("must_contain_any", [])]
        if kind == "orch_to_branch":
            ok = bool(extracted) and extracted in must
        else:
            targets = must_any if must_any else must
            ok = bool(extracted) and extracted in targets
        if ok:
            passed += 1
            continue
        failures.append(
            {
                "n": qrow.get("n", i + 1),
                "question": erow.get("question"),
                "must_contain": erow.get("must_contain", []),
                "must_contain_any": erow.get("must_contain_any", []),
                "extracted": qrow.get("extracted", ""),
                "answer_head": (qrow.get("answer") or "")[:400],
            }
        )

    out_dir = Path("/tmp/openclaw-mem0-fastpath/latest-50q")
    score = {
        "passed": passed,
        "total": total,
        "pass_rate": (passed / total) if total else 0.0,
        "failed": total - passed,
    }
    (out_dir / "score.json").write_text(json.dumps(score, indent=2), encoding="utf-8")
    (out_dir / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
    print(json.dumps(score))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
