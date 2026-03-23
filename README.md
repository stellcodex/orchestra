# ORCHESTRA

Canonical owner for STELLCODEX execution and state authority.

## Responsibility

- workflow state machine
- required-input gating
- approval gating
- execution/session persistence through backend internal runtime APIs
- execution-side integration with STELL.AI decision authority

## Canonical runtime

- authoritative HTTP runtime: `runtime_app/`
- canonical image entrypoint: [`Dockerfile`](/root/workspace/_canonical_repos/orchestra/Dockerfile)

This runtime is the proven split service used by backend proxy calls.

## Repository notes

- `runtime_app/` is the canonical STELLCODEX integration runtime
- `orchestrator/`, `src/`, `litellm*`, and helper scripts remain Orchestra-owned historical/support material
- this repo is no longer a boundary-only placeholder
