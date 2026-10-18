"""研究导师课程：把研究方法拆成可检查的课程，而不是只给一段泛泛的提示词。

课程是确定性的、可离线运行的。前端记录完成进度；Agent 在解释回测时引用同一组
检查项，避免“导师说一套、审查卡另一套”。
"""

from __future__ import annotations

from typing import Any

MODULES: list[dict[str, Any]] = [
    {
        "id": "hypothesis",
        "title": "研究假设：先写清楚要验证什么",
        "summary": "把“我想赚钱”改写成可证伪的假设：市场状态、信号、预期方向和失效条件。",
        "checks": ["假设可证伪", "明确标的与时间范围", "预先写失效条件"],
        "practice": "用一句话写下：当 X 条件出现时，Y 标的在 Z 周期内应跑赢基准，否则假设错误。",
        "red_flags": ["看到回测结果后才补假设", "同时改变标的、周期和参数"],
    },
    {
        "id": "data",
        "title": "数据与时间：当时真的拿得到吗",
        "summary": "统一时区、K 线边界和字段可用时间；用 as-of 数据，不用未来才发布的数据。",
        "checks": ["交易所与标的明确", "时间戳单位统一", "按 available_at 对齐", "记录数据摘要"],
        "practice": "检查一份数据集的覆盖范围、缺口、重复和未收盘 bar。",
        "red_flags": ["把收盘后才能知道的字段用于当根开盘", "用当前成分股回看历史"],
    },
    {
        "id": "signal",
        "title": "信号构造：因果、可执行、可解释",
        "summary": "指标只用已收盘数据；信号到成交之间要有一天/一根 bar 的延迟。",
        "checks": ["无未来函数", "预热足够", "信号可复现", "交易频率合理"],
        "practice": "把一次交易从决策 bar、成交 bar、费用到持仓变化逐项写出来。",
        "red_flags": ["用当前 bar 收盘价决策又按同一收盘价成交", "参数多到无法解释"],
    },
    {
        "id": "backtest",
        "title": "回测与基准：别只看策略自己",
        "summary": "默认同时看买入持有和简单规则基准，并逐笔计入手续费、滑点、价差和资金费。",
        "checks": ["有基准曲线", "成本口径完整", "交易次数足够", "回撤与收益同时看"],
        "practice": "比较策略、买入持有、SMA 20/60 三条曲线，解释超额收益来自哪里。",
        "red_flags": ["只展示最好的参数", "手续费只算一边", "没有基准"],
    },
    {
        "id": "robustness",
        "title": "稳健性：样本外、滚动验证与多重检验",
        "summary": "把数据分成开发、验证和紧封测试；记录试验次数，修正最好成绩。",
        "checks": ["样本外", "Walk-forward", "参数敏感性", "多重检验修正"],
        "practice": "固定一次参数后，在不参与开发的时间段重跑并比较。",
        "red_flags": ["反复调参直到好看", "样本外只在最后看一次却改了策略", "忽略试验次数"],
    },
    {
        "id": "perp",
        "title": "永续合约：收益是否来自杠杆和资金费",
        "summary": "检查资金费结算时刻、标记价、MMR 档位、强平费用与逐仓损失上限。",
        "checks": ["资金费来源与结算时刻", "标记价", "MMR 档位", "强平原因与费用"],
        "practice": "用 10 倍杠杆急跌场景手算一次强平损失，再和报告核对。",
        "red_flags": ["用现货 K 线乘杠杆冒充合约回测", "用最新成交价判断强平", "忽略资金费"],
    },
    {
        "id": "execution",
        "title": "执行与上线：回测和真实成交差在哪",
        "summary": "区分纸面、交易所模拟盘和真实资金；逐笔归因滑点、延迟和部分成交。",
        "checks": ["模拟盘对账", "执行缺口", "只读账户", "停止与回滚路径"],
        "practice": "拿一份模拟盘成交与回测成交逐笔比较，找出最大的执行差异。",
        "red_flags": ["把模拟成交当成真实成交", "没有一键停止", "真实资金没有权限隔离"],
    },
]


def course_payload(completed: list[str] | None = None) -> dict[str, Any]:
    done = set(completed or [])
    lessons = [{**m, "completed": m["id"] in done} for m in MODULES]
    return {
        "title": "QuantLab 研究导师",
        "principle": "成功标准不是生成多少策略，而是研究过程是否严谨、结果是否可验证、实验能否重现。",
        "modules": lessons,
        "completed": sorted(done & {m["id"] for m in MODULES}),
        "total": len(MODULES),
        "progress": round(len(done & {m["id"] for m in MODULES}) / len(MODULES), 3),
    }


def lesson_ids() -> set[str]:
    return {m["id"] for m in MODULES}


__all__ = ["MODULES", "course_payload", "lesson_ids"]
