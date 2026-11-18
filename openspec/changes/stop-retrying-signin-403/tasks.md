## 1. Phase-aware signin retry boundary

- [ ] 1.1 Update the account-liveness preflight retry decision so an HTTP 403 raised during signin propagates immediately, while HTTP 403 from the preceding CSRF request retains its current retry behavior; add regression tests for both cases and verify them with the targeted account-liveness test command.
- [ ] 1.2 Run the focused authentication and registration regression tests to verify signin handling changes without altering ordinary registration or authorize retry behavior.
