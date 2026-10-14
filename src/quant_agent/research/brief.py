"""AI 晨报：每天汇总市场状态、库内因子与模型健康度、组合与执行、夜班成果、等你决定的事。

数据由 API 层收集好传进来；这里只负责排版成几个小节 + 一段可推送的纯文本。
有模型时可以再让模型写一句"今天最值得关注的事"，没有模型时用规则挑。只陈述数据，不预测行情。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

BRIEFS = "rd_briefs"


def _pct(v: Any, d: int = 1) -> str:
    return "—" if v is None else f"{float(v) * 100:.{d}f}%"


def _num(v: Any, d: int = 2) -> str:
    return "—" if v is None else f"{float(v):.{d}f}"


def compose(data: dict[str, Any]) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    focus: list[tuple[int, str]] = []  # (优先级, 一句话)

    m = data.get("market") or {}
    rows = m.get("rows") or []
    if rows:
        states: dict[str, int] = {}
        for r in rows:
            states[r.get("state") or "—"] = states.get(r.get("state") or "—", 0) + 1
        lines = ["市场状态：" + "，".join(f"{k} {v} 个" for k, v in sorted(states.items(), key=lambda x: -x[1]))]
        movers = sorted(rows, key=lambda r: -abs(r.get("ret_24h") or 0))[:3]
        lines.append(
            "24 小时波动最大：" + "，".join(f"{r['symbol'].split('/')[0]} {_pct(r.get('ret_24h'))}" for r in movers)
        )
        for a in (m.get("alerts") or [])[:4]:
            lines.append(f"异动：{a}")
            focus.append((2, a))
        sections.append(
            {
                "key": "market",
                "title": "市场",
                "lines": lines,
                "tone": "warn" if m.get("alerts") else "",
                "note": "含合成行情" if m.get("synthetic") else "",
            }
        )

    f = data.get("factors") or {}
    if f.get("total"):
        lines = [f"入库 {f.get('active', 0)} 个，候选 {f.get('candidate', 0)} 个，退役 {f.get('retired', 0)} 个"]
        if f.get("decaying"):
            lines.append("衰减预警：" + "、".join(f["decaying"][:6]))
            focus.append((1, f"{len(f['decaying'])} 个入库因子出现衰减：{'、'.join(f['decaying'][:3])}"))
        sections.append(
            {"key": "factors", "title": "因子库", "lines": lines, "tone": "warn" if f.get("decaying") else ""}
        )

    md = data.get("models") or {}
    if md.get("total"):
        lines = [f"上线 {md.get('active', 0)} 个，候选 {md.get('candidate', 0)} 个"]
        for x in (md.get("flagged") or [])[:5]:
            lines.append(f"{x['name']}：{x['why']}")
            focus.append((1, f"模型 {x['name']} {x['why']}"))
        sections.append(
            {"key": "models", "title": "模型库", "lines": lines, "tone": "warn" if md.get("flagged") else ""}
        )

    pf = data.get("portfolio") or {}
    if pf.get("schemes"):
        lines = []
        for x in pf["schemes"][:4]:
            lines.append(
                f"{x['name']}：夏普 {_num(x.get('sharpe'))}，最大回撤 {_pct(x.get('max_drawdown'))}，"
                f"年化波动 {_pct(x.get('ann_vol'))}"
                + (f"（目标 {_pct(x.get('vol_target'), 0)}）" if x.get("vol_target") else "")
            )
            if x.get("vol_target") and x.get("ann_vol") and x["ann_vol"] > x["vol_target"] * 1.2:
                focus.append((2, f"组合 {x['name']} 实际波动高出目标 20% 以上"))
        sections.append({"key": "portfolio", "title": "组合与风控", "lines": lines})

    ex = data.get("execution") or {}
    if ex.get("runs"):
        lines = [
            f"最近 {ex['runs']} 次执行仿真平均成本 {_num(ex.get('avg_is_bps'))} bp，比 TWAP "
            f"省 {_num(ex.get('avg_beat_twap'))} bp"
        ]
        if ex.get("active"):
            lines.append("启用中的执行方案：" + "、".join(ex["active"][:4]))
        sections.append({"key": "execution", "title": "执行", "lines": lines})

    pp = data.get("paper") or {}
    if pp.get("running"):
        lines = [f"实时模拟运行中 {pp['running']} 个"]
        for x in (pp.get("sessions") or [])[:4]:
            lines.append(f"{x['name']}：累计 {_pct(x.get('ret'))}")
        for x in (pp.get("shadows") or [])[:3]:
            lines.append(f"影子运行 {x['label']}：{x['verdict']}")
            if x.get("ready"):
                focus.append((1, f"影子运行 {x['label']} 已满足替换条件，等你决定"))
        sections.append({"key": "paper", "title": "实时模拟", "lines": lines})

    wt = data.get("watcher") or {}
    if wt.get("watched"):
        flagged = wt.get("flagged") or []
        lines = [
            f"值守 {wt['watched']} 个实验，{len(flagged)} 条需要留意"
            if flagged
            else f"值守 {wt['watched']} 个实验，没有发现和说明书矛盾的行为"
        ]
        for x in flagged[:4]:
            lines.append(f"{x['name']}：{x['text']}")
        if flagged:
            focus.append(
                (0 if flagged[0]["level"] == "error" else 1, f"值守员：{flagged[0]['name']} {flagged[0]['text']}")
            )
        sections.append(
            {
                "key": "watcher",
                "title": "AI 值守员",
                "lines": lines,
                "tone": "warn" if flagged else "ok",
                "note": "只看不动，处理由你决定",
            }
        )

    sh = data.get("shift")
    if sh:
        items = sh.get("items") or []
        lines = [
            f"{time.strftime('%m-%d %H:%M', time.localtime(sh.get('started_at') or 0))} 开始，"
            f"跑了 {len(items)} 个任务，"
            f"共 {int((sh.get('used') or {}).get('trials') or 0)} 个版本"
            + (f"，停止原因：{sh['stop_reason']}" if sh.get("stop_reason") else "")
        ]
        good = 0
        for it in items[:6]:
            mark = "✓" if it.get("passed") else "·"
            good += 1 if it.get("passed") else 0
            lines.append(f"{mark} {it.get('headline') or it.get('idea', '')[:40]}")
        if good:
            focus.append((0, f"夜班有 {good} 个成果达到验收目标，等你决定是否启用"))
        sections.append({"key": "shift", "title": "夜班成果", "lines": lines, "tone": "ok" if good else ""})

    lg = data.get("ledger") or {}
    if lg:
        lines = [f"累计试验 {lg.get('total', 0)} 次，过去 24 小时新增 {lg.get('last_24h', 0)} 次"]
        for w in (lg.get("warnings") or [])[:3]:
            lines.append(w)
        if lg.get("holdout"):
            lines.append(lg["holdout"])
        sections.append(
            {"key": "ledger", "title": "研究纪律", "lines": lines, "tone": "warn" if lg.get("warnings") else ""}
        )

    aw = data.get("awaiting") or []
    if aw:
        sections.append(
            {
                "key": "awaiting",
                "title": "等你决定",
                "lines": [x["title"] for x in aw[:5]] + ([f"还有 {len(aw) - 5} 个"] if len(aw) > 5 else []),
                "tone": "warn",
                "note": "在 AI 研究室里回答",
            }
        )
        focus.append((0, f"{len(aw)} 个研究在等你决定"))

    focus.sort(key=lambda x: x[0])
    headline = focus[0][1] if focus else "没有需要特别处理的事。"
    day = time.strftime("%Y-%m-%d", time.localtime(data.get("at") or time.time()))
    text_lines = [f"【研究晨报】{day}", f"重点：{headline}"]
    for s in sections:
        text_lines.append(f"· {s['title']}：{s['lines'][0]}")
    text_lines.append("仅为研究结果，非投资建议。")
    return {
        "title": f"研究晨报 · {day}",
        "headline": headline,
        "focus": [x[1] for x in focus[:5]],
        "sections": sections,
        "text": "\n".join(text_lines),
    }


def save(store: Any, brief: dict[str, Any], *, source: str, pushed: list[str] | None = None) -> dict[str, Any]:
    rec = {"id": f"b_{uuid.uuid4().hex[:8]}", "at": time.time(), "source": source, "pushed": pushed or [], **brief}
    store.upsert(BRIEFS, rec)
    # 只留最近 60 份
    rows = sorted(store.list(BRIEFS), key=lambda r: -(r.get("at") or 0))
    for old in rows[60:]:
        store.delete(BRIEFS, old["id"])
    return rec


def latest(store: Any) -> dict[str, Any] | None:
    rows = sorted(store.list(BRIEFS), key=lambda r: -(r.get("at") or 0))
    return rows[0] if rows else None


def history(store: Any, limit: int = 30) -> list[dict[str, Any]]:
    return [
        {k: r.get(k) for k in ("id", "at", "title", "headline", "source", "pushed")}
        for r in sorted(store.list(BRIEFS), key=lambda r: -(r.get("at") or 0))[:limit]
    ]


__all__ = ["BRIEFS", "compose", "history", "latest", "save"]
