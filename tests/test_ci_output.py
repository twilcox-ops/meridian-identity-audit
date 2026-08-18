"""Tests for CI-aware console output suppression.

Proves that `CI=true` (set automatically by GitHub Actions) suppresses the
detailed per-item console block each check would otherwise print - real
UPNs/display names would land in a GitHub Actions run log otherwise - while
leaving local interactive output unchanged when `CI` is unset. Fake data
only; all placeholders below are fictional, not real tenant identities.
"""

from __future__ import annotations

from identity_audit.ci import is_ci_environment
from identity_audit.run import _print_check_block


def test_is_ci_environment_true_for_true_variants(monkeypatch):
    for value in ("true", "True", "TRUE", " true "):
        monkeypatch.setenv("CI", value)
        assert is_ci_environment() is True


def test_is_ci_environment_false_when_unset_or_not_true(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    assert is_ci_environment() is False

    monkeypatch.setenv("CI", "false")
    assert is_ci_environment() is False

    monkeypatch.setenv("CI", "")
    assert is_ci_environment() is False


def test_print_check_block_suppressed_under_ci(monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")

    _print_check_block(
        "\nFictional finding type (2):",
        ["fictional.user.one@example.test", "fictional.user.two@example.test"],
        lambda item: f"  {item}",
    )

    out = capsys.readouterr().out
    assert out == ""


def test_print_check_block_full_detail_outside_ci(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)

    _print_check_block(
        "\nFictional finding type (2):",
        ["fictional.user.one@example.test", "fictional.user.two@example.test"],
        lambda item: f"  {item}",
    )

    out = capsys.readouterr().out
    assert "Fictional finding type (2):" in out
    assert "fictional.user.one@example.test" in out
    assert "fictional.user.two@example.test" in out


def test_print_check_block_handles_empty_items_outside_ci(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)

    _print_check_block("\nFictional finding type (0):", [], lambda item: f"  {item}")

    out = capsys.readouterr().out
    assert out.strip() == "Fictional finding type (0):"
