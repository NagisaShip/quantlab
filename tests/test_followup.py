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
    monkeypatch.setattr(
        F, "_fetch", lambda tool, rid: (state["done"], {"best_params": {"fast": 12}} if state["done"] else {})
    )
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


def test_followup_does_not_start_a_turn_while_waiting_for_the_user(monkeypatch):
    monkeypatch.setattr(F, "_fetch", lambda tool, rid: (True, {"status": "done"}))
    lab = _Lab()
    lab.store.project["status"] = "awaiting_user"  # 审批卡还没回答
    w = F._Watch(lab, "p1", "start_stress_drill", "st_1", contextvars.copy_context(), "u1")
    assert F.FollowUps()._check(w) is True
    assert lab.started == []  # 不另起回合
    assert any("【后台任务完成】" in m["content"] for m in lab.store.msgs)


def test_agent_reading_the_result_itself_cancels_the_followup():
    fu = F.FollowUps()
    w = F._Watch(_Lab(), "p1", "start_tuning", "run-1", contextvars.copy_context(), "u1")
    fu._items.append(w)
    fu.seen("p1", "tuning_result", {"state": "RUNNING"})  # 还没结束：保留
    assert fu.pending("p1")
    fu.seen("p1", "tuning_result", {"best_params": {"fast": 14}})
    assert fu.pending("p1") == []


def test_watch_ignores_failed_starts_and_other_tools():
    fu = F.FollowUps()
    fu.watch(_Lab(), "p1", "start_tuning", {"error": "缺少参数"})
    fu.watch(_Lab(), "p1", "list_strategies", {"run_id": "x"})
    assert fu.pending("p1") == []
