## Context

See `proposal.md` for motivation and `specs/auth-signin/spec.md` for the behavior contract. `signin_openai()` already issues one POST and calls `raise_for_status()`. The retry that can repeat it is the wider account-liveness preflight loop: it covers warmup, CSRF, session probe, and signin, then classifies exceptions by message, including `403`.

## Goals / Non-Goals

**Goals:**
- Prevent a signin HTTP 403 from being treated as a retryable preflight failure.
- Preserve the existing retry behavior for failures occurring before signin, including CSRF 403.
- Preserve the original HTTP exception and keep retry decisions limited to the request phase that failed.

**Non-Goals:**
- Change authorize navigation retries, registration flow, or the retry policy for unrelated requests.
- Change which network errors are retryable outside the signin phase.

## Decisions

- Make the account-liveness preflight retry decision phase-aware. Track the current stage around the sequential warmup / CSRF / session-probe / signin calls, and do not retry when the failure is HTTP 403 from signin.
  - Rationale: `signin_openai()` already propagates the response error; the behavior gap is the caller's broad retry catch. A stage-aware exception boundary preserves the original error and does not require changing the HTTP response type.
  - Alternative considered: globally remove 403 from `_RETRYABLE_NETWORK_HINTS`. Rejected because it would also change retries for CSRF and other preflight operations, contrary to the spec.
- Keep existing retry behavior for a CSRF 403 that occurs before signin. The phase marker must be reset on each preflight attempt so a prior signin attempt cannot affect classification of a later attempt.

## Risks / Trade-offs

- [Risk] Phase tracking may misclassify an exception if set too broadly → Set the phase immediately before each operation and check both the signin phase and HTTP status 403 in the retry decision.
- [Risk] A test may only verify `signin_openai()` in isolation and miss the caller retry → Cover the account-liveness preflight path with separate signin-403 and CSRF-403 scenarios.
