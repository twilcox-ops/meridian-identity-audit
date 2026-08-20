# Bug write-up: rollback targeting its own prior output

## Summary

`--rollback`'s "most recent run for this user" lookup, `find_run_entries()`
in `src/identity_audit/rollback.py`, could select a *prior rollback's own*
audit entries as the run to reverse, instead of the original
onboarding/offboarding run those entries were meant to undo. The result
was double-prefixed, meaningless action names like
`rollback_rollback_create_user` — every one reported "not reversible"
since nothing in any reversal mapping matches that name.

## How it was discovered

Found via live testing against the real tenant, not by code inspection.

1. A dry-run rollback was executed against a user with a prior
   onboarding run in the audit log. Like every dry-run action in this
   project, it correctly wrote its own audit entries — in this case
   `rollback_*`-prefixed ones — to the same JSONL audit log the original
   run's entries live in.
2. A real `--execute` rollback was then run against the same user right
   after.
3. Instead of finding the original onboarding run, `find_run_entries()`
   found the *dry-run rollback's own entries* — because they were now the
   most recent ones logged for that user — and tried to reverse those.
4. Every resulting action name was double-prefixed
   (`rollback_rollback_create_user` and similar), and every one was
   reported "not reversible," since no reversal mapping has an entry for
   an action name like that.

Zero real Graph calls happened as a result, but nothing was actually
rolled back either — a silent no-op dressed up as normal-looking output.
That's exactly what made it easy to miss: nothing crashed, nothing threw
an error, the tool just quietly did nothing useful. It only surfaced
because the rollback was, by chance, run twice in a row against the same
user during testing.

## Root cause

`find_run_entries()` groups every audit-log entry for a given user by
timestamp, then either takes the run at an explicit `--timestamp` or
defaults to the most recent one (`max()` over the grouped timestamps).
The lookup never excluded entries whose `action` starts with the
`rollback_` prefix from that grouping. Since a dry-run rollback writes
real audit entries just like any other dry-run action, and always at a
timestamp later than the run it's reversing, a prior rollback attempt's
own log entries were always eligible to be selected as the *next*
rollback's target — and, being more recent, usually would be.

## The fix

Entries whose `action` starts with `rollback_` are now excluded entirely
at read time, before they're ever grouped into a candidate run — not
merely deprioritized or filtered out after the "most recent" run is
chosen. Concretely, in `find_run_entries()`'s line-by-line parse of the
audit log:

```python
if raw.get("action", "").startswith(ROLLBACK_ACTION_PREFIX):
    continue
```

This line runs before each entry is added to `entries_by_timestamp`, the
dict the "most recent" (`max()`) and explicit-`--timestamp` lookups both
read from. Filtering at the source this way closes both paths at once:

- The default "most recent run" path can no longer resolve to a
  rollback's own entries, since they're never added to the candidate
  pool.
- An explicit `--timestamp` that happens to land on a rollback run is
  blocked too, not just deprioritized — because rolling back a rollback
  isn't a meaningful operation this engine supports at all, not a case to
  merely avoid by default.

## Regression test

`test_rollback_never_targets_a_prior_rollbacks_own_entries` in
`tests/test_rollback.py` reproduces the exact scenario using the real
`run_rollback()` and `find_run_entries()` functions — not a hand-built
fixture standing in for them. It:

1. Writes an original action's audit entry to a temp audit log.
2. Runs a real dry-run rollback against that entry, at a later timestamp,
   and confirms — by reading the log back — that it genuinely did write
   a `rollback_`-prefixed entry (`rollback_fictional_disable`), the same
   way the live bug was produced.
3. Calls `find_run_entries()` exactly as a real `--rollback` invocation
   would (no explicit `--timestamp`, so "most recent for this user") and
   asserts it still resolves to the *original* run's timestamp and
   action name, not the rollback's own later entry.
4. Runs a real rollback using those correctly-found entries and confirms
   it actually reverses the original action end-to-end — proving the fix
   works at the full-flow level, not just inside the lookup function.

## Live verification

A real `--execute` rollback was run against the tenant after the fix, to
confirm it worked outside the test suite as well as inside it. It
correctly reversed both license assignment and user creation from an
earlier onboarding run (both reported `reversed`). Group removal was
reported `failed` — not a re-emergence of this bug: the user had already
been removed from that group by an intervening offboarding run against
the same user, confirmed directly with `scripts/check_group_membership.py`
rather than assumed. Trying to remove someone from a group they're
already not in is expected to fail, and the audit trail reporting that
plainly (rather than masking it as a success) is the system behaving
correctly under already-consistent state.

See the "Rollback" section of the [README](../README.md) for the feature
this bug was found in, and the full permissions/verification context
around it.
