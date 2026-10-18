"""Research Review Gate：把“这份回测能不能支持结论”变成可复核的对象。

设计原则：
- 只使用看得见的事实（manifest、数据摘要、成交、成本、资金费、试验次数）；
- 严重问题用 block，明确阻断保存/晋级；
- 证据不足用 unknown，不假装通过；
- 每个问题都带证据，前端可以跳到原始记录。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _item(
    key: str,
    title: str,
    severity: str,
    detail: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": key,
        "title": title,
        "severity": severity,
        "ok": severity == "info",
        "detail": detail,
        "evidence": evidence or {},
    }


def build_review(
    *,
    metrics: dict[str, Any] | None,
    source: str = "",
    datasets: list[dict[str, Any]] | None = None,
    funding: list[dict[str, Any]] | None = None,
    contract_specs: dict[str, Any] | None = None,
    simple_benchmark: dict[str, Any] | None = None,
    trial_count: int | None = None,
    dsr: float | None = None,
    cost_audit: dict[str, Any] | None = None,
    redteam_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m = metrics or {}
    datasets = datasets or []
    funding = funding or []
    items: list[dict[str, Any]] = []

    # 1. 数据覆盖与来源
    if not datasets:
        items.append(_item("data", "数据摘要", "block", "回测没有数据摘要，无法确认用了哪份行情"))
    else:
        synthetic = [d for d in datasets if d.get("source") == "synthetic"]
        missing_digest = [d for d in datasets if not d.get("digest")]
        if synthetic:
            items.append(
                _item(
                    "data.synthetic",
                    "合成数据",
                    "warn",
                    "本次使用合成数据：只能验证代码与流程，不能据此判断策略好坏",
                    evidence={"symbols": [d.get("symbol") for d in synthetic]},
                )
            )
        if missing_digest:
            items.append(
                _item(
                    "data.digest",
                    "数据摘要",
                    "block",
                    "部分数据集没有 digest，无法复现",
                    evidence={"count": len(missing_digest)},
                )
            )
        if not synthetic and not missing_digest:
            items.append(
                _item(
                    "data",
                    "数据摘要",
                    "info",
                    f"{len(datasets)} 个数据集均有来源与 digest",
                    evidence={"datasets": datasets[:8]},
                )
            )

    # 2. 交易样本量
    trades = int(m.get("trades") or 0)
    if trades == 0:
        items.append(_item("trades", "交易样本", "block", "没有完成交易，当前结果不能支持策略结论"))
    elif trades < 10:
        items.append(_item("trades", "交易样本", "block", f"只有 {trades} 笔完成交易，统计意义不足"))
    elif trades < 30:
        items.append(_item("trades", "交易样本", "warn", f"只有 {trades} 笔完成交易，胜率与夏普误差较大"))
    else:
        items.append(_item("trades", "交易样本", "info", f"{trades} 笔完成交易"))

    # 3. 基准比较
    br = m.get("benchmark_return")
    tr = m.get("total_return")
    if br is None or tr is None:
        items.append(_item("benchmark", "基准比较", "unknown", "缺少基准或策略收益，无法判断是否跑赢买入持有"))
    elif tr <= br:
        items.append(
            _item(
                "benchmark",
                "基准比较",
                "warn",
                f"策略收益 {tr:.2%} 未超过买入持有 {br:.2%}",
                evidence={"strategy": tr, "buy_and_hold": br},
            )
        )
    else:
        items.append(_item("benchmark", "基准比较", "info", f"策略收益 {tr:.2%} 超过买入持有 {br:.2%}"))

    sbr = (simple_benchmark or {}).get("total_return")
    if sbr is not None and tr is not None and tr <= sbr:
        items.append(
            _item(
                "benchmark.simple",
                "简单规则基准",
                "warn",
                f"策略收益 {tr:.2%} 未超过 SMA 20/60 基线 {sbr:.2%}",
                evidence={"strategy": tr, "simple_rule": sbr},
            )
        )

    # 4. 成本与多重检验
    if cost_audit:
        if cost_audit.get("consistent") is False:
            items.append(
                _item(
                    "cost", "成本一致性", "block", "成本压力回测的扣费明细对不上，不能据此下结论", evidence=cost_audit
                )
            )
        else:
            items.append(_item("cost", "成本一致性", "info", "基础成本与压力成本扣费一致"))
    elif int(m.get("fills") or 0) > 0 and m.get("total_fees") == 0:
        items.append(_item("cost", "成本假设", "block", "有成交但手续费为 0，成本假设不完整"))
    else:
        items.append(_item("cost", "成本压力", "unknown", "本次回测没有附带成本压力扫描；结论只能视为初步"))

    if trial_count and trial_count > 1:
        if dsr is not None and dsr < 0.95:
            items.append(
                _item(
                    "multiple_testing",
                    "多重检验",
                    "block",
                    f"共 {trial_count} 次试验，DSR {dsr:.2f} < 0.95，需扩大样本或降低试验次数",
                    evidence={"trials": trial_count, "dsr": dsr},
                )
            )
        else:
            items.append(
                _item(
                    "multiple_testing",
                    "多重检验",
                    "warn",
                    f"共 {trial_count} 次试验；最好成绩必须按试验次数修正",
                    evidence={"trials": trial_count, "dsr": dsr},
                )
            )
    else:
        items.append(_item("multiple_testing", "多重检验", "unknown", "未找到可以核对的试验次数，无法确认是否反复调参"))

    # 5. 永续专属：资金费与合约规格
    perp = any(str(d.get("symbol", "")).upper().endswith(":PERP") for d in datasets)
    if perp:
        if not funding:
            items.append(_item("funding", "资金费数据", "block", "永续回测没有资金费数据，资金费按 0 计，结果不可信"))
        else:
            missing_marks = [f for f in funding if not f.get("mark_prices")]
            items.append(
                _item(
                    "funding",
                    "资金费数据",
                    "warn" if missing_marks else "info",
                    "资金费已接入；标记价缺失的合约回退到 bar 价格"
                    if missing_marks
                    else "资金费来源、结算时刻与标记价已记录",
                    evidence={"funding": funding[:8]},
                )
            )
        specs = contract_specs or {}
        if not specs:
            items.append(
                _item("contract", "合约规格", "block", "永续回测缺少合约规格（MMR/强平费用/杠杆），无法复核强平")
            )
        else:
            items.append(
                _item(
                    "contract",
                    "合约规格",
                    "info",
                    "已记录合约类型、MMR 档位、强平费用与滑点假设",
                    evidence={"specs": specs},
                )
            )
        items.append(
            _item(
                "contract.scope",
                "合约范围",
                "warn",
                "当前仅支持 Binance USDT-M 线性永续、逐仓、单向、简化强平；全仓/对冲/反向合约未支持",
            )
        )

    # 6. 红队规则问题
    for p in redteam_points or []:
        sev = "block" if p.get("severity") == "high" else "warn"
        items.append(_item(f"redteam.{len(items)}", p.get("title") or "红队问题", sev, p.get("detail") or ""))

    # 7. 无法自动验证的偏差：明确写 unknown，而不是假装通过
    items.append(
        _item("survivorship", "幸存者偏差", "unknown", "当前回测使用当前标的池，未自动验证退市/摘牌与历史成分")
    )
    items.append(
        _item(
            "lookahead", "前视偏差", "unknown", "平台已限制 history 只返回已收盘数据；仍需人工复核策略是否用了未来信息"
        )
    )
    items.append(
        _item("out_of_sample", "样本外验证", "unknown", "本次回测未附带 walk-forward / holdout 结果；保存前应补做")
    )

    blocks = [i for i in items if i["severity"] == "block"]
    warns = [i for i in items if i["severity"] == "warn"]
    unknowns = [i for i in items if i["severity"] == "unknown"]
    if blocks:
        verdict = "fail"
    elif unknowns:
        verdict = "insufficient"
    elif warns:
        verdict = "conditional"
    else:
        verdict = "pass"
    key = json.dumps({"items": items, "metrics": m}, sort_keys=True, ensure_ascii=False, default=str)
    return {
        "id": f"rv-{hashlib.sha256(key.encode()).hexdigest()[:10]}",
        "verdict": verdict,
        "blocks_promotion": bool(blocks),
        "summary": {
            "block": len(blocks),
            "warn": len(warns),
            "unknown": len(unknowns),
            "pass": len(items) - len(blocks) - len(warns) - len(unknowns),
        },
        "items": items,
        "generated_at": __import__("time").time(),
        "model": "deterministic research review",
    }


def latest_review(store: Any, slug: str) -> dict[str, Any] | None:
    """取某策略最近一次成功回测附加的审查卡。"""
    from ..store import Collections

    rows = [r for r in store.list(Collections.BACKTESTS) if r.get("slug") == slug and r.get("status") == "done"]
    rows.sort(key=lambda r: r.get("created_at") or 0)
    for row in reversed(rows):
        rv = row.get("review")
        if rv:
            return rv
    return None


__all__ = ["build_review", "latest_review"]
