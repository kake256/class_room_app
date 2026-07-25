# CGA outbound gateway

This package contains two deliberately small components:

- `gateway.app`: an in-memory HTTPS reverse relay for the explicitly allowlisted Web UI/API routes.
- `gateway.agent`: a 251-side long-poll process that makes outbound connections and forwards allowed requests to the loopback grading API.

No client-specific gateway token is needed; the local application's Google session, CSRF checks, and RBAC remain authoritative. The cloud/agent channel uses a separate secret whose SHA-256 digest is configured in the cloud.

Build the gateway from the repository root with `docker build -f gateway/Dockerfile .`. Run unit tests with `pytest -q gateway/tests`. Configuration, provider URL examples, OAuth redirect setup, limits, and the no-persistence/no-PII-log policy are documented in [`docs/outbound-gateway.md`](../docs/outbound-gateway.md).
