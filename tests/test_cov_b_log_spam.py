"""The rate-limited logger."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from custom_components.roomwise.log_spam_less import BermudaLogSpamLess

CLOCK = "custom_components.roomwise.log_spam_less.monotonic_time_coarse"


@pytest.fixture
def clock():
    with patch(CLOCK, return_value=1000.0) as fake:
        yield fake


def test_repeats_are_suppressed_then_reported_with_a_count(clock, caplog):
    log = BermudaLogSpamLess(logging.getLogger("spamtest"), 10)
    caplog.set_level(logging.DEBUG, logger="spamtest")

    log.debug("cov-b-repeat", "hello")
    log.debug("cov-b-repeat", "hello")
    log.debug("cov-b-repeat", "hello")
    assert [r.getMessage() for r in caplog.records] == ["hello"]

    clock.return_value = 1011.0
    log.debug("cov-b-repeat", "hello")
    assert caplog.records[-1].getMessage() == "hello (2 previous messages suppressed)"

    # The window restarts after a report.
    log.debug("cov-b-repeat", "hello")
    assert len(caplog.records) == 2


def test_an_expired_key_with_nothing_suppressed_logs_the_plain_message(clock, caplog):
    log = BermudaLogSpamLess(logging.getLogger("spamtest"), 10)
    caplog.set_level(logging.DEBUG, logger="spamtest")
    log.debug("cov-b-quiet", "once")
    clock.return_value = 1100.0
    log.debug("cov-b-quiet", "once")
    assert [r.getMessage() for r in caplog.records] == ["once", "once"]


@pytest.mark.parametrize(
    ("method", "level"),
    [("debug", logging.DEBUG), ("info", logging.INFO), ("warning", logging.WARNING), ("error", logging.ERROR)],
)
def test_each_level_logs_at_its_own_level_with_arguments(clock, caplog, method, level):
    log = BermudaLogSpamLess(logging.getLogger("spamtest"), 10)
    caplog.set_level(logging.DEBUG, logger="spamtest")
    getattr(log, method)(f"cov-b-level-{method}", "value is %s", 42)
    record = caplog.records[-1]
    assert record.levelno == level
    assert record.getMessage() == "value is 42"
