from __future__ import annotations

from datetime import UTC, datetime

import pytest

from desk.clock import FakeClock
from desk.config import DEFAULT_CONFIG_PATH, load_config
from desk.db import Database


def pytest_addoption(parser):
    parser.addoption("--network", action="store_true", help="also run tests that call real public APIs")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def config():
    """The real config/config.yaml (so tests also prove it's valid), pinned to day mode
    and 5% per trade: the settings most tests were written for."""
    from tests.fakes import baseline_config
    return baseline_config()


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 9, 8, 0, 0, tzinfo=UTC), tz="Australia/Sydney")


@pytest.fixture
def db(tmp_path, clock):
    d = Database(tmp_path / "test.db", clock)
    d.migrate()
    yield d
    d.close()
