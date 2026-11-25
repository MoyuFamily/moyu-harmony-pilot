"""Keep the offline core suite within its reviewed case budget."""

from collections import Counter

import pytest

MAX_CORE_CASES = 300
MAX_PARAMETER_CASES = 5


def pytest_collection_finish(session: pytest.Session) -> None:
    count = len(session.items)
    if count > MAX_CORE_CASES:
        raise pytest.UsageError(
            f"Core suite collects {count} cases, exceeding the {MAX_CORE_CASES}-case limit. "
            "Replace redundant coverage before adding cases. See AGENTS.md."
        )
    groups = Counter(item.nodeid.split("[", 1)[0] for item in session.items)
    oversized = [
        f"{name} ({count})"
        for name, count in groups.items()
        if count > MAX_PARAMETER_CASES
    ]
    if oversized:
        raise pytest.UsageError(
            f"A test may collect at most {MAX_PARAMETER_CASES} representative cases. "
            "Reduce these parameter matrices: " + ", ".join(oversized)
        )
