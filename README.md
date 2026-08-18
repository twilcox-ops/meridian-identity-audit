# Identity Automation and Posture Audit

<!-- TODO: replace with the actual problem this solves, e.g.:
"Identity teams find out about MFA gaps and orphaned admin access during an
audit or an incident, not before. This is a nightly job that surfaces those
gaps against a real Microsoft Graph tenant, and a dry-run-first automation
path for the onboarding/offboarding lifecycle work that usually happens by
hand." -->

TODO: one paragraph, problem-first, before any mention of libraries.

## Status

All seven of Part A's checks are now implemented, covered by tests that
mock Graph pagination and 429 throttling, and live-verified against a real
tenant:

- Users without MFA registered.
- Licensed users inactive 90+ days. The live run found 0 stale users in the
  sandbox tenant — expected, since its test users are fictitious and freshly
  created rather than long-idle, not evidence the logic never matches.
- Guest accounts and how long they've been in the tenant. The live run
  found 0 guests — expected for a fresh sandbox with no external invites
  sent, not evidence the logic doesn't work.
- Users holding privileged directory roles. The live run found the expected
  sandbox tenant admin as the sole privileged-role holder.
- Service principals with credentials nearing expiry. The live run also
  incidentally proved pagination against a real multi-page response — 168
  service principals across 2 pages — and found 0 credentials nearing
  expiry, expected for a fresh sandbox populated mostly with
  Microsoft-provisioned service principals rather than long-lived
  custom app registrations.
- Groups with no owner. The live run confirmed the N+1 call pattern flagged
  during development — 1 groups-list call plus 8 per-group owner checks —
  and found 0 ownerless groups among the sandbox's 8 groups, all of which
  have at least one owner. Expected for a small, actively-managed sandbox,
  not evidence the logic doesn't work.
- Devices that are non-compliant or haven't checked in. The live run found
  0 devices in the tenant — expected, since the sandbox has no
  enrolled/registered devices, not evidence the logic doesn't work.

**Part A's checks are now complete, and severity-ranked HTML report
generation is implemented, tested, and live-verified.** A live run against
the sandbox tenant correctly produced 16 critical findings (every MFA-less
user) and 1 warning finding (the sole privileged-role holder — not
escalated to critical, since that account does have MFA registered), and
the report rendered correctly in a browser. See "Report severity model"
below for the ranking logic.

Email delivery is also implemented, tested, and live-verified
end-to-end — the report email was sent via Graph's `sendMail` and
confirmed received in the sandbox mailbox, not just accepted with a 202
by the API. It's optional: unset `DIGEST_TO`/`DIGEST_FROM` and a run
produces the local file only.

Nightly scheduling via GitHub Actions is now implemented and
live-verified — a manually triggered run of the workflow succeeded
end-to-end: all seven checks returned 200, the report was written, and
the email sent with a 202, confirmed received in the sandbox mailbox from
the automated run itself, not just from a local one. The CI-log
suppression was confirmed live too: the Actions run log showed only the
per-check summary-count lines, no per-user detail. GitHub's own secret
scrubbing added a second layer on top of that automatically, redacting
the `DIGEST_FROM` address in the log wherever it appeared, on top of
(not instead of) the suppression logic already built for this.

**All three pieces of the "audit runs nightly, unattended, emailing a
severity-ranked report" acceptance criterion are now complete and
live-verified**: report generation, email delivery, and nightly
scheduling.

The "read-only until the audit is solid" gate from the project brief is now
met: every check so far issues GET requests only, and all seven of the
checks the brief asks for exist, are tested, and have been run against a
real tenant.

All of Part B is not started.

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

## Report severity model

Findings are ranked into three levels:

- **Critical** — an active exposure right now (no MFA registered) or
  something that already failed rather than being about to (a service
  principal credential that's expired, not just expiring).
- **Warning** — a governance or hygiene gap that raises risk without being
  an active compromise: stale-but-licensed accounts, ownerless groups,
  non-compliant devices, credentials nearing expiry, or simply holding a
  privileged role on its own.
- **Info** — visibility, not a failure: guest accounts (existing is normal
  for most orgs; this is about awareness of how long they've been around),
  and devices that are compliant but just haven't checked in recently.

**Escalation.** A privileged-role finding is bumped to critical if that
same user also shows up in one of these other checks:

- No MFA registered — an admin account reachable without a second factor.
- Guest account — an external identity holding elevated internal access.
- Inactive 90+ days, still licensed — a dormant admin credential nobody's
  watching but that still works.

The full reasoning, and the code that implements it, lives in
`src/identity_audit/report.py`.

## Design constraints

- **Certificate-based app-only auth** — no client secret in code or config.
- **Pagination** — follows `@odata.nextLink`, tested against a forced small
  page size.
- **Throttling** — honors `Retry-After` on HTTP 429, no fixed sleep.
- **Least privilege** — starts at `User.Read.All`; every additional
  permission gets a one-line justification here once it's requested.
- **Batching** — not implemented yet; planned to use Graph's `$batch`
  endpoint for bulk reads once there's enough per-run request volume across
  checks to make it worth measuring.
- **Audit log** — every write records who ran it, what changed, and the
  before/after state.

## Permissions

| Scope | Type | Justification |
| --- | --- | --- |
| `User.Read.All` | Application | Baseline directory read the app authenticates with — resolves the user identities (UPN, display name, account state) that every check's findings are reported against; app-only because this runs as an unattended nightly job with no signed-in user to delegate from. Also part of the confirmed-sufficient pair (with `AuditLog.Read.All`) for reading `signInActivity` in the stale-account check. |
| `Reports.Read.All` | Application | Needed by the MFA-registration check to call `reports/authenticationMethods/userRegistrationDetails`. Microsoft's docs list this as sufficient on its own, but live testing against this tenant showed it is **not** — see `AuditLog.Read.All` below. |
| `AuditLog.Read.All` | Application | Required alongside `Reports.Read.All` for `userRegistrationDetails` on this tenant: a live app-only call with `Reports.Read.All` granted and consented still 403'd with `Authentication_MSGraphPermissionMissing`, naming `AuditLog.Read.All` as missing. Broader than the docs suggest should be necessary, but confirmed required by testing, not assumption. Also covers the stale-account check's `signInActivity` reads on `/users` — live-tested against the tenant, no additional permission was needed there. |
| `RoleManagement.Read.Directory` | Application | Needed by the privileged-role check to call `/directoryRoles` and `/directoryRoles/{id}/members` — role membership is a distinct permission surface that none of the other granted scopes cover. Confirmed sufficient by live testing against the tenant, not assumed from docs. |
| `Application.Read.All` | Application | Needed by the service-principal-credential check to read `passwordCredentials`/`keyCredentials` on `/servicePrincipals` — service principal objects aren't covered by any of the other granted scopes. Confirmed sufficient by live testing against the tenant. |
| `GroupMember.Read.All` | Application | Needed by the ownerless-group check to call `/groups` and `/groups/{id}/owners` — group objects aren't covered by any of the other granted scopes. Confirmed sufficient by live testing against the tenant, no repeat of the docs-vs-reality surprise from the MFA check this time. |
| `Device.Read.All` | Application | Needed by the device-compliance check to call `/devices` — device objects aren't covered by any of the other granted scopes. Confirmed sufficient by live testing against the tenant, no additional permission required. |
| `Mail.Send` | Application | Needed to send the audit report via `POST /users/{sender}/sendMail`. Distinct from every other permission above: this is the first genuinely **write-capable** grant in the project — everything else is `*.Read.*`. Confirmed by live testing end-to-end: the email was both accepted by Graph and confirmed received in the mailbox, not just a 202 response. As granted, it's unscoped to a single mailbox — app-only `Mail.Send` allows sending as any user in the tenant, not just the configured sender. See "What I'd do differently" for why that's a known tradeoff, not something addressed in this portfolio version. |

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
- `Mail.Send` is granted app-wide, not scoped to the one mailbox this
  project actually sends from. Graph's app-only `Mail.Send` permission by
  default lets the app send as *any* mailbox in the tenant; Exchange
  Online's `ApplicationAccessPolicy` can restrict that to a single
  mailbox, but setting one up is an Exchange admin action outside this
  repo's code, and wasn't done here. Known tradeoff, not an oversight —
  worth calling out unprompted in an interview rather than waiting to be
  asked, and the first thing to fix before this pattern touched a real
  production tenant.
