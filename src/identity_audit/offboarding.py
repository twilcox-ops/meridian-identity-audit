"""Part B, second piece: offboarding writes for a departing user.

Disable sign-in, revoke refresh tokens, remove from every group, reclaim a
license, and - documented, not automated, see below - convert the mailbox
to shared. Same dry-run default + `--execute` + typed-confirmation pattern
as `onboarding.py`, same shared `AuditEntry` audit trail.

## Permissions

Mostly reuses what onboarding already needs, but with one correction and
one genuinely new grant:

- `User.ReadWrite.All` - disable sign-in (`PATCH /users/{id}`) and reclaim
  the license (`POST /users/{id}/assignLicense`). Already requested for
  onboarding, no new grant for these two.
- `User.RevokeSessions.All` - **a separate, new grant**, for
  `POST /users/{id}/revokeSignInSessions`. Initially assumed
  `User.ReadWrite.All` covered this too; verified against Microsoft's own
  docs and it doesn't. Graph's permissions table for this specific action
  lists `User.RevokeSessions.All` as the *only* Application permission -
  the "higher privileged alternative" column reads "Not available" for
  Application (the broader alternatives Graph shows, e.g.
  `Directory.ReadWrite.All`, only apply to the Delegated permission row).
  `User.ReadWrite.All` isn't a valid app-only substitute for this call at
  all, not just an unnecessarily-broad one.
- `GroupMember.ReadWrite.All` - remove from groups
  (`DELETE /groups/{id}/members/{id}/$ref`). Already requested for onboarding.
- `GroupMember.Read.All` (already granted for the ownerless-groups check)
  - enumerating current group membership (`GET /users/{id}/memberOf`).
- `User.Read.All` (already granted, Part A baseline) - resolving the UPN
  to an object id (`GET /users/{id}`).

## Convert mailbox to shared - not automated, and why

There is no Microsoft Graph endpoint for mailbox type conversion. It's an
Exchange Online administrative operation (`Set-Mailbox -Type Shared` or the
Exchange admin center), requiring Exchange's own app-only permission model
(`Exchange.ManageAsApp`) - a different auth surface entirely, never used
anywhere else in this project. Rather than fake this or silently skip it,
`offboard_user()` always logs it with `result="not_automated"` so it's
honest in the audit trail and doesn't look either done or missing.

## Reversibility (documented per the spec, not built as a `--rollback`
## command - matching onboarding, which doesn't have one either)

- **Disable sign-in** - reversible: `PATCH accountEnabled: true` restores
  exactly the prior state.
- **Revoke refresh tokens** - *not* reversible as an action (no Graph call
  un-revokes a specific session, those tokens are gone permanently), but
  not a lasting lockout either: a still-enabled account lets the user sign
  in again immediately and get a fresh valid session.
- **Remove from groups** - reversible: re-add via the same
  `POST /groups/{id}/members/$ref` onboarding uses, replaying the group
  IDs this module's audit trail recorded before removing them.
- **Reclaim the license** - reversible via `assignLicense`'s
  `addLicenses`, with one caveat: if the SKU pool has no free seats by the
  time someone reverses it, that fails for licensing-inventory reasons,
  not a Graph limitation.
- **Convert mailbox to shared** - the underlying Exchange operation is
  itself administratively reversible, but since nothing here performs the
  forward direction either, reversibility is moot for what's built.

## Dry-run and the accuracy tradeoff that comes with it

Dry-run makes zero real Graph calls, matching onboarding's tested
guarantee. The cost: dry-run can't resolve the UPN to an object id or
enumerate real current group membership (both require a live call), so the
`remove_from_groups` dry-run entry is necessarily generic rather than
listing the groups that would actually be removed. `--execute` is what
resolves the id and enumerates `memberOf` for real.

`memberOf` returns a mixed collection of groups *and* activated directory
roles. Only `@odata.type == "#microsoft.graph.group"` entries are removed
here - pulling someone out of a privileged role via this same code path
would need `RoleManagement.ReadWrite.Directory`, not requested, and
deserves its own explicit decision rather than being a side effect of
"remove from groups."
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from identity_audit.audit_trail import AuditEntry, record_audit_entry
from identity_audit.auth import get_access_token
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_LOG_PATH = Path("logs") / "offboarding-audit.jsonl"

USERS_PATH = "/users"
GROUP_ODATA_TYPE = "#microsoft.graph.group"


@dataclass(frozen=True)
class OffboardingResult:
    user_id: str | None  # None if dry-run or user resolution itself failed
    groups_removed: list[str]
    entries: list[AuditEntry]


def offboard_user(
    client: GraphClient,
    user_principal_name: str,
    license_sku_id: str,
    operator: str,
    dry_run: bool = True,
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH,
    now: datetime | None = None,
) -> OffboardingResult:
    """Disable, revoke tokens, remove from groups, reclaim license.

    `client` is never called when `dry_run=True` - every action is planned
    and audited with `result="simulated"` instead (`not_automated` for the
    mailbox step, which is never actually attempted either way). Group
    membership is discovered live via `memberOf` in a real run, not
    assumed from config, since a departing user may hold memberships
    beyond whatever they were given at onboarding time.
    """
    reference_time = now or datetime.now(timezone.utc)
    timestamp = reference_time.isoformat(timespec="seconds")
    entries: list[AuditEntry] = []

    def audit(action: str, before: dict | None, after: dict | None, result: str) -> None:
        entry = AuditEntry(
            timestamp=timestamp,
            operator=operator,
            action=action,
            dry_run=dry_run,
            target=user_principal_name,
            before=before,
            after=after,
            result=result,
        )
        entries.append(entry)
        record_audit_entry(entry, path=audit_log_path)

    # --- Resolve the user (skipped entirely in dry-run - see module docstring) ---
    if dry_run:
        user_id = None
        was_enabled = None
    else:
        try:
            response = client.get(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_principal_name}",
                params={"$select": "id,accountEnabled"},
            )
            resolved = response.json()
            user_id = resolved.get("id")
            was_enabled = resolved.get("accountEnabled")
        except GraphError as exc:
            audit("resolve_user", before=None, after=None, result="failed")
            logger.error("Could not resolve user, aborting offboarding: %s", exc)
            return OffboardingResult(user_id=None, groups_removed=[], entries=entries)

    # --- Disable sign-in ---
    if dry_run:
        audit(
            "disable_sign_in",
            before={"accountEnabled": True},
            after={"accountEnabled": False},
            result="simulated",
        )
    else:
        try:
            client.patch(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}",
                json_body={"accountEnabled": False},
            )
            audit(
                "disable_sign_in",
                before={"accountEnabled": was_enabled},
                after={"accountEnabled": False},
                result="success",
            )
        except GraphError as exc:
            audit(
                "disable_sign_in",
                before={"accountEnabled": was_enabled},
                after=None,
                result="failed",
            )
            logger.error("Disabling sign-in failed, continuing offboarding: %s", exc)

    # --- Revoke refresh tokens ---
    if dry_run:
        audit("revoke_refresh_tokens", before=None, after=None, result="simulated")
    else:
        try:
            client.post(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}/revokeSignInSessions",
                json_body={},
            )
            audit(
                "revoke_refresh_tokens",
                before={"sessions_valid": True},
                after={"sessions_valid": False},
                result="success",
            )
        except GraphError as exc:
            audit("revoke_refresh_tokens", before=None, after=None, result="failed")
            logger.error("Revoking refresh tokens failed, continuing offboarding: %s", exc)

    # --- Remove from every current group membership ---
    groups_removed: list[str] = []
    if dry_run:
        audit(
            "remove_from_groups",
            before=None,
            after=None,
            result="simulated",
        )
    else:
        try:
            group_ids: list[str] = []
            for page in client.get_pages(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}/memberOf",
                params={"$select": "id,displayName"},
            ):
                group_ids.extend(
                    item["id"] for item in page if item.get("@odata.type") == GROUP_ODATA_TYPE
                )
        except GraphError as exc:
            audit("remove_from_groups", before=None, after=None, result="failed")
            logger.error("Could not enumerate group memberships: %s", exc)
            group_ids = []

        for group_id in group_ids:
            try:
                client.delete(f"{GRAPH_BASE_URL}/groups/{group_id}/members/{user_id}/$ref")
                audit(
                    "remove_from_groups",
                    before={"member_of": True, "group_id": group_id},
                    after={"member_of": False, "group_id": group_id},
                    result="success",
                )
                groups_removed.append(group_id)
            except GraphError as exc:
                audit(
                    "remove_from_groups",
                    before={"member_of": True, "group_id": group_id},
                    after=None,
                    result="failed",
                )
                logger.error("Removing from group %s failed: %s", group_id, exc)

    # --- Reclaim the license ---
    if dry_run:
        audit(
            "reclaim_license",
            before={"licensed": True, "sku_id": license_sku_id},
            after={"licensed": False},
            result="simulated",
        )
    else:
        try:
            client.post(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}/assignLicense",
                json_body={"addLicenses": [], "removeLicenses": [license_sku_id]},
            )
            audit(
                "reclaim_license",
                before={"licensed": True, "sku_id": license_sku_id},
                after={"licensed": False},
                result="success",
            )
        except GraphError as exc:
            audit(
                "reclaim_license",
                before={"licensed": True, "sku_id": license_sku_id},
                after=None,
                result="failed",
            )
            logger.error("Reclaiming license failed: %s", exc)

    # --- Convert mailbox to shared: not automated, see module docstring ---
    audit("convert_mailbox_to_shared", before=None, after=None, result="not_automated")

    return OffboardingResult(user_id=user_id, groups_removed=groups_removed, entries=entries)


def _confirm_real_run(user_principal_name: str, confirm_fn: Callable[[str], str] = input) -> bool:
    typed = confirm_fn(
        f"Type the exact user principal name to confirm offboarding "
        f"{user_principal_name} for real: "
    )
    return typed.strip() == user_principal_name


def main(argv: list[str] | None = None, confirm_fn: Callable[[str], str] = input) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    parser = argparse.ArgumentParser(description="Offboard a user (Part B, dry-run by default).")
    parser.add_argument("--user-principal-name", required=True)
    parser.add_argument(
        "--license-sku-id", required=True, help="SKU to reclaim (see scripts/list_license_skus.py)."
    )
    parser.add_argument("--operator", required=True, help="Human identity to attribute this run to.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Make real changes. Without this, the run is a dry run (default).",
    )
    args = parser.parse_args(argv)

    dry_run = not args.execute

    if not dry_run:
        if not _confirm_real_run(args.user_principal_name, confirm_fn=confirm_fn):
            record_audit_entry(
                AuditEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    operator=args.operator,
                    action="offboarding_aborted",
                    dry_run=False,
                    target=args.user_principal_name,
                    before=None,
                    after=None,
                    result="aborted",
                ),
                path=DEFAULT_AUDIT_LOG_PATH,
            )
            print("Confirmation did not match - aborted, no changes made.")
            return 1

    if dry_run:
        # Dry run never calls Graph (see offboard_user) - no reason to force
        # a real auth round-trip just to plan and audit what *would* happen.
        client = GraphClient(access_token="unused-in-dry-run")
    else:
        try:
            config = load_graph_config()
            token = get_access_token(config)
        except RuntimeError as exc:
            logger.error(str(exc))
            return 1
        client = GraphClient(access_token=token)

    result = offboard_user(
        client,
        user_principal_name=args.user_principal_name,
        license_sku_id=args.license_sku_id,
        operator=args.operator,
        dry_run=dry_run,
    )

    if dry_run:
        print(f"Dry run complete - {len(result.entries)} action(s) planned, none executed.")
    else:
        print(
            f"Offboarding complete - user id {result.user_id}, "
            f"removed from {len(result.groups_removed)} group(s), "
            f"{len(result.entries)} action(s) total."
        )
        print(
            "Mailbox conversion to shared is NOT automated - see the audit "
            "trail's convert_mailbox_to_shared entry and handle it in Exchange."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
