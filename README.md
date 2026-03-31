# ORCHESTRA

Execution and state authority for STELLCODEX.

## Role in System

ORCHESTRA is responsible for executing workflows and managing state transitions.

It handles:
- job execution
- workflow progression
- state management
- task orchestration

## System Position

- STELLCODEX → product/workflow surface  
- STELL.AI → intelligence authority  
- ORCHESTRA → execution authority  
- INFRA → runtime infrastructure  

## Responsibility

- execute decisions produced by STELL.AI  
- maintain deterministic workflow state  
- ensure idempotent operations  

## Rules

- do not implement intelligence here  
- do not bypass STELL.AI decisions  
- do not move infrastructure logic here  

## Related

- `stellcodex/stellcodex`
- `stellcodex/stell-ai`
- `stellcodex/infra`
- `stellcodex/stell-assistant`
