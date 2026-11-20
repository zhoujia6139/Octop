# ADR 004 — Local Agent execution and independent cloud control

**Status:** Accepted; supersedes ADR 001's single combined process and the product assembly/storage assumptions in ADR 002/003. Their retained module details are historical, not product entry contracts.

**Date:** 2026-11-20

## Context and decision

Agent tools need to act on the user's computer. Cloud hosts login, resource administration, model access/settlement and backup storage. Build two independent application factories on Harness, reusing selected existing infrastructure; do not wrap the old OctopServer or fork runtime library behavior into Octop.

Electron plus a Python sidecar runs the Agent. The cloud does not execute tools or take over suspended tasks. Password plus TOTP/recovery authenticates accounts; one device binding per user is fenced by generation. All model calls use cloud authentication, grants, quotas and billing. Platform resources publish immutable revisions and assign to the current device; personal copies are independent. Supported messages/files/logical memory synchronize to cloud without execution checkpoints or credentials.

Source maintenance uses `vendor/octop-harness` and `vendor/octop-memory`, explicitly selected by uv path dependencies. This replaces the earlier P5 proposal to publish those versions to PyPI first. Local builds create library wheels from tracked source, and deliver them with Octop's wheel and locked role requirements. A cloud-only environment has no execution libraries installed. No remote release is part of this decision.

## Consequences

- Cloud/local migrations, state and factories are separate. Only the local process imports Harness/Memory. Cloud keeps encrypted keys and content in its database; local data stays within the verified identity namespace.
- Renderer credentials are mediated by main. Quiesce, cancel, memory drain and final-sync acknowledgement precede detachment. Wake/restart does not replay tools or pending approvals.
- No first-phase web chat, IM, cron, cross-device continuation or cross-user Agent collaboration. Legacy source may remain for shared regressions but is not registered.
- Deployment examples use one cloud gateway and ordinary PostgreSQL or SQLite; vector storage and sticky sessions are not required. Native installers and OS keychain/lifecycle need actual platform validation, beyond unit-test claims.
- CI and packaging validate dependency isolation and actual installed CLI startup. User-requested final test scope is changed/directly dependent product behavior, not unrelated legacy features.

See [architecture](../architecture.md), [product setup](../product-applications.md), [Plan.md](../../Plan.md) and [shared contracts](../desktop-cloud-contracts.md).
