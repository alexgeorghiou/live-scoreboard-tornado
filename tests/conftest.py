import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("REDIS_URL"):
        return
    skip = pytest.mark.skip(reason="REDIS_URL not set; real Redis tests skipped")
    for item in items:
        if item.get_closest_marker("redis") is not None:
            item.add_marker(skip)
