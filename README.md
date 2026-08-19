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
met, held to precisely rather than overstated: the audit issues no
identity- or directory-mutating writes — no user, group, role, or device is
ever created, changed, or deleted. `Mail.Send`'s `sendMail` call is a real
`POST`, so "every check issues GET requests only" is no longer literally
true, but that POST is a notification side-effect of the report, not a
state-changing write against the Graph data model the checks read from. All
seven of the checks the brief asks for exist, are tested, and have been run
against a real tenant.

Both pieces of Part B are now built and dry-run tested. Onboarding (create
user, add to department groups, assign license) has had a real `--execute`
run succeed for user creation and license assignment against the tenant,
though the group-add step and the overall flow haven't completed
end-to-end yet — see the Permissions table for what's still pending
there. Offboarding (disable sign-in, revoke refresh tokens, remove from
groups, reclaim the license, convert mailbox to shared) has now had a
full real `--execute` run succeed end-to-end against the tenant — disable,
revoke, both group removals, and the correctly-not-automated mailbox step
all completed as designed — with one known transient issue on the
license-reclaim step (a 409 that a manual retry resolved, root cause not
confirmed). See "Offboarding reversibility" below and
`src/identity_audit/offboarding.py`'s docstring for the full account of
that issue.

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

## Offboarding reversibility

Per-action, not a general statement — the project brief asks for this to
be documented precisely:

| Action | Reversible via Graph? | How / why not |
| --- | --- | --- |
| Disable sign-in | Yes | `PATCH /users/{id}` with `accountEnabled: true` restores the exact prior state. |
| Revoke refresh tokens | Not as an action | No Graph call un-revokes a specific session — those tokens are invalidated permanently. Not a lasting lockout, though: a still-enabled account lets the user sign in again immediately and get a fresh valid session. |
| Remove from groups | Yes | `POST /groups/{id}/members/$ref` (the same call onboarding uses to add) re-adds the user to each group ID the audit trail recorded before removal. |
| Reclaim the license | Yes, with a caveat | `assignLicense`'s `addLicenses` re-assigns the same SKU — but if the tenant's SKU pool has no free seats left by the time someone reverses it, that fails for licensing-inventory reasons, not a Graph limitation. |
| Convert mailbox to shared | Not automated at all | There's no Microsoft Graph endpoint for mailbox type conversion — it's an Exchange Online administrative operation, requiring Exchange's own `Exchange.ManageAsApp` application permission on a separate auth surface this project has never used. Not faked or silently skipped: every offboarding run logs this action with `result="not_automated"`. |

**Live-verified end-to-end.** A full real `--execute` offboarding run
succeeded against the tenant: disable, revoke, both group removals, and
the mailbox step (correctly `not_automated`) all completed as designed.
One known transient issue: the license-reclaim step hit a 409 immediately
after disable+revoke; a manual retry of the identical call succeeded with
no code changes. The original 409's error body was never captured, so
this is **known but not root-caused** — consistent with directory
propagation delay after the two preceding writes, not confirmed as the
actual cause. No retry loop has been added for this step. Full account in
`src/identity_audit/offboarding.py`'s docstring.

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
| `User.ReadWrite.All` | Application | Needed by onboarding for `POST /users` (create) and `POST /users/{id}/assignLicense`, and by offboarding for `PATCH /users/{id}` (disable sign-in) and reclaiming the license via the same `assignLicense` call — Graph's own documentation lists this as the least-privileged permission for all of these. Partially live-verified: a real onboarding `--execute` run against the tenant successfully created a user and successfully assigned a license. Not yet confirmed sufficient in the same fully-tested sense as the Part A rows above, since the same run's group-add step failed (see `GroupMember.ReadWrite.All` below), the onboarding flow hasn't completed end-to-end, and none of offboarding's uses of this permission have been live-tested at all yet. |
| `User.RevokeSessions.All` | Application | Needed by offboarding for `POST /users/{id}/revokeSignInSessions` (revoke refresh tokens) — **not covered by `User.ReadWrite.All`**. This was assumed correct the first time and turned out wrong: Graph's own permissions table for this specific action lists `User.RevokeSessions.All` as the *only* Application permission, with the higher-privileged-alternative column reading "Not available" for Application (the broader options Graph does show, e.g. `Directory.ReadWrite.All`, apply only to the Delegated permission row). Caught and corrected during development by checking the docs directly rather than assuming `User.ReadWrite.All`'s broad coverage extended here. A distinct, new grant — not yet live-tested. |
| `GroupMember.ReadWrite.All` | Application | Needed by onboarding for `POST /groups/{id}/members/$ref` (add to group) and by offboarding for `DELETE /groups/{id}/members/{id}/$ref` (remove from group) — widens the read-only `GroupMember.Read.All` already granted for the ownerless-groups check, per Graph's documentation for both endpoints. No new grant needed for offboarding's use beyond what onboarding already requires. **Not yet live-confirmed sufficient for either direction**: onboarding's real `--execute` run's group-add calls failed, but against placeholder GUIDs from the example config, not real group IDs — the failure is consistent with an invalid/nonexistent resource, not a permission denial, so this remains a docs-based justification pending a successful run against a real group ID. Offboarding's use of this permission (removal) hasn't been live-tested at all yet. |

## Setup

TODO once implemented — will cover the Microsoft 365 Developer tenant, the
Entra ID app registration (certificate upload, no secret), and `.env` from
`.env.example`.

### GitHub Actions secrets

The nightly workflow (`.github/workflows/nightly-audit.yml`) reads
everything it needs from repo secrets — Settings → Secrets and variables →
Actions → New repository secret. Six are required for the workflow to run
at all:

| Secret | Contents |
| --- | --- |
| `GRAPH_CERT_PEM` | Full contents of the certificate's PEM private key file (the one `GRAPH_CERT_PATH` points at locally) — paste the file contents as-is. |
| `GRAPH_TENANT_ID` | Same value as the local `.env`'s `GRAPH_TENANT_ID`. |
| `GRAPH_CLIENT_ID` | Same value as the local `.env`'s `GRAPH_CLIENT_ID`. |
| `GRAPH_CERT_THUMBPRINT` | Same value as the local `.env`'s `GRAPH_CERT_THUMBPRINT`. |
| `DIGEST_TO` | Report recipient address. |
| `DIGEST_FROM` | Report sender mailbox — must be one the `Mail.Send`-granted app can send as. |

One more is optional: `HEARTBEAT_URL`, a healthchecks.io (or similar)
dead-man's-switch ping URL. Omit it and the heartbeat step just no-ops
rather than failing the run.

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
- A live offboarding run hit a 409 on the license-reclaim step,
  immediately after disable-sign-in and revoke-refresh-tokens had both
  already succeeded against the same user. A manual retry of the
  identical call, no code changes, succeeded with a 200 — evidence the
  call and permission are fine, but evidence from a successful *retry*,
  not from the original failure: the 409's actual response body was never
  captured, so Graph's stated reason for it is unknown. This is **known
  but not root-caused** — consistent with directory propagation delay
  right after two preceding writes to the same user, not confirmed as the
  actual cause. No retry loop has been added for this step; it's
  documented as an open issue, not treated as solved. Lesson: a
  successful retry tells you the code isn't broken, it doesn't tell you
  why the first call failed — don't let it read as more resolved than
  it is.
