"""CI-environment detection.

GitHub Actions sets `CI=true` on every runner automatically (most other CI
systems follow the same convention). Used to gate console output that would
otherwise leak real tenant identities (UPNs, display names) into a CI log -
e.g. GitHub Actions run logs, readable by anyone with repo read access on a
public repo - while leaving local interactive runs unchanged.
"""

from __future__ import annotations

import os


def is_ci_environment() -> bool:
    """True if the `CI` environment variable is set to "true" (case-insensitive)."""
    return os.environ.get("CI", "").strip().lower() == "true"
