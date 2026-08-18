# Identity Automation and Posture Audit

<!-- TODO: replace with the actual problem this solves, e.g.:
"Identity teams find out about MFA gaps and orphaned admin access during an
audit or an incident, not before. This is a nightly job that surfaces those
gaps against a real Microsoft Graph tenant, and a dry-run-first automation
path for the onboarding/offboarding lifecycle work that usually happens by
hand." -->

TODO: one paragraph, problem-first, before any mention of libraries.

## Status

Part A's first check — users without MFA registered — is implemented,
covered by tests that mock Graph pagination and 429 throttling, and
live-verified against a real tenant. Everything else in Part A, and all of
Part B, is not started.

## What this will do

**Part A — audit (read-only, built first):**

- Users without MFA registered
- Accounts that haven't signed in for 90+ days but still hold licenses
- Guest accounts and how long they've been there
- Users holding privileged directory roles
- Service principals with credentials nearing expiry
- Groups with no owner
- Devices that are non-compliant or haven't checked in

Output: an HTML report, severity-ranked, emailed on a schedule.

**Part B — writes (dry-run by default, built after Part A is solid):**

Onboarding: create user, assign groups from a department mapping, assign a
license, log every action.

Offboarding: disable sign-in, revoke refresh tokens, remove from groups,
reclaim the license, convert the mailbox to shared. Reversible where the
Graph API allows it, documented where it isn't.

Every write path defaults to `--dry-run`; a real run needs an explicit flag
and a typed confirmation.

## Design constraints

- **Certificate-based app-only auth** — no client secret in code or config.
- **Pagination** — follows `@odata.nextLink`, tested against a forced small
  page size.
- **Throttling** — honors `Retry-After` on HTTP 429, no fixed sleep.
- **Least privilege** — starts at `User.Read.All`; every additional
  permission gets a one-line justification here once it's requested.
- **Batching** — uses Graph's `$batch` endpoint for bulk reads.
- **Audit log** — every write records who ran it, what changed, and the
  before/after state.

## Permissions

| Scope | Type | Justification |
| --- | --- | --- |
| `User.Read.All` | Application | Baseline directory read the app authenticates with — resolves the user identities (UPN, display name, account state) that every check's findings are reported against; app-only because this runs as an unattended nightly job with no signed-in user to delegate from. |
| `Reports.Read.All` | Application | Needed by the MFA-registration check to call `reports/authenticationMethods/userRegistrationDetails`. Microsoft's docs list this as sufficient on its own, but live testing against this tenant showed it is **not** — see `AuditLog.Read.All` below. |
| `AuditLog.Read.All` | Application | Required alongside `Reports.Read.All` for `userRegistrationDetails` on this tenant: a live app-only call with `Reports.Read.All` granted and consented still 403'd with `Authentication_MSGraphPermissionMissing`, naming `AuditLog.Read.All` as missing. Broader than the docs suggest should be necessary, but confirmed required by testing, not assumption. |

## Setup

TODO once implemented — will cover the Microsoft 365 Developer tenant, the
Entra ID app registration (certificate upload, no secret), and `.env` from
`.env.example`.

## Architecture

TODO: diagram once there's a shape to draw.

## Measurements

TODO: batching improvement, throttle-recovery behavior, whatever else this
project ends up producing numbers for.

## What I'd do differently

*Raw draft notes — captured as they happened, to be refined into prose once
the project is further along.*

- Microsoft's own Graph docs list `Reports.Read.All` as sufficient for the
  `userRegistrationDetails` endpoint. Live testing against the real tenant
  proved otherwise — got a 403 with `Authentication_MSGraphPermissionMissing`
  naming `AuditLog.Read.All` as required. Lesson: verify permission
  requirements against a live tenant rather than trusting docs alone,
  especially for less-common endpoints.
- The Microsoft 365 Developer Program's free sandbox eligibility has
  tightened — it's no longer automatic just by joining. Personal Microsoft
  accounts without a Visual Studio Professional/Enterprise subscription or
  partner program membership got rejected with "you don't currently
  qualify." Had to use a different account type to get through. Lesson:
  verify current program eligibility rules before assuming a "free and
  renewable" resource is still frictionless.
- Getting from a Windows certificate store object to a file MSAL can
  actually read wasn't a single documented step — required exporting to
  PFX via PowerShell, then converting to PEM via OpenSSL. Lesson: budget
  time for tooling gaps between "the cert exists" and "the cert is usable
  by your auth library."
