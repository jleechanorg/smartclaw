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

## Scoring Dimensions (0-20 each, 100 total)

1. **Naming & Consistency** (0-20) — Does naming match FastAPI/tRPC conventions? Descriptive names? Consistent style?
2. **Error Handling** (0-20) — Does it handle errors like Requests (simple, clear) or FastAPI (typed exceptions, middleware)?
3. **Type Safety** (0-20) — Does it have type annotations like FastAPI/tRPC? No `any` types? Structued inputs?
4. **Architecture** (0-20) — Is it composable like Axum? Clean separation like TanStack Query?
5. **Test Coverage Signal** (0-20) — Does the code invite testing? Are edge cases obvious? Is it testable by construction?

## Output Format

```
## Canonical Code Score: X/100

### Naming & Consistency: X/20
- [what's good]
- [what's not]

### Error Handling: X/20
- [what's good]
- [what's not]

... (each dimension)

## Top 3 Improvement Suggestions
1. ...
2. ...
3. ...

## Comparison to Canonical Repos
- Closest match: [FastAPI/tRPC/etc.]
- Missing from: [what pattern is absent]
```
