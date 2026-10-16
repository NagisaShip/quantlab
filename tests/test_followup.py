"""Agent 主动跟进：后台任务结束后回到原对话汇报。"""

import contextvars

from quant_agent.agents import followup as F


class _Store:
    def __init__(self):
        self.project = {"id": "p1", "title": "趋势研究", "status": "idle"}
        self.msgs = []

    def get(self, pid):
        return self.project if pid == "p1" else None

    def append(self, pid, msg):
        self.msgs.append(msg)


class _Lab:
    def __init__(self):
        self.store = _Store()
        self.started = []

    def start(self, pid, kind, fn, wait=False):
        self.started.append((pid, kind))

    def chat(self, pid, text):
        pass


def test_followup_reports_when_finished_and_waits_while_busy(monkeypatch):
    state = {"done": False}
    monkeypatch.setattr(F, "_fetch", lambda tool, rid: (state["done"], {"best_params": {"fast": 12}} if state["done"] else {}))
    lab = _Lab()
    w = F._Watch(lab, "p1", "start_tuning", "run-1", contextvars.copy_context(), "u1")
    fu = F.FollowUps()
    assert fu._check(w) is False  # 还没结束
    state["done"] = True
    lab.store.project["status"] = "running"
    assert fu._check(w) is False  # 对话正忙：先不打断
    lab.store.project["status"] = "idle"
    assert fu._check(w) is True
    assert lab.started == [("p1", "chat")]
    assert any("【后台任务完成】" in m["content"] for m in lab.store.msgs if m["role"] == "user")


def test_watch_ignores_failed_starts_and_other_tools():
    fu = F.FollowUps()
    fu.watch(_Lab(), "p1", "start_tuning", {"error": "缺少参数"})
    fu.watch(_Lab(), "p1", "list_strategies", {"run_id": "x"})
    assert fu.pending("p1") == []
