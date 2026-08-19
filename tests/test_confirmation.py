"""Tests for the shared typed-confirmation gate.

Pure logic, no Graph, no stdin. All names below are fictional placeholders.
"""

from __future__ import annotations

from identity_audit.confirmation import confirm_retype


def test_confirm_retype_true_on_exact_match():
    assert confirm_retype(
        "fictional.user@example.test",
        "prompt text",
        confirm_fn=lambda prompt: "fictional.user@example.test",
    ) is True


def test_confirm_retype_false_on_mismatch_or_whitespace_only_difference_ignored():
    # A trailing/leading whitespace difference is stripped and still matches...
    assert confirm_retype(
        "fictional.user@example.test",
        "prompt text",
        confirm_fn=lambda prompt: "  fictional.user@example.test  ",
    ) is True
    # ...but an actual mismatch does not.
    assert confirm_retype(
        "fictional.user@example.test",
        "prompt text",
        confirm_fn=lambda prompt: "not-the-right-value",
    ) is False
