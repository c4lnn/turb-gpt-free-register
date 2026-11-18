## Purpose

Defines failure handling for the NextAuth signin request so explicit authorization rejection is surfaced to callers without silently repeating the signin operation.

## ADDED Requirements

### Requirement: Signin HTTP 403 fails without retry
The system MUST propagate an HTTP 403 response from the signin request without resending that request or restarting a containing authentication preflight. This requirement does not change retry behavior for the preceding CSRF request or unrelated network operations.

#### Scenario: Signin responds with HTTP 403
- **WHEN** the signin request returns HTTP 403
- **THEN** the caller receives the failure and no second signin request is sent

#### Scenario: CSRF responds with HTTP 403 before signin
- **WHEN** the CSRF request returns HTTP 403 before any signin request is sent
- **THEN** the existing CSRF retry policy remains unchanged
