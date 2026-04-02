#!/usr/bin/env bash
set -euo pipefail

MODE="run"
BATCH_START="${OPENCLAW_50Q_START:-1}"
BATCH_COUNT="${OPENCLAW_50Q_COUNT:-0}"
BATCH_END="${OPENCLAW_50Q_END:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-run}"
      shift 2
      ;;
    --start)
      BATCH_START="${2:-}"
      shift 2
      ;;
    --count)
      BATCH_COUNT="${2:-}"
      shift 2
      ;;
    --end)
      BATCH_END="${2:-}"
      shift 2
      ;;
    dry-run)
      MODE="dry-run"
      shift
      ;;
    run)
      MODE="run"
      shift
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

for var_name in BATCH_START BATCH_COUNT BATCH_END; do
  value="${!var_name}"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$var_name must be a non-negative integer, got '$value'" >&2
    exit 2
  fi
done
if (( BATCH_START < 1 )); then
  echo "BATCH_START must be >= 1" >&2
  exit 2
fi
if (( BATCH_COUNT > 0 && BATCH_END > 0 )); then
  echo "use either --count or --end, not both" >&2
  exit 2
fi
if (( BATCH_END > 0 && BATCH_END < BATCH_START )); then
  echo "BATCH_END must be >= BATCH_START" >&2
  exit 2
fi
if (( BATCH_COUNT > 0 )); then
  BATCH_END=$((BATCH_START + BATCH_COUNT - 1))
elif (( BATCH_END == 0 )); then
  BATCH_END=50
fi
if (( BATCH_END > 50 )); then
  BATCH_END=50
fi
if (( BATCH_END < BATCH_START )); then
  echo "invalid batch range: ${BATCH_START}-${BATCH_END}" >&2
  exit 2
fi

ROOT="/tmp/openclaw-mem0-fastpath"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ROOT/$STAMP-50q-agent"
mkdir -p "$OUT"
LATEST_DIR="$ROOT/latest-50q"
ln -sfn "$OUT" "$LATEST_DIR"

BATCH_LABEL="$(printf "q%03d-%03d" "$BATCH_START" "$BATCH_END")"
BATCH_DIR="$ROOT/batches/$BATCH_LABEL"
mkdir -p "$BATCH_DIR"

export OPENCLAW_50Q_OUT_DIR="$LATEST_DIR"
export OPENCLAW_50Q_BATCH_DIR="$BATCH_DIR"
export OPENCLAW_50Q_BATCH_LABEL="$BATCH_LABEL"
export OPENCLAW_50Q_BATCH_START="$BATCH_START"
export OPENCLAW_50Q_BATCH_END="$BATCH_END"

if [[ "$MODE" == "dry-run" ]]; then
  cat > "$OUT/dry-run.json" <<JSON
{"ok":true,"mode":"dry-run","batch_label":"$BATCH_LABEL","batch_start":$BATCH_START,"batch_end":$BATCH_END,"batch_dir":"$BATCH_DIR","latest_dir":"$LATEST_DIR"}
JSON
  cp "$OUT/dry-run.json" "$BATCH_DIR/dry-run.json"
  echo "$OUT"
  exit 0
fi

# Build canonical expected prompts from slack-history memory export.
python3 -u <<'PY'
import json
import os
import pathlib
import re
from collections import defaultdict

base = pathlib.Path.home() / '.smartclaw' / 'memory' / 'slack-history'
mds = sorted(base.glob('*.md'))
text = '\n'.join(p.read_text(errors='ignore') for p in mds)

pairs = []
def normalize_orch(raw: str) -> str:
    token = 'ORCH-' + raw.split('-', 1)[1]
    return token

def is_canonical_orch(token: str) -> bool:
    # Keep the canary corpus constrained to stable ORCH families we can score reliably.
    return bool(
        re.fullmatch(r'ORCH-e2e-[0-9a-f]{6,}', token, flags=re.I)
        or re.fullmatch(r'ORCH-self-hosted-runner-\d+', token, flags=re.I)
        # Allow other multi-segment ORCH tokens, but reject short malformed forms
        # like ORCH-2 / ORCH-ssa.
        or re.fullmatch(r'ORCH-[A-Za-z][A-Za-z0-9]+-[A-Za-z0-9][A-Za-z0-9\-]{2,}', token, flags=re.I)
    )

for m in re.finditer(r'(ORCH-[A-Za-z0-9\-]+).*?`(ai-orch-\d+)`', text, flags=re.I|re.S):
    orch = normalize_orch(m.group(1))
    if not is_canonical_orch(orch):
        continue
    pairs.append((orch, m.group(2)))

# stable de-dupe
seen = set()
dedup = []
for p in pairs:
    if p in seen:
        continue
    seen.add(p)
    dedup.append(p)

# Canonical seeds that should always be present
canonical = [
    ('ORCH-e2e-029c50', 'ai-orch-56066'),
    ('ORCH-e2e-2cfd73', 'ai-orch-55438'),
    ('ORCH-self-hosted-runner-001', 'ai-orch-92020'),
]
for p in canonical:
    if p not in seen:
        dedup.append(p)
        seen.add(p)

branch_to_orch = defaultdict(set)
orch_to_branches = defaultdict(set)
for orch, branch in dedup:
    orch_to_branches[orch].add(branch)
    branch_to_orch[branch].add(orch)

# Keep only strict one-to-one mappings to avoid ambiguous scoring.
strict_pairs = []
for orch, branches in orch_to_branches.items():
    if len(branches) != 1:
        continue
    branch = next(iter(branches))
    if len(branch_to_orch[branch]) != 1:
        continue
    strict_pairs.append((orch, branch))
strict_pairs = sorted(strict_pairs, key=lambda x: x[0])

# Build 50 questions, mixing forward and reverse lookups.
expected_all = []

# 25 forward lookups (exact ORCH -> branch)
for orch, branch in strict_pairs[:25]:
    expected_all.append({
        'kind': 'orch_to_branch',
        'question': f'Which branch was {orch} committed to?',
        'must_contain': [branch],
        'must_contain_any': [],
    })

# 25 reverse lookups (branch -> ORCH) from strict one-to-one mappings.
for orch, branch in strict_pairs[25:50]:
    expected_all.append({
        'kind': 'branch_to_orch',
        'question': f'Find the ORCH token associated with branch {branch}.',
        'must_contain': [orch],
        'must_contain_any': [orch],
    })

# If the filtered corpus is slightly short, pad deterministically with valid forward lookups.
if len(expected_all) < 50:
    pad_idx = 0
    while len(expected_all) < 50 and strict_pairs:
        orch, branch = strict_pairs[pad_idx % len(strict_pairs)]
        expected_all.append({
            'kind': 'orch_to_branch',
            'question': f'Which branch was {orch} committed to?',
            'must_contain': [branch],
            'must_contain_any': [],
        })
        pad_idx += 1

expected_all = expected_all[:50]
if len(expected_all) != 50:
    raise SystemExit(f'expected exactly 50 prompts, got {len(expected_all)}')

batch_start = int(os.environ.get('OPENCLAW_50Q_BATCH_START', '1'))
batch_end_requested = int(os.environ.get('OPENCLAW_50Q_BATCH_END', '50'))
if batch_start < 1 or batch_start > len(expected_all):
    raise SystemExit(f'batch start out of range: {batch_start}')
batch_end = min(max(batch_end_requested, batch_start), len(expected_all))
expected = expected_all[batch_start - 1:batch_end]

out_dir = pathlib.Path(os.environ.get('OPENCLAW_50Q_OUT_DIR', '/tmp/openclaw-mem0-fastpath/latest-50q'))
batch_dir = pathlib.Path(os.environ.get('OPENCLAW_50Q_BATCH_DIR', str(out_dir)))
out_dir.mkdir(parents=True, exist_ok=True)
batch_dir.mkdir(parents=True, exist_ok=True)
batch_meta = {
    'batch_label': os.environ.get('OPENCLAW_50Q_BATCH_LABEL', ''),
    'batch_start': batch_start,
    'batch_end': batch_end,
    'selected_total': len(expected),
    'full_total': len(expected_all),
    'latest_dir': str(out_dir),
    'batch_dir': str(batch_dir),
}
(out_dir / 'expected-50.json').write_text(json.dumps(expected, indent=2))
(out_dir / 'pair-map.json').write_text(json.dumps({k: sorted(v) for k, v in branch_to_orch.items()}, indent=2))
(out_dir / 'batch-meta.json').write_text(json.dumps(batch_meta, indent=2))
(batch_dir / 'expected-50.json').write_text(json.dumps(expected, indent=2))
(batch_dir / 'pair-map.json').write_text(json.dumps({k: sorted(v) for k, v in branch_to_orch.items()}, indent=2))
(batch_dir / 'batch-meta.json').write_text(json.dumps(batch_meta, indent=2))

print(f'wrote {len(expected)} expected queries [{batch_start}-{batch_end}] -> {out_dir / "expected-50.json"}')
PY

# Refresh memory index before running agent QA.
if ! openclaw memory index --force > "$OUT/reindex.log" 2>&1; then
  echo "memory index failed; see $OUT/reindex.log" >&2
  exit 1
fi

# Ask openclaw agent directly for each question and score extracted identifiers.
python3 - <<'PY'
import json
import os
import pathlib
import re
import subprocess
import uuid

out_dir = pathlib.Path(os.environ.get('OPENCLAW_50Q_OUT_DIR', '/tmp/openclaw-mem0-fastpath/latest-50q'))
batch_dir = pathlib.Path(os.environ.get('OPENCLAW_50Q_BATCH_DIR', str(out_dir)))
batch_dir.mkdir(parents=True, exist_ok=True)
expected = json.loads((out_dir / 'expected-50.json').read_text())
agent_id = os.environ.get('OPENCLAW_50Q_AGENT', 'memqa')

rows = []
passed = 0

def extract_identifier(kind: str, text: str) -> str:
    if kind == 'orch_to_branch':
        m = re.search(r'ai-orch-\d+', text, flags=re.I)
        return (m.group(0).lower() if m else '')
    m = re.search(r'ORCH-[A-Za-z0-9\-]+', text, flags=re.I)
    if not m:
        return ''
    token = m.group(0)
    return 'ORCH-' + token.split('-', 1)[1]

for i, e in enumerate(expected, 1):
    q = e['question']
    kind = e['kind']
    if kind == 'orch_to_branch':
        prompt = (
            'Memory-only recall test.\\n'
            'Reply with exactly one token in format ai-orch-<digits>.\\n'
            'If unknown, reply I_DONT_KNOW.\\n'
            f'Question: {q}'
        )
    else:
        prompt = (
            'Memory-only recall test.\\n'
            'Reply with exactly one token in format ORCH-<token>.\\n'
            'If unknown, reply I_DONT_KNOW.\\n'
            f'Question: {q}'
        )

    attempts = []
    extracted = ''
    raw_answer = ''
    for attempt in range(1, 4):
        # Hard reset context for every question attempt.
        session_id = f"mem0-qa-q{i:02d}-a{attempt}-{uuid.uuid4().hex[:12]}"
        try:
            p = subprocess.run(
                ['openclaw', '--log-level', 'fatal', 'agent', '--local', '--agent', agent_id, '--session-id', session_id, '--timeout', '120', '--json', '--thinking', 'off', '-m', prompt],
                capture_output=True,
                text=True,
                env=os.environ.copy(),
                timeout=140,
            )
            raw = (p.stdout or '').strip()
            stderr = (p.stderr or '').strip()
            answer = ''
            try:
                payload = json.loads(raw)
                payloads = payload.get('payloads') or payload.get('result', {}).get('payloads', [])
                if payloads:
                    answer = str(payloads[0].get('text') or '')
            except Exception:
                answer = raw
            if stderr:
                answer = (answer + '\n' + stderr).strip()
            err_l = (stderr or "").lower()
            if p.returncode != 0 and (
                "session file locked" in err_l
                or "rate limit" in err_l
                or "timed out" in err_l
                or "all models failed" in err_l
            ):
                # Runtime/transient failure: repair session metadata and retry.
                subprocess.run(
                    ['openclaw', 'sessions', 'cleanup'],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                attempts.append({
                    'attempt': attempt,
                    'answer': answer[:2000],
                    'extracted': '',
                    'returncode': p.returncode,
                    'transient_retry': True,
                })
                continue
        except subprocess.TimeoutExpired:
            answer = 'I_DONT_KNOW'

        token = extract_identifier(kind, answer)
        attempts.append({'attempt': attempt, 'answer': answer[:2000], 'extracted': token, 'returncode': p.returncode if 'p' in locals() else None})
        raw_answer = answer
        if token:
            extracted = token
            break

    expected_primary = [x.lower() for x in e.get('must_contain', [])]
    expected_any = [x.lower() for x in e.get('must_contain_any', [])]

    ok = False
    if extracted:
        extracted_l = extracted.lower()
        if kind == 'orch_to_branch':
            ok = extracted_l in expected_primary
        else:
            targets = expected_any if expected_any else expected_primary
            ok = extracted_l in targets

    if ok:
        passed += 1

    rows.append({
        'n': i,
        'question': q,
        'kind': kind,
        'expected': e.get('must_contain', []),
        'expected_any': e.get('must_contain_any', []),
        'answer': raw_answer[:8000],
        'extracted': extracted,
        'passed': ok,
        'attempts': attempts,
    })

    status = 'OK' if ok else 'MISS'
    print(f"{i:02d} {status} {extracted or 'NO_ID'}", flush=True)

score = {
    'passed': passed,
    'total': len(expected),
    'pass_rate': (passed / len(expected)) if expected else 0.0,
    'batch_label': os.environ.get('OPENCLAW_50Q_BATCH_LABEL', ''),
    'batch_start': int(os.environ.get('OPENCLAW_50Q_BATCH_START', '1')),
    'batch_end': int(os.environ.get('OPENCLAW_50Q_BATCH_END', str(len(expected)))),
    'latest_dir': str(out_dir),
    'batch_dir': str(batch_dir),
}
(out_dir / 'qa-50.json').write_text(json.dumps(rows, indent=2))
(out_dir / 'score.json').write_text(json.dumps(score, indent=2))
(out_dir / 'failures.json').write_text(json.dumps([r for r in rows if not r['passed']], indent=2))
(batch_dir / 'qa-50.json').write_text(json.dumps(rows, indent=2))
(batch_dir / 'score.json').write_text(json.dumps(score, indent=2))
(batch_dir / 'failures.json').write_text(json.dumps([r for r in rows if not r['passed']], indent=2))
batch_meta_path = out_dir / 'batch-meta.json'
batch_meta = {}
if batch_meta_path.exists():
    try:
        batch_meta = json.loads(batch_meta_path.read_text())
    except Exception:
        batch_meta = {}
batch_meta.update({
    'score': score,
    'artifacts': {
        'expected': str(out_dir / 'expected-50.json'),
        'qa': str(out_dir / 'qa-50.json'),
        'failures': str(out_dir / 'failures.json'),
        'score': str(out_dir / 'score.json'),
        'batch_expected': str(batch_dir / 'expected-50.json'),
        'batch_qa': str(batch_dir / 'qa-50.json'),
        'batch_failures': str(batch_dir / 'failures.json'),
        'batch_score': str(batch_dir / 'score.json'),
    },
})
(out_dir / 'batch-meta.json').write_text(json.dumps(batch_meta, indent=2))
(batch_dir / 'batch-meta.json').write_text(json.dumps(batch_meta, indent=2))
print('FINAL', json.dumps(score))
PY

echo "$OUT"
