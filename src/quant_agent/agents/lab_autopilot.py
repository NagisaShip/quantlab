"""AI 研究室的自动迭代与版本结算。

把“调研 → 生成 → 回测 → 改进 → 最终成本复核”的状态机从主类拆出。函数接收研究室实例，
继续使用同一套 store、工具、消息卡片和审批/停止语义。
"""

from __future__ import annotations

import json
from typing import Any

from . import lab_goal as G
from .strategy_lab import (
    _call_market,
    _history_metrics,
    _hopeless,
    _latest_eval,
    _latest_metrics,
    _range_text,
    metrics_text,
    rule_revise,
    smoke_test,
)


def autopilot(
    lab,
    pid: str,
    *,
    rounds: int | None = None,
    research: str = "none",
    team_id: str | None = None,
    force: bool = False,
) -> None:
    project = lab.store.get(pid) or {}
    if project.get("market_auto") or not project.get("securities"):
        lab._post(pid, role="event", kind="error", content="请先设置标的与周期，再开始自动迭代")
        return
    items = G.project_items(project)
    spec = G.project_spec(project)
    # 开始前先看目标是否合理：明显不合理就停下来问，而不是带着不可能的目标空转几轮
    check = G.check_spec(spec, history=_history_metrics(project))
    if check["verdict"] == "unreasonable" and not force and project.get("permission") != "bypass":
        lab._post(
            pid,
            role="event",
            kind="goal",
            content="目标需要调整",
            data={
                "stage": "check",
                "spec": spec,
                "range": _range_text(spec),
                "check": check,
                "resume": {"rounds": rounds, "research": research, "team_id": team_id},
            },
        )
        lab._ask(
            pid,
            f"开始前检查了一下目标：{check['summary']}要怎么处理？",
            ["按建议修改后开始", "仍然开始", "先不开始"],
            decision=True,
        )
        return
    max_rounds = max(1, min(int(rounds or project.get("max_rounds") or 4), 8))
    copilot = lab._copilot(project)
    lab._post(
        pid,
        role="event",
        kind="round",
        content="开始自动迭代",
        data={
            "stage": "start",
            "goals": project.get("goals"),
            "goals_text": G.items_text(items),
            "rounds": max_rounds,
            "range": _range_text(spec),
            "research": research,
        },
    )

    # ---- 0. 调研 ----
    research_notes = ""
    if research in ("market", "team"):
        lab._phase(pid, "调研行情")
        sym, tf = project["securities"][0], project["timeframe"]
        summary = lab._tool(
            pid,
            "get_market_summary",
            "调研：行情摘要",
            {"标的": sym, "周期": tf, "K 线": int(project.get("bars_limit") or 1500)},
            lambda: _call_market(lab.tools.market, sym, tf, int(project.get("bars_limit") or 1500)),
        )
        research_notes += f"行情摘要：{json.dumps(summary, ensure_ascii=False, default=str)[:1500]}\n"
    if research == "team" and lab.tools.team is not None:
        lab._check_stop(pid)
        topic = (
            f"为这个想法做策略调研：{project.get('idea') or project.get('title')}。"
            "给出适配的策略类型、关键参数区间与主要风险。"
        )
        out = lab._convene(pid, topic, team_id=team_id)
        research_notes += f"团队调研结论：{out.get('synthesis', '')[:2000]}\n"

    # ---- 1..N 轮 ----
    history: list[str] = []
    scores: list[float] = []
    for r in range(1, max_rounds + 1):
        lab._check_stop(pid)
        project = lab.store.get(pid) or {}
        code = project.get("code") or ""
        last = _latest_eval(project)
        if not code:
            lab._phase(pid, f"第 {r} 轮 · 生成代码")
            idea = project.get("idea") or project.get("title")
            desc = (
                idea
                + (f"\n\n调研参考：\n{research_notes}" if research_notes else "")
                + "".join(f"\n\n{p['title']}：{p['text'][:1500]}" for p in project.get("pins") or [])
            )
            # 同样把循环变量绑定进闭包：`_tool` 目前是同步调用 `fn()`，
            # 晚绑定暂时不出错，但一旦改成延迟执行就会静默用错标的。
            result = lab._tool(
                pid,
                "generate",
                f"第 {r} 轮：生成策略",
                {"想法": idea[:40]},
                lambda desc=desc, secs=project["securities"], tf=project["timeframe"]: copilot.generate(
                    desc, securities=secs, timeframe=tf
                ),
            )
            note = "模板降级生成" if result.mode == "fallback" else "根据想法生成"
            if result.content:
                lab._post(
                    pid, role="assistant", kind="text", content=result.content, mode=result.mode, model=result.model
                )
            if not result.code or not (result.checks or {}).get("ok"):
                lab._post(pid, role="event", kind="error", content="生成的代码没有通过平台检查，自动迭代停止。")
                return
            lab._commit(pid, result.code, note=note, mode=result.mode, model=result.model, before="")
        elif last is not None and last.get("passed") and r == 1:
            lab._post(pid, role="event", kind="text", content="当前版本已经达标，直接进入压力测试。")
            break
        else:
            lab._phase(pid, f"第 {r} 轮 · 改进代码")
            evaluation = last or G.evaluate_items(None, items)
            failing = [
                f"{i['label']} 目标 {i['op']} {G.fmt_value(i['metric'], i['target'])}"
                f"{'（尽量）' if i['priority'] == 'try' else ''}，"
                f"实际 {G.fmt_value(i['metric'], i['actual'])}"
                for i in evaluation["items"]
                if not i["ok"]
            ]
            instruction = (
                f"这是自动迭代第 {r} 轮。最近一次平台回测：{metrics_text(_latest_metrics(project))}。\n"
                f"未达成的验收目标：{'；'.join(failing) or '无'}。\n"
                + ("之前几轮尝试过：\n" + "\n".join(history[-4:]) + "\n" if history else "")
                + "请分析原因并有针对性地改进策略逻辑或参数，避免简单堆参数导致过拟合，保持风险约束。"
                + (f"\n调研参考：{research_notes[:1500]}" if research_notes else "")
            )
            # 用默认参数把 code / instruction / project 的值**绑定**进闭包：
            # 它们是循环变量，每轮都会被重新赋值，晚绑定的闭包会读到最后一轮的值。
            result = lab._tool(
                pid,
                "revise",
                f"第 {r} 轮：改进策略",
                {"未达标": len(failing)},
                lambda code=code, instruction=instruction, secs=project["securities"], tf=project["timeframe"]: (
                    copilot.edit(code, instruction, securities=secs, timeframe=tf)
                ),
            )
            if result.mode == "llm" and result.code and (result.checks or {}).get("ok"):
                if result.content:
                    lab._post(
                        pid, role="assistant", kind="text", content=result.content, mode="llm", model=result.model
                    )
                lab._commit(pid, result.code, note=f"第 {r} 轮改进", mode="llm", model=result.model, before=code)
                first_line = (result.content or "").splitlines()[0][:80] if result.content else "模型修改"
                history.append(f"- 第 {r} 轮：{first_line}")
            else:
                new_code, notes = rule_revise(code, evaluation, r)
                if new_code == code:
                    lab._post(
                        pid,
                        role="assistant",
                        kind="text",
                        mode="fallback",
                        content=(
                            "**规则降级**：参数规则已经没有可调的方向了。可以配置模型让 AI 改策略逻辑，"
                            "或者召集团队讨论换一个策略类型。"
                        ),
                    )
                    break
                lab._post(
                    pid,
                    role="assistant",
                    kind="text",
                    mode="fallback",
                    content="**规则降级改进**（未调用大模型）：\n\n" + "\n".join(f"- {n}" for n in notes),
                )
                lab._commit(pid, new_code, note=f"第 {r} 轮规则调参", mode="fallback", model="", before=code)
                history.append(f"- 第 {r} 轮：" + "；".join(notes)[:120])

        lab._check_stop(pid)
        lab._phase(pid, f"第 {r} 轮 · 平台回测")
        bt = lab.backtest_current(pid, label=f"策略室自动迭代 第{r}轮")
        if not bt.get("ok"):
            return
        ev = bt["evaluation"]
        lab._post(
            pid,
            role="event",
            kind="round",
            content=f"第 {r} 轮结束",
            data={
                "stage": "round",
                "round": r,
                "v": (lab.store.get(pid) or {}).get("current_v"),
                "passed": ev["passed"],
                "items": ev["items"],
                "metrics": bt.get("metrics"),
            },
        )
        if ev["passed"]:
            break
        scores.append(ev["score"])
        if reason := _hopeless(ev, scores, r):
            lab._post(
                pid,
                role="event",
                kind="goal",
                content="提前结束迭代",
                data={"stage": "early_stop", "reason": reason, "gaps": G.gap_report(ev)},
            )
            break

    # ---- 收尾：选最佳版本 + 压力测试 + 询问 ----
    lab._finish(pid)


def _commit(lab, pid: str, code: str, *, note: str, mode: str, model: str, before: str) -> None:
    project = lab.store.get(pid) or {}
    checks = smoke_test(code, project.get("securities"), project.get("timeframe") or "1h")
    row = lab.add_version(
        pid, code, source="fallback" if mode == "fallback" else "ai", note=note, checks=checks, model=model
    )
    lab._post(
        pid,
        role="tool",
        kind="code",
        content=note,
        data={
            "v": row["v"],
            "before": before,
            "after": code,
            "params": row["params"],
            "fills": checks.get("fills"),
        },
    )


def _finish(lab, pid: str) -> None:
    project = lab.store.get(pid) or {}
    scored = [r for r in project.get("versions") or [] if r.get("evaluation")]
    if not scored:
        return
    best = max(scored, key=lambda r: r["evaluation"]["score"])
    if best["v"] != project.get("current_v"):
        lab.restore(pid, best["v"], note="自动迭代：回到成绩最好的一版")
    lab._check_stop(pid)
    lab._phase(pid, "压力测试")
    stress = lab.backtest_current(pid, cost_scale=2.0, label=f"策略室 v{best['v']} 2倍成本")
    warn = []
    base = best.get("metrics") or {}
    if stress.get("ok"):
        sm = stress.get("metrics") or {}
        if (sm.get("total_return") or 0) < 0 <= (base.get("total_return") or 0):
            warn.append("2 倍成本下收益转负：策略对手续费/滑点非常敏感")
        elif (base.get("sharpe") or 0) > 0 and (sm.get("sharpe") or 0) < (base.get("sharpe") or 0) * 0.5:
            warn.append("2 倍成本下夏普下降超过一半")
    if len(best.get("params") or {}) >= 7:
        warn.append(f"参数多达 {len(best['params'])} 个，注意过拟合")
    if (base.get("trades") or 0) < 10:
        warn.append("交易笔数少于 10，统计意义有限")
    if best.get("formal") is False:
        warn.append("数据核验未通过（覆盖不足或数据不新鲜），这是非正式回测，不能据此判断策略是否有效")
    passed = best["evaluation"]["passed"]
    lab._post(
        pid,
        role="event",
        kind="round",
        content="迭代完成",
        data={
            "stage": "done",
            "best_v": best["v"],
            "passed": passed,
            "items": best["evaluation"]["items"],
            "metrics": base,
            "stress": stress.get("metrics") if stress.get("ok") else None,
            "warnings": warn,
        },
    )
    verdict = "已达成全部验收目标" if passed else "还没有全部达标"
    tail = f"{'；'.join(warn)}。" if warn else ""
    lab._ask(
        pid,
        f"迭代结束：v{best['v']} {verdict}。{tail}你对这个结果满意吗？",
        ["满意，保存为策略", "继续优化", "换个方向"],
        decision=True,
    )


def decide(lab, pid: str, choice: str, note: str = "") -> None:
    if lab._decide_permission(pid, choice, note):
        return
    msgs = [m for m in lab.store.messages(pid) if m["kind"] == "ask" and m["data"].get("answered") is None]
    open_ask = msgs[-1]["data"] if msgs else {}
    for m in msgs:
        lab.store.update(pid, m["seq"], data={"answered": choice})
    lab._answered(pid)
    lab._post(pid, role="user", kind="text", content=choice + (f"：{note}" if note else ""))
    text = choice + (f"。{note}" if note else "")
    # AI 用 ask_user 提的问题（例如选研究方向），或研究者自己写的回答：都交回模型接着做
    if not open_ask.get("decision") or choice not in (open_ask.get("options") or []):
        lab.chat(pid, text)
        return
    if choice.startswith("按建议修改") or choice == "仍然开始":
        pending = next(
            (
                m["data"].get("resume") or {}
                for m in reversed(lab.store.messages(pid))
                if m["kind"] == "goal" and m["data"].get("stage") == "check"
            ),
            {},
        )
        if choice != "仍然开始":
            project = lab.store.get(pid) or {}
            spec = G.project_spec(project)
            lab.set_goal(
                pid,
                G.apply_suggestions(spec, G.check_spec(spec, history=_history_metrics(project))),
                source="suggest",
            )
        lab.autopilot(
            pid,
            force=True,
            rounds=pending.get("rounds"),
            research=pending.get("research") or "none",
            team_id=pending.get("team_id"),
        )
        return
    if choice == "先不开始":
        lab._post(pid, role="assistant", kind="text", content="好的，先不开始。可以输入 `/goal` 重新设定目标。")
        return
    if choice.startswith("满意"):
        if lab.tools.save_strategy is None:
            return
        project = lab.store.get(pid) or {}
        rec = lab._tool(
            pid,
            "save_strategy",
            "保存为平台策略",
            {"版本": f"v{project.get('current_v')}"},
            lambda: lab.tools.save_strategy(project),
        )
        lab.store.patch(pid, slug=rec.get("slug"))
        lab._post(pid, role="event", kind="saved", content="已保存为策略草稿", data=rec)
    elif choice.startswith("继续"):
        if note:
            lab._chat_or_note(pid, note)
        lab.autopilot(pid, rounds=2)
    elif choice.startswith("换"):
        if note:
            lab.store.patch(pid, idea=note)
            project = lab.store.get(pid) or {}
            lab.pin(pid, kind="note", title="研究者的新方向", text=note)
            lab._post(
                pid,
                role="assistant",
                kind="text",
                content=f"好的，按新方向重新开始：{note}。我会保留之前的版本以便对比。",
            )
            lab.store.patch(pid, code="", current_v=project.get("current_v"))
            lab.autopilot(pid)
        else:
            # 没写新方向：让模型基于目前结果提几个方向给研究者点选
            lab.chat(pid, "我想换个研究方向。请结合目前的结果提出 2-3 个明显不同的新方向，用 ask_user 让我选。")
    else:
        lab.chat(pid, text)


def restore(lab, pid: str, v: int, *, note: str = "") -> dict[str, Any] | None:
    project = lab.store.get(pid) or {}
    row = next((r for r in project.get("versions") or [] if r["v"] == v), None)
    if row is None:
        return None
    lab.store.patch(pid, code=row["code"], current_v=v)
    lab._post(pid, role="event", kind="text", content=note or f"已切换到 v{v}")
    return row
