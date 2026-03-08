# STELLCODEX Orchestra

Local-first orchestration stack under `/root/workspace/ops/orchestra`.

## Services
- `orchestrator` on `7010`
- `litellm` on `4000`
- `ollama` on `11434` (internal)
- `autopilot` folder runner
- `stellai` on `7020`

All core services are configured with `restart: always`.

## One-Command Bringup
```bash
cd /root/workspace/ops/orchestra
./run.sh
```

`run.sh` does:
1. Creates `/root/workspace/_jobs/{inbox,done,failed,deferred,output,logs,backups}`.
2. Runs mandatory preflight and writes:
   - `/root/workspace/_jobs/logs/PREFLIGHT_REPORT.md`
   - `/root/workspace/_jobs/logs/PREFLIGHT_SNAPSHOT.json`
   - appends preflight events to `/root/workspace/_jobs/logs/events.jsonl`
3. Runs `docker-compose up -d --build` for this stack.
4. Runs local smoke probe (`scripts/smoke_local.sh`) for `local_fast` and `local_reason`.
5. Prints exactly one final line: `READY`, `READY_LOCAL`, or `DEGRADED (...)`.

## Send a Task
```bash
cd /root/workspace/ops/orchestra
./orchestra.sh "Write a 10-line system health report"
```

Default speed is `eco`.
If task text starts with first line `SPEED=max` (or `SPEED=eco`), wrapper uses that speed.

## Autopilot Runner
```bash
cd /root/workspace/ops/orchestra
./autopilot.sh start
./autopilot.sh status
./autopilot.sh tail
./autopilot.sh stop
```

Autopilot watches `/root/workspace/_jobs/inbox` every 5 seconds for `.md/.txt`, calls `/orchestrate`, and moves files to:
- `done/`
- `failed/`
- `deferred/`

Per-task outputs are written to:
- `/root/workspace/_jobs/output/<task_id>/final_output.md`
- `/root/workspace/_jobs/output/<task_id>/routing.json`
- `/root/workspace/_jobs/output/<task_id>/results.json`
- `/root/workspace/_jobs/output/<task_id>/status.json`

Runtime events are append-only in `/root/workspace/_jobs/logs/events.jsonl`.

## Readiness Rules
- `READY`: at least one paid model passes inference probe.
- `READY_LOCAL`: paid unavailable but local probe passes (`local_fast` or `local_reason`).
- `FAIL`: LiteLLM unreachable, or zero models pass inference probe.

`/state` includes cached readiness probe (60s TTL).

## Local Smoke
```bash
cd /root/workspace/ops/orchestra
./scripts/smoke_local.sh
```

## Manual Proof Commands
```bash
curl -sS http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local_fast","messages":[{"role":"user","content":"Reply OK"}],"max_tokens":16}'

curl -sS http://localhost:7010/state

curl -sS -X POST http://localhost:7010/orchestrate \
  -H 'Content-Type: application/json' \
  -d '{"task":"Write a 10-line system health report","speed":"eco"}'
```

## Watchdog
`watchdog.sh` checks `/state` every 60 seconds and runs:
```bash
docker-compose -f /root/workspace/ops/orchestra/docker-compose.yml restart orchestrator litellm autopilot
```

## Stellcodex 7/24 Orchestrator
For the 45-module runtime orchestration flow:

```bash
cd /root/workspace/ops/orchestra
./stellcodex_247.sh run-once
./stellcodex_247.sh start
./stellcodex_247.sh status
./stellcodex_247.sh tail
```

Generated outputs:
- `/root/stellcodex_output/REPORT.md`
- `/root/stellcodex_output/test_results.json`
- `/root/stellcodex_output/limits.log`
- `/root/stellcodex_output/errors.log`

Report interaction CLI:

```bash
python3 /root/workspace/ops/orchestra/stellcodex_report_cli.py
```

Systemd enablement (persistent 7/24):

```bash
sudo cp /root/workspace/ops/systemd/stellcodex-247.service /etc/systemd/system/
sudo cp /root/workspace/ops/systemd/stellcodex-247-watchdog.service /etc/systemd/system/
sudo cp /root/workspace/ops/systemd/stellcodex-247-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stellcodex-247.service
sudo systemctl enable --now stellcodex-247-watchdog.timer
```
