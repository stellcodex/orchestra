# ORCHESTRA Boundary

Ownership: execution authority, workflow/state transitions, required-input/approval gating, retries, evidence lifecycle, and execution-side integration with STELL.AI.

Canonical runtime: `runtime_app/`

Forbidden: product routing/UI ownership, auth/session ownership, strategic intelligence authority, and permanent archive ownership outside defined job exports.

## Known Open Debt

- **Retry logic**: Ownership is listed above but no retry or backoff is implemented in the current runtime. Transient backend or STELL.AI failures surface immediately as 503. Retry/backoff implementation is deferred.
- **Approve idempotency**: The `POST /sessions/approve` endpoint is intentionally fail-closed on re-attempt (returns 409 if session is not at S5). This is a deliberate integrity constraint, not an oversight. The README's general claim of "idempotent operations" does not apply to the approve transition.
