"""因子库与试验台账。

- 因子库（collection ``factors``）：每个因子 = 表达式 + 假设卡 + 状态 + 最近一次评估摘要（完整报告存 blob）。
  状态：候选 candidate → 入库 active → 衰减预警 decaying → 退役 retired。
- 试验台账（collection ``research_trials``）：每评估一次记一笔（规范化表达式的哈希去重），
  用于显著性修正：同一个工作区里试得越多，入库门槛越高。
- 种子假设：无模型时的规则降级从这里挑起点，每条都写明经济学逻辑与可证伪条件。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import numpy as np

from .factor_expr import FactorExprError, evaluate, normalize, parse

FACTORS = "factors"
TRIALS = "research_trials"
REPORTS = "factor_reports"

STATUS_LABELS = {"candidate": "候选", "active": "入库", "decaying": "衰减预警", "retired": "退役"}
CATEGORIES = {
    "risk_premium": "风险溢价",
    "behavioral": "行为偏差",
    "microstructure": "市场微观结构",
    "structural": "结构性资金流",
    "other": "其他",
}

SEEDS: list[dict[str, Any]] = [
    {
        "name": "短期反转",
        "expression": "-returns(3)",
        "keywords": ["反转", "reversal", "超跌", "均值回归"],
        "hypothesis": {
            "category": "microstructure",
            "rationale": "短期价格冲击来自流动性需求而非信息，流动性提供者会在之后得到补偿，价格部分回吐",
            "direction": "过去几期涨得多的币，下一期相对跑输",
            "falsify": "在流动性最好的大币里依然显著为正，说明不是流动性补偿",
        },
    },
    {
        "name": "中期动量",
        "expression": "returns(30) - returns(3)",
        "keywords": ["动量", "momentum", "趋势", "强者恒强"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "投资者对信息反应不足、羊群效应使趋势延续；剔除最近几期以避开短期反转",
            "direction": "过去一段时间相对强势的币继续跑赢",
            "falsify": "下跌期或高波动期显著为负（动量崩溃）且全样本不显著",
        },
    },
    {
        "name": "低波动异象",
        "expression": "-ts_std(ret, 30)",
        "keywords": ["波动", "低波", "volatility", "风险"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "彩票偏好使投资者高估高波动币，长期看低波动币风险调整后收益更好",
            "direction": "波动率低的币相对跑赢",
            "falsify": "剔除市场贝塔后不再有效",
        },
    },
    {
        "name": "非流动性溢价",
        "expression": "ts_mean(abs(ret) / log(amount + 1), 30)",
        "keywords": ["流动性", "amihud", "成交额", "小币"],
        "hypothesis": {
            "category": "risk_premium",
            "rationale": "持有流动性差的资产要承担冲击成本与退出风险，需要更高的预期收益补偿",
            "direction": "Amihud 非流动性越高，后续收益越高",
            "falsify": "扣除合理成本后多空收益为负",
        },
    },
    {
        "name": "资金费率拥挤反转",
        "expression": "-cs_rank(ts_mean(funding, 9))",
        "keywords": ["资金费", "funding", "拥挤", "杠杆", "多头"],
        "hypothesis": {
            "category": "structural",
            "rationale": "资金费率持续偏高说明杠杆多头拥挤，多头需要持续付费，拥挤交易在去杠杆时被挤出",
            "direction": "近期资金费率越高的币，后续越容易跑输",
            "falsify": "资金费率极端值附近效应反向或消失",
        },
    },
    {
        "name": "持仓量背离",
        "expression": "-ts_corr(delta(oi, 1), ret, 24)",
        "keywords": ["持仓", "oi", "open interest", "背离"],
        "hypothesis": {
            "category": "structural",
            "rationale": "价格上涨但持仓量同步上升意味着新增杠杆推动，脆弱；价格与持仓背离更可持续",
            "direction": "持仓量变化与价格同向越强，后续越弱",
            "falsify": "在没有合约的现货币上同样有效（说明与杠杆无关）",
        },
    },
    {
        "name": "主动买入压力",
        "expression": "ts_mean(log(taker_ratio), 12)",
        "keywords": ["主动买", "taker", "订单流", "买盘"],
        "hypothesis": {
            "category": "microstructure",
            "rationale": "主动买入占比反映知情交易者的方向，订单流有持续性",
            "direction": "主动买入占比高的币短期继续上涨",
            "falsify": "效应只在 1 期内存在且被成本完全吃掉",
        },
    },
    {
        "name": "永续溢价收敛",
        "expression": "-ts_zscore(basis, 48)",
        "keywords": ["基差", "溢价", "basis", "premium", "期现"],
        "hypothesis": {
            "category": "structural",
            "rationale": "永续价格相对现货的溢价偏离会被套利资金拉回，高溢价意味着衍生品端情绪过热",
            "direction": "溢价相对自身历史偏高时后续跑输",
            "falsify": "溢价偏离后价格向永续方向收敛（现货跟随）",
        },
    },
    {
        "name": "恐贪逆向",
        "expression": "-(ts_mean(fng, 3) - 50)",
        "keywords": ["恐惧", "贪婪", "情绪", "fng", "sentiment"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "极端情绪对应过度反应：恐惧时被低估、贪婪时被高估",
            "direction": "市场越恐惧，后续收益越高（时序）",
            "falsify": "情绪指标滞后于价格，加入价格动量后不再有增量",
        },
    },
    {
        "name": "新闻情绪动量",
        "expression": "cs_rank(ev_news)",
        "keywords": ["新闻", "情绪", "news", "事件", "舆情"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "利好新闻的信息扩散需要时间，市场对新闻反应不足，情绪得分高的币短期继续跑赢",
            "direction": "近 24 小时新闻情绪越正面，后续越强",
            "falsify": "效应在发布后 1 小时内完全兑现，或标注器换成规则词典后消失（说明是模型回看偏差）",
        },
    },
    {
        "name": "下架公告规避",
        "expression": "ev_delisting",
        "keywords": ["下架", "delist", "公告", "事件"],
        "hypothesis": {
            "category": "structural",
            "rationale": "下架公告后流动性枯竭、被动卖盘集中，价格持续承压",
            "direction": "有下架公告的币后续显著跑输（因子值为负）",
            "falsify": "公告后反而出现超跌反弹",
        },
    },
    {
        "name": "上币公告溢价",
        "expression": "ev_listing",
        "keywords": ["上币", "listing", "公告", "事件"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "上币带来新的流动性与关注度，短期有溢价",
            "direction": "上币公告后的币短期跑赢",
            "falsify": "公告前已充分上涨，公告后反转",
        },
    },
    {
        "name": "强平失衡反转",
        "expression": "log(liq_long + 1) - log(liq_short + 1)",
        "keywords": ["强平", "清算", "liquidation", "爆仓"],
        "hypothesis": {
            "category": "microstructure",
            "rationale": "多头集中被强平是被动抛售，价格被打穿后会部分回补",
            "direction": "多头强平远多于空头之后，价格反弹",
            "falsify": "强平之后继续单边下跌（趋势而非流动性冲击）",
        },
    },
    {
        "name": "期权偏度恐慌",
        "expression": "ts_zscore(iv_skew, 72)",
        "keywords": ["期权", "偏度", "skew", "iv", "波动率"],
        "hypothesis": {
            "category": "risk_premium",
            "rationale": "看跌期权相对变贵说明市场在为下跌付保险，恐慌溢价随后回归，现货往往已提前下跌",
            "direction": "偏度相对历史偏高时后续收益偏高（逆向）",
            "falsify": "偏度上升后现货继续下跌",
        },
    },
    {
        "name": "宏观事件前降风险",
        "expression": "-ev_macro",
        "keywords": ["宏观", "cpi", "fomc", "非农", "日历"],
        "hypothesis": {
            "category": "risk_premium",
            "rationale": "重要宏观数据公布前不确定性高，风险资产常在公布前承压",
            "direction": "未来 24 小时有重要事件时收益偏低（时序）",
            "falsify": "公布前后收益分布没有差异",
        },
    },
    {
        "name": "量价相关",
        "expression": "-ts_corr(close, volume, 20)",
        "keywords": ["量价", "成交量", "volume"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "放量上涨常伴随散户追涨与过度乐观，量价高度同向后容易回落",
            "direction": "量价相关越高，后续越弱",
            "falsify": "只在小币上有效且换手极高",
        },
    },
    {
        "name": "最大日收益（彩票）",
        "expression": "-ts_max(ret, 42)",
        "keywords": ["彩票", "暴涨", "max", "lottery"],
        "hypothesis": {
            "category": "behavioral",
            "rationale": "近期出现过极端单期涨幅的币吸引彩票型资金，被系统性高估",
            "direction": "最近出现过最大单期涨幅越大，后续越弱",
            "falsify": "与短期反转相关性 > 0.8，没有独立信息",
        },
    },
    {
        "name": "市场贝塔",
        "expression": "-ts_beta(ret, mkt_ret, 60)",
        "keywords": ["贝塔", "beta", "防御"],
        "hypothesis": {
            "category": "risk_premium",
            "rationale": "杠杆约束使投资者偏好高贝塔资产（BAB 效应），低贝塔资产风险调整后收益更高",
            "direction": "贝塔越低越好",
            "falsify": "只在下跌期有效",
        },
    },
]


def _now() -> float:
    return time.time()


def expr_hash(expr: str) -> str:
    try:
        norm = normalize(expr)
    except FactorExprError:
        norm = expr.strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def pick_seeds(text: str, *, exclude: set[str] | None = None, k: int = 3) -> list[dict[str, Any]]:
    """按关键词挑种子假设；没有命中就按默认顺序。"""
    exclude = exclude or set()
    low = (text or "").lower()
    hit = [s for s in SEEDS if any(kw.lower() in low for kw in s["keywords"])]
    rest = [s for s in SEEDS if s not in hit]
    return [s for s in hit + rest if expr_hash(s["expression"]) not in exclude][:k]


def mutate(expr: str, round_no: int) -> list[tuple[str, str]]:
    """规则降级的改进：换窗口、加截面排名、做平滑。返回 [(新表达式, 说明)]。"""
    import re

    out: list[tuple[str, str]] = []
    nums = [int(m.group(2)) for m in re.finditer(r"([,(]\s*)(\d+)(\s*\))", expr) if 1 < int(m.group(2)) <= 200]
    for n in dict.fromkeys(nums):
        for f, label in ((2.0, "拉长"), (0.5, "缩短")):
            m = max(2, round(n * f))
            if m != n:
                cand = re.sub(rf"([,(]\s*){n}(\s*\))", lambda g, m=m: f"{g.group(1)}{m}{g.group(2)}", expr, count=1)
                out.append((cand, f"把窗口 {n} {label}到 {m}"))
    if "rank(" not in expr:
        out.append((f"cs_rank({expr})", "做截面排名，降低极端值影响"))
    if "decay_linear" not in expr:
        out.append((f"decay_linear({expr}, 3)", "做 3 期线性衰减平滑，降低换手"))
    rot = round_no % max(1, len(out))
    return out[rot:] + out[:rot]


class FactorLibrary:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ---------------- 因子 ----------------
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(FACTORS)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: -(r.get("updated_at") or 0))

    def get(self, fid: str) -> dict[str, Any] | None:
        return self.store.get(FACTORS, fid)

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((f for f in self.store.list(FACTORS) if f.get("name") == name or f.get("id") == name), None)

    def save(self, payload: dict[str, Any], *, report: dict[str, Any] | None = None) -> dict[str, Any]:
        from .contracts import activation_error, annotate_report

        if report is not None:
            annotate_report(report)
        expr = str(payload.get("expression") or "").strip()
        parse(expr)
        fid = payload.get("id") or f"f_{uuid.uuid4().hex[:8]}"
        prev = self.get(fid) or {}
        name = str(payload.get("name") or prev.get("name") or expr[:24]).strip()[:40]
        clash = next((f for f in self.store.list(FACTORS) if f.get("name") == name and f.get("id") != fid), None)
        if clash:
            name = f"{name}_{fid[-4:]}"
        hyp = {**(prev.get("hypothesis") or {}), **(payload.get("hypothesis") or {})}
        rec = {
            **prev,
            "id": fid,
            "name": name,
            "expression": normalize(expr),
            "hypothesis": hyp,
            "status": payload.get("status") or prev.get("status") or "candidate",
            "universe": payload.get("universe") or prev.get("universe") or [],
            "timeframe": payload.get("timeframe") or prev.get("timeframe") or "4h",
            "horizon": int(payload.get("horizon") or prev.get("horizon") or 1),
            "tags": payload.get("tags") or prev.get("tags") or [],
            "created_by": payload.get("created_by") or prev.get("created_by") or "user",
            "project_id": payload.get("project_id") or prev.get("project_id"),
            "note": str(payload.get("note") or prev.get("note") or "")[:400],
            "created_at": prev.get("created_at") or _now(),
            "updated_at": _now(),
        }
        if report is not None:
            rec["summary"] = report.get("summary")
            rec["checks"] = report.get("checks")
            rec["admitted"] = report.get("admitted")
            rec["evaluated_at"] = _now()
            self.store.put_blob(REPORTS, fid, report)
            health = list(rec.get("health") or [])
            health.append(
                {
                    "t": _now(),
                    "rank_ic": (report.get("summary") or {}).get("rank_ic"),
                    "oos_rank_ic": (report.get("summary") or {}).get("oos_rank_ic"),
                }
            )
            rec["health"] = health[-30:]
        proof = report if report is not None else self.report(fid)
        if prev and normalize(expr) != prev.get("expression") and report is None:
            proof = None
            rec.update(summary=None, checks=None, admitted=False)
            self.store.delete_blob(REPORTS, fid)
        rec["activation_block_reason"] = activation_error(proof)
        rec["activation_eligible"] = not rec["activation_block_reason"]
        if rec["status"] == "active" and not rec["activation_eligible"]:
            rec["status"] = "candidate"
        self.store.upsert(FACTORS, rec)
        return rec

    def report(self, fid: str) -> dict[str, Any] | None:
        from .contracts import annotate_report

        report = self.store.get_blob(REPORTS, fid)
        return annotate_report(report) if report else None

    def set_status(self, fid: str, status: str) -> dict[str, Any] | None:
        from .contracts import activation_error

        rec = self.get(fid)
        if rec is None or status not in STATUS_LABELS:
            return None
        if status == "active" and (reason := activation_error(self.report(fid))):
            raise ValueError(reason)
        rec.update(status=status, updated_at=_now())
        self.store.upsert(FACTORS, rec)
        return rec

    def delete(self, fid: str) -> bool:
        self.store.delete_blob(REPORTS, fid)
        return self.store.delete(FACTORS, fid)

    def search(self, query: str = "") -> list[dict[str, Any]]:
        q = (query or "").lower()
        rows = self.list()
        if q:
            rows = [
                r
                for r in rows
                if q
                in (
                    r.get("name", "") + r.get("expression", "") + str((r.get("hypothesis") or {}).get("rationale", ""))
                ).lower()
            ]
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "expression": r["expression"],
                "status": r.get("status"),
                "rank_ic": (r.get("summary") or {}).get("rank_ic"),
                "icir": (r.get("summary") or {}).get("icir"),
                "category": (r.get("hypothesis") or {}).get("category"),
            }
            for r in rows[:20]
        ]

    def library_values(self, panel: Any, *, exclude: str | None = None) -> dict[str, np.ndarray]:
        """在给定面板上计算库内（入库 + 衰减预警）因子的值，用于相关性去重。"""
        out: dict[str, np.ndarray] = {}
        for f in self.list():
            if f.get("status") not in ("active", "decaying") or f.get("id") == exclude:
                continue
            try:
                out[f["name"]] = evaluate(f["expression"], panel.data)
            except Exception:  # 缺字段等：跳过这一个，不影响评估
                continue
        return out

    def health_flag(self, rec: dict[str, Any]) -> str | None:
        """入库因子最近两次评估都明显弱于入库时 → 衰减预警。"""
        h = [x for x in rec.get("health") or [] if x.get("rank_ic") is not None]
        if rec.get("status") != "active" or len(h) < 3:
            return None
        base = h[0]["rank_ic"]
        if base and all(abs(x["rank_ic"]) < abs(base) * 0.4 or x["rank_ic"] * base < 0 for x in h[-2:]):
            return "decaying"
        return None

    # ---------------- 试验台账 ----------------
    def record_trial(self, expr: str, summary: dict[str, Any], *, source: str = "", project_id: str = "") -> int:
        h = expr_hash(expr)
        rec = self.store.get(TRIALS, h)
        if rec is None:
            rec = {
                "id": h,
                "expression": expr[:300],
                "first_at": _now(),
                "count": 0,
                "source": source,
                "project_id": project_id,
            }
        rec.update(
            count=int(rec.get("count") or 0) + 1,
            last_at=_now(),
            rank_ic=summary.get("rank_ic"),
            ic_t=summary.get("ic_t"),
        )
        self.store.upsert(TRIALS, rec)
        return self.trial_count()

    def trial_count(self) -> int:
        return self.store.count(TRIALS)

    def failed_ideas(self, limit: int = 12) -> list[dict[str, Any]]:
        """否定结果：试过但 |t| 很低的表达式，告诉 AI 不要再试一遍。"""
        rows = [r for r in self.store.list(TRIALS) if r.get("ic_t") is not None and abs(r["ic_t"]) < 1.0]
        rows.sort(key=lambda r: -(r.get("last_at") or 0))
        return [{"expression": r["expression"], "rank_ic": r.get("rank_ic")} for r in rows[:limit]]


__all__ = ["CATEGORIES", "FACTORS", "SEEDS", "STATUS_LABELS", "FactorLibrary", "expr_hash", "mutate", "pick_seeds"]
