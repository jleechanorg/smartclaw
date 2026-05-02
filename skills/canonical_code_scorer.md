# Canonical Code Scorer

Score any generated code against the world's cleanest, most respected codebases.

## How to Use

When you have generated code (from a PR, a patch, a harness output), run the scorer:

```
You are a code quality scorer. Evaluate this generated code against canonical exemplars.

Code to evaluate:
```
<generated code>
```

Canonical repos to compare against:
- FastAPI (Python) — error handling, async, type annotations
- Requests (Python) — simple & correct API design
- tRPC (TypeScript) — end-to-end type safety
- TanStack Query — state management patterns
- Axum/Tokio (Rust) — async composable handlers

## Scoring Dimensions (100 total)

1. **Type Safety / Architecture** (0-30) — TypedDict, strong typing, clean architecture; composable like Axum; clean separation like TanStack Query
2. **Error Handling / Robustness** (0-20) — Handles errors like Requests (simple, clear) or FastAPI (typed exceptions, middleware); input validation; edge cases
3. **Naming & Consistency** (0-15) — Naming match FastAPI/tRPC conventions; descriptive names; consistent style
4. **Test Coverage & Clarity** (0-15) — Code invites testing; edge cases obvious; testable by construction
5. **Documentation Standards** (0-10) — Docstrings explain *why*, not *what*; inline comments on non-obvious logic
6. **Evidence-Standard Adherence** (0-10) — Harness evidence standards met (video, captions, gist)

## Output Format

```
## Canonical Code Score: X/100

### Type Safety / Architecture: X/30
### Error Handling / Robustness: X/20
### Naming & Consistency: X/15
### Test Coverage & Clarity: X/15
### Documentation Standards: X/10
### Evidence-Standard Adherence: X/10

## Top 3 Improvement Suggestions
1. ...
2. ...
3. ...

## Comparison to Canonical Repos
- Closest match: [FastAPI/tRPC/etc.]
- Missing from: [what pattern is absent]
```
