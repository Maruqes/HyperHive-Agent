# Findings
- Existing dirty files: .env.example, README.md, collector.py, tests/test_collector.py; TLS opt-out, all-service/interface discovery changes predate this task.
- Backend persists explicit health independently of heartbeat and aggregates worst participant health.
- Agent marks every non-loaded service unhealthy, lacks oneshot semantics, drops a full service scan when one show fails, and service events only consider ActiveState=failed.
- SMART failure and filesystem exhaustion are collected but ignored by health.
- Delivery starts before initial snapshots and can report healthy before checks complete.
- Knowledge consulted: HyperHive index and frontend/support-widget-layout Snapshot tables. No live deployment verified.
- Implemented explicit service semantics, normalized names/aliases, tolerated missing-unit show output, aligned failure/recovery events, initial pending checks, SMART failures, writable filesystem 90/98 percent space/inode rules, and critical-first UTF-8 bounded health reasons. No network/CPU/memory threshold feature added.
- Confirmed successful inactive oneshot semantics against the official systemd.service documentation.
