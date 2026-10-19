"""Disk quota expectations are independent of the production calculator."""

from types import SimpleNamespace

import pytest

from quant_agent.platform import backtest_retention as retention


@pytest.mark.parametrize(("total", "expected"), [(358400 * 50000 * 5, 50000), (358400 * 200000 * 5, 100000)])
def test_disk_total_capacity(monkeypatch, total, expected):
    monkeypatch.setattr(retention.shutil, "disk_usage", lambda _: SimpleNamespace(total=total, free=1))
    result = retention.workspace_hard_max()
    assert result["hard_max_units"] == expected
    assert result["disk_total_bytes"] == total


def test_disk_read_failure(monkeypatch):
    def fail(_):
        raise OSError("unavailable")

    monkeypatch.setattr(retention.shutil, "disk_usage", fail)
    assert retention.workspace_hard_max()["hard_max_units"] == 100000


def test_admin_lower_quota(monkeypatch):
    monkeypatch.setattr(retention.shutil, "disk_usage", lambda _: SimpleNamespace(total=358400 * 50000 * 5))
    auth = SimpleNamespace(workspace=lambda _: {"settings": {"quotas": {"backtest_max_records_per_user": 1234}}})
    assert retention.workspace_limit(auth, "workspace") == 1234
