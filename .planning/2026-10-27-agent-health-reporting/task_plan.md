# Agent health reporting

## Objective
Correct agent health classifications and include actionable reasons while preserving existing local changes.

## Scope
collector.py health evaluation, service discovery/callers, delivery, regression tests, README, canonical SilverBullet knowledge. No API, frontend, credentials, installer or dependency changes needed.

## Phases
1. Inspect health paths and existing changes — complete.
2. Correct classifications and reporting — complete.
3. Run tests and inspect final diff — complete.
4. Save a concise canonical knowledge update — complete.

## Next Step
Complete: report local fixes, test evidence and deployment limits.

## Risks
Retain sender health payload compatibility. Do not access or deploy to hosts. Preserve existing TLS/network/discovery changes. Avoid treating intentionally inactive/masked services or successful oneshots as failures.

## Validation
24 unit/integration tests passed, including real local HTTP and HTTPS delivery and installer regressions. git diff --check passed. Knowledge page and both navigation links reread successfully; new page sources and linked parents checked. No live host access or rollout.

## Knowledge maintenance scope
Canonical page: knowledge/projects/HyperHive/features/observability/hh-agent-health. Added discovery links in observability/index and hh-support. K6 exception: pre-existing oversized parent pages received only small navigation links; a whole-page rewrite was outside this fix. Their unrelated claims were not revalidated.
