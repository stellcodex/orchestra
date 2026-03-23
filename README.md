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
- canonical image entrypoint: `Dockerfile`

This runtime is the proven split service used by backend proxy calls.

## Repository notes

- `runtime_app/` is the only active service runtime in this repo
- historical sidecars, helper scripts, and alternate runtime trees were removed during canonical lock
- this repo is no longer a boundary-only placeholder
