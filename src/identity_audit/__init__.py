"""Identity posture audit and lifecycle automation against Microsoft Graph.

Part A (read-only): nightly checks — MFA registration, stale-but-licensed
accounts, guest accounts, privileged role holders, service principal
credential expiry, ownerless groups, non-compliant/stale devices — rolled
up into a severity-ranked report.

Part B (writes, dry-run by default): onboarding and offboarding lifecycle
automation, gated behind an explicit flag and typed confirmation for any
real run, with a full audit trail of who ran what and what changed.
"""
