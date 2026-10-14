"""规则 DSL v2：结构化语法树 + 白名单运算 + 解释执行（不生成、不执行任何 Python 代码）。

plan v2.0 第 96–98 行：用户自建策略采用受限 DSL。指标至少有 SMA、EMA、RSI、ATR、通道与价格收益；
条件有大于、小于、交叉、连续满足与逻辑组合；变量只读当前及过去已完成事件；仓位为目标资金占比或
固定数量，并独立规定精度、最大仓位与资金不足处理。规则解析不调用 eval / exec。

同一份 JSON 同时驱动“逐项表单”和“JSON 视图”（前端两种视图编辑的是同一个对象，schema 见
`DSL_SCHEMA`）。示例::

    {
      "dsl_version": 2,
      "instrument": "BTC/USDT", "timeframe": "1h",
      "params": {"fast": 12, "slow": 48},
      "indicators": {
        "ma_fast": {"type": "sma", "period": "$fast"},
        "ma_slow": {"type": "sma", "period": "$slow"},
        "rsi14":   {"type": "rsi", "period": 14}
      },
      "entry": {"op": "all", "conds": [
        {"op": "cross_above", "left": {"ref": "ma_fast"}, "right": {"ref": "ma_slow"}},
        {"op": "lt", "left": {"ref": "rsi14"}, "right": {"const": 70}}
      ]},
      "exit":  {"op": "cross_below", "left": {"ref": "ma_fast"}, "right": {"ref": "ma_slow"}},
      "position": {"mode": "weight", "value": 0.8, "max_weight": 1.0, "qty_step": 0.0001,
                   "insufficient_funds": "reject"},
      "risk": {"stop_loss": 0.05, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0}
    }

条件节点：
    {"op": "gt"|"gte"|"lt"|"lte", "left": 操作数, "right": 操作数}
    {"op": "cross_above"|"cross_below", "left": 操作数, "right": 操作数}
    {"op": "consecutive", "cond": 条件, "bars": N}        最近 N 根 bar 都满足
    {"op": "all"|"any", "conds": [条件, ...]}   {"op": "not", "cond": 条件}
操作数：
    {"ref": 指标名, "lag": k}      指标在 k 根 bar 之前的值（默认 0 = 最近一根已收盘 bar）
    {"price": "close"|"open"|"high"|"low", "lag": k}
    {"const": 数值}
    任意操作数可带 "mul": 系数（例如通道下沿 × 0.98）
入场与离场在同一根 bar 同时满足时，**离场优先**（不加仓、不反手）。

多标的与轮动（T04）::

    {
      "dsl_version": 2,
      "instruments": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
      "timeframe": "1d", "params": {"lookback": 20},
      "indicators": {"ret": {"type": "return", "period": "$lookback"}},
      "rank": {"by": "ret", "top": 1, "order": "desc", "weighting": "equal"},
      "exit": {"op": "lt", "left": {"ref": "ret"}, "right": {"const": 0}},
      "position": {"mode": "weight", "value": 1.0}
    }

- `instrument`（单标的）与 `instruments`（多标的）**二选一**：同时给出会被拒绝，
  而不是静默取其一——静默选择会让用户以为另一个也生效了。
- `rank.by` 必须是**已定义的指标名**（不是任意表达式）：排序口径必须唯一且可复现。
- `rank.top` 不能超过标的数（取不满会静默变成"少买一个"，属于静默降级）。
- 排序的平局打破规则见 `rank_order()` 的注释（按标的代码字典序）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

DSL_VERSION = 2
INDICATOR_TYPES = {
    "sma": "简单移动平均",
    "ema": "指数移动平均",
    "rsi": "相对强弱指数（Wilder）",
    "atr": "平均真实波幅（Wilder）",
    "highest": "通道上沿：区间最高价",
    "lowest": "通道下沿：区间最低价",
    "return": "区间价格收益率",
}
PRICE_FIELDS = ("open", "high", "low", "close")
COMPARE_OPS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
CROSS_OPS = {"cross_above": "上穿", "cross_below": "下穿"}
LOGIC_OPS = ("all", "any", "not", "consecutive")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
INSTRUMENT_RE = re.compile(r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")
MAX_INDICATORS = 12
MAX_NODES = 60
MAX_DEPTH = 6
MAX_PERIOD = 500
MAX_LAG = 50
MAX_CONSECUTIVE = 50
MAX_INSTRUMENTS = 20
RANK_ORDERS = {"desc": "降序（得分高的优先）", "asc": "升序（得分低的优先）"}
RANK_WEIGHTINGS = {"equal": "等权（前 N 名各占 1/N）", "score": "按得分加权（按得分占比分配）"}
# 排序时的平局打破规则：按标的代码字典序。见 rank_order() 的注释说明为什么必须固定。
TIE_BREAK = "instrument_asc"
TIE_BREAK_LABEL = "同分时按标的代码字典序"


class DslError(ValueError):
    """DSL 校验失败。`problems` 是逐项问题（字段路径 + 说明），前端逐项标红。"""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        self.problems = problems
        super().__init__("；".join(f"{p['path']}：{p['message']}" for p in problems[:6]))


# ---------------------------------------------------------------------------
# JSON Schema（前端表单与 JSON 视图共用；后端校验更严格，以 validate() 为准）
# ---------------------------------------------------------------------------
_OPERAND = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "price": {"enum": list(PRICE_FIELDS)},
        "const": {"type": "number"},
        "lag": {"type": "integer", "minimum": 0, "maximum": MAX_LAG},
        "mul": {"type": "number"},
    },
}
DSL_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "QuantLab 规则 DSL v2",
    "type": "object",
    "required": ["dsl_version", "timeframe", "indicators", "entry", "position"],
    "properties": {
        "dsl_version": {"const": DSL_VERSION},
        "name": {"type": "string", "maxLength": 60},
        "instrument": {"type": "string", "pattern": r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$"},
        # instrument 与 instruments 二选一（后端 validate 强制，schema 只做形状约束）
        "instruments": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_INSTRUMENTS,
            "items": {"type": "string", "pattern": r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$"},
        },
        "timeframe": {"enum": list(TIMEFRAMES)},
        "params": {"type": "object", "additionalProperties": {"type": "number"}},
        "indicators": {
            "type": "object",
            "maxProperties": MAX_INDICATORS,
            "additionalProperties": {
                "type": "object",
                "required": ["type", "period"],
                "properties": {
                    "type": {"enum": list(INDICATOR_TYPES)},
                    "period": {
                        "oneOf": [
                            {"type": "integer", "minimum": 1, "maximum": MAX_PERIOD},
                            {"type": "string", "pattern": r"^\$[a-z][a-z0-9_]*$"},
                        ]
                    },
                    "source": {"enum": list(PRICE_FIELDS)},
                },
            },
        },
        "entry": {"$ref": "#/$defs/cond"},
        "exit": {"$ref": "#/$defs/cond"},
        "rank": {
            "type": "object",
            "required": ["by", "top"],
            "properties": {
                "by": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,23}$"},
                "top": {"type": "integer", "minimum": 1, "maximum": MAX_INSTRUMENTS},
                "order": {"enum": list(RANK_ORDERS)},
                "weighting": {"enum": list(RANK_WEIGHTINGS)},
                "min_score": {"type": "number"},
            },
        },
        "position": {
            "type": "object",
            "required": ["mode", "value"],
            "properties": {
                "mode": {"enum": ["weight", "quantity"]},
                "value": {"type": "number", "exclusiveMinimum": 0},
                "max_weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "qty_step": {"type": "number", "exclusiveMinimum": 0},
                "insufficient_funds": {"enum": ["reject", "reduce"]},
            },
        },
        "risk": {
            "type": "object",
            "properties": {
                "stop_loss": {"type": "number", "minimum": 0, "exclusiveMaximum": 1},
                "take_profit": {"type": "number", "minimum": 0},
                "max_trades_per_day": {"type": "integer", "minimum": 0},
                "drawdown_pause": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
    "$defs": {
        "operand": _OPERAND,
        "cond": {
            "type": "object",
            "required": ["op"],
            "properties": {
                "op": {"enum": [*COMPARE_OPS, *CROSS_OPS, *LOGIC_OPS]},
                "left": {"$ref": "#/$defs/operand"},
                "right": {"$ref": "#/$defs/operand"},
                "cond": {"$ref": "#/$defs/cond"},
                "conds": {"type": "array", "items": {"$ref": "#/$defs/cond"}},
                "bars": {"type": "integer", "minimum": 1, "maximum": MAX_CONSECUTIVE},
            },
        },
    },
}

TEMPLATE: dict[str, Any] = {
    "dsl_version": 2,
    "name": "均线交叉 + RSI 过滤",
    "instrument": "BTC/USDT",
    "timeframe": "1h",
    "params": {"fast": 12, "slow": 48},
    "indicators": {
        "ma_fast": {"type": "sma", "period": "$fast", "source": "close"},
        "ma_slow": {"type": "sma", "period": "$slow", "source": "close"},
        "rsi14": {"type": "rsi", "period": 14, "source": "close"},
    },
    "entry": {
        "op": "all",
        "conds": [
            {"op": "cross_above", "left": {"ref": "ma_fast"}, "right": {"ref": "ma_slow"}},
            {"op": "lt", "left": {"ref": "rsi14"}, "right": {"const": 70}},
        ],
    },
    "exit": {"op": "cross_below", "left": {"ref": "ma_fast"}, "right": {"ref": "ma_slow"}},
    "position": {"mode": "weight", "value": 0.8, "max_weight": 1.0, "qty_step": 0.0001, "insufficient_funds": "reject"},
    "risk": {"stop_loss": 0.05, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0},
}

EXAMPLES: dict[str, dict[str, Any]] = {
    "ma_cross_rsi": TEMPLATE,
    "donchian_breakout": {
        "dsl_version": 2,
        "name": "唐奇安通道突破 + ATR 止损",
        "instrument": "BTC/USDT",
        "timeframe": "4h",
        "params": {"channel": 20},
        "indicators": {
            "upper": {"type": "highest", "period": "$channel", "source": "high"},
            "lower": {"type": "lowest", "period": "$channel", "source": "low"},
            "atr14": {"type": "atr", "period": 14},
        },
        "entry": {"op": "gt", "left": {"price": "close"}, "right": {"ref": "upper", "lag": 1}},
        "exit": {"op": "lt", "left": {"price": "close"}, "right": {"ref": "lower", "lag": 1}},
        "position": {
            "mode": "weight",
            "value": 0.5,
            "max_weight": 0.5,
            "qty_step": 0.0001,
            "insufficient_funds": "reject",
        },
        "risk": {"stop_loss": 0.08, "take_profit": 0, "max_trades_per_day": 2, "drawdown_pause": 0.25},
    },
    "rsi_reversion_fixed_qty": {
        "dsl_version": 2,
        "name": "RSI 连续超卖反转（固定数量）",
        "instrument": "ETH/USDT",
        "timeframe": "1h",
        "params": {},
        "indicators": {"rsi14": {"type": "rsi", "period": 14}, "ret24": {"type": "return", "period": 24}},
        "entry": {
            "op": "all",
            "conds": [
                {
                    "op": "consecutive",
                    "bars": 3,
                    "cond": {"op": "lt", "left": {"ref": "rsi14"}, "right": {"const": 30}},
                },
                {"op": "gt", "left": {"ref": "ret24"}, "right": {"const": -0.15}},
            ],
        },
        "exit": {"op": "gt", "left": {"ref": "rsi14"}, "right": {"const": 55}},
        "position": {
            "mode": "quantity",
            "value": 0.5,
            "max_weight": 0.6,
            "qty_step": 0.001,
            "insufficient_funds": "reject",
        },
        "risk": {"stop_loss": 0.06, "take_profit": 0.12, "max_trades_per_day": 0, "drawdown_pause": 0},
    },
    "momentum_rotation_top1": {
        "dsl_version": 2,
        "name": "动量轮动：20 日收益取前 1（等权）",
        "instruments": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "timeframe": "1d",
        "params": {"lookback": 20},
        "indicators": {"ret": {"type": "return", "period": "$lookback"}},
        "rank": {"by": "ret", "top": 1, "order": "desc", "weighting": "equal"},
        "entry": {"op": "gt", "left": {"ref": "ret"}, "right": {"const": 0}},
        "exit": {"op": "lt", "left": {"ref": "ret"}, "right": {"const": 0}},
        "position": {
            "mode": "weight",
            "value": 1.0,
            "max_weight": 1.0,
            "qty_step": 0.0001,
            "insufficient_funds": "reject",
        },
        "risk": {"stop_loss": 0, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0},
    },
    "momentum_rotation_weighted": {
        "dsl_version": 2,
        "name": "动量轮动：20 日收益取前 2（按得分加权）",
        "instruments": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "timeframe": "1d",
        "params": {"lookback": 20},
        "indicators": {"ret": {"type": "return", "period": "$lookback"}},
        "rank": {"by": "ret", "top": 2, "order": "desc", "weighting": "score"},
        "entry": {"op": "gt", "left": {"ref": "ret"}, "right": {"const": 0}},
        "exit": {"op": "lt", "left": {"ref": "ret"}, "right": {"const": 0}},
        "position": {
            "mode": "weight",
            "value": 1.0,
            "max_weight": 1.0,
            "qty_step": 0.0001,
            "insufficient_funds": "reject",
        },
        "risk": {"stop_loss": 0, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0},
    },
}


# ---------------------------------------------------------------------------
# 校验与编译
# ---------------------------------------------------------------------------
@dataclass
class CompiledDsl:
    spec: dict[str, Any]  # 规范化后的原始 JSON（含 $param 引用）
    resolved: dict[str, Any]  # 参数代入后的可执行形式
    warmup_bars: int
    code_hash: str
    summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "warmup_bars": self.warmup_bars,
            "code_hash": self.code_hash,
            "summary": self.summary,
            "warnings": self.warnings,
            "params": self.spec.get("params", {}),
        }


def canonical(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def rank_order(scores: dict[str, float | None], *, order: str = "desc") -> list[str]:
    """按得分排序标的，返回**稳定**的完整名次（含数据不足的标的，排在最后）。

    为什么必须固定平局打破规则：轮动策略的成交序列直接取决于名次。
    如果同分时顺序随实现细节抖动（比如依赖 dict 插入序、集合迭代序或
    `sorted` 的不稳定比较），同一份输入就会产生不同的成交序列，而协议要求
    "同样的输入必须产生同样的 signal_id 与同样的成交序列"（见
    docs/策略协议与数据口径.md 第 4 节）。所以这里显式规定：

    1. 先按得分排序（`desc` 降序 / `asc` 升序）；
    2. 得分相同 —— 以及得分为 None（指标未就绪、数据不足）—— 一律按**标的代码字典序**；
    3. 得分为 None 的标的永远排在有效得分之后（排序键里的 `valid` 位）。

    第 3 条同样是为了确定性：None 与任何数比较都会抛 TypeError，若交给
    `sorted` 处理就会崩，若用 `-inf` 代替又会在 `asc` 下变成"最优"，
    两种都不对。显式分档后，`asc` / `desc` 两种顺序对缺失数据的处置一致。
    """

    def key(item: tuple[str, float | None]) -> tuple[int, float, str]:
        name, score = item
        if score is None:
            return (1, 0.0, name)
        # Python 的 sorted 是升序；"降序"要显式取负，否则会静默变成"最差的排第一"。
        return (0, score if order == "asc" else -score, name)

    return [name for name, _ in sorted(scores.items(), key=key)]


def _rank_weights(picks: list[str], scores: dict[str, float | None], weighting: str) -> dict[str, float]:
    """把名次换算成目标权重（未归一化，调用方再按 max_weight 裁剪）。

    - `equal`：前 N 名等权，各 1/N；
    - `score`：按得分占比分配。总得分非正（含全为 0 或含负数）时退回等权——
      负权重在现货里没有意义（不能做空），按负比例分配会让"最差的标的"
      拿到负仓位，与"取前 N 名"的语义直接冲突。
    """
    if not picks:
        return {}
    if weighting == "score":
        vals = [scores.get(p) or 0.0 for p in picks]
        total = sum(vals)
        if total > 0 and all(v >= 0 for v in vals):
            return {p: v / total for p, v in zip(picks, vals, strict=True)}
    return {p: 1.0 / len(picks) for p in picks}


def parse_dsl_text(text: str) -> dict[str, Any]:
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DslError([{"path": "$", "message": f"不是合法的 JSON：第 {exc.lineno} 行 {exc.msg}"}]) from exc
    if not isinstance(spec, dict):
        raise DslError([{"path": "$", "message": "DSL 顶层必须是对象"}])
    return spec


def validate(spec: dict[str, Any], *, params: dict[str, Any] | None = None) -> CompiledDsl:
    """逐项校验 DSL，返回编译结果；任何问题都汇总后一次性抛出 DslError。"""
    P: list[dict[str, str]] = []

    def bad(path: str, msg: str) -> None:
        P.append({"path": path, "message": msg})

    if not isinstance(spec, dict):
        raise DslError([{"path": "$", "message": "DSL 顶层必须是对象"}])
    known_top = {
        "dsl_version",
        "name",
        "instrument",
        "instruments",
        "timeframe",
        "params",
        "indicators",
        "rank",
        "entry",
        "exit",
        "position",
        "risk",
        "description",
    }
    for k in spec:
        if k not in known_top:
            bad(k, "未知字段（DSL 不接受额外字段，避免拼写错误被静默忽略）")
    if spec.get("dsl_version") != DSL_VERSION:
        bad("dsl_version", f"必须为 {DSL_VERSION}")

    # ---- 标的：instrument（单标的）与 instruments（多标的）二选一 ----
    # 两个都写时不取其一：静默选择会让用户以为另一个也生效了，回测结果与预期不符却没有任何提示。
    instruments: list[str] = []
    if "instrument" in spec and "instruments" in spec:
        bad("instruments", "instrument 与 instruments 只能二选一：单标的用 instrument，多标的用 instruments")
    if "instruments" in spec:
        raw_list = spec.get("instruments")
        if not isinstance(raw_list, list) or not raw_list:
            bad("instruments", '必须是至少包含一个标的的数组，例如 ["BTC/USDT", "ETH/USDT"]')
        else:
            for i, s in enumerate(raw_list):
                if not isinstance(s, str) or not INSTRUMENT_RE.fullmatch(s or ""):
                    bad(f"instruments[{i}]", "标的格式应为 BASE/QUOTE，例如 BTC/USDT")
                    continue
                if s in instruments:
                    bad(f"instruments[{i}]", f"标的 {s} 重复；同一标的出现两次会让排名与仓位分配失去意义")
                    continue
                instruments.append(s)
            if len(raw_list) > MAX_INSTRUMENTS:
                bad("instruments", f"最多 {MAX_INSTRUMENTS} 个标的")
    else:
        inst = spec.get("instrument")
        if not isinstance(inst, str) or not INSTRUMENT_RE.fullmatch(inst or ""):
            bad("instrument", "标的格式应为 BASE/QUOTE，例如 BTC/USDT")
        else:
            instruments = [inst]
    if spec.get("timeframe") not in TIMEFRAMES:
        bad("timeframe", f"周期只能是 {', '.join(TIMEFRAMES)}")

    # ---- 参数 ----
    declared = spec.get("params") or {}
    if not isinstance(declared, dict):
        bad("params", "必须是对象")
        declared = {}
    values: dict[str, float] = {}
    for k, v in declared.items():
        if not NAME_RE.fullmatch(str(k)):
            bad(f"params.{k}", "参数名只能用小写字母、数字、下划线，且以字母开头")
        elif not _num(v):
            bad(f"params.{k}", "参数值必须是有限数值")
        else:
            values[k] = v
    for k, v in (params or {}).items():
        if k not in declared:
            bad(f"params.{k}", f"参数 {k} 未在 DSL 的 params 中声明")
        elif not _num(v):
            bad(f"params.{k}", "参数值必须是有限数值")
        else:
            values[k] = v
    used_params: set[str] = set()

    def resolve(v: Any, path: str) -> Any:
        if isinstance(v, str) and v.startswith("$"):
            name = v[1:]
            if name not in values:
                bad(path, f"引用了未声明的参数 {v}")
                return None
            used_params.add(name)
            return values[name]
        return v

    # ---- 指标 ----
    inds = spec.get("indicators") or {}
    resolved_inds: dict[str, dict[str, Any]] = {}
    if not isinstance(inds, dict) or not inds:
        bad("indicators", "至少定义一个指标")
        inds = {}
    if len(inds) > MAX_INDICATORS:
        bad("indicators", f"最多 {MAX_INDICATORS} 个指标")
    for name, ind in inds.items():
        path = f"indicators.{name}"
        if not NAME_RE.fullmatch(str(name)):
            bad(path, "指标名只能用小写字母、数字、下划线，且以字母开头，最长 24 个字符")
            continue
        if not isinstance(ind, dict):
            bad(path, "必须是对象")
            continue
        for k in ind:
            if k not in ("type", "period", "source"):
                bad(f"{path}.{k}", "未知字段")
        t = ind.get("type")
        if t not in INDICATOR_TYPES:
            bad(f"{path}.type", f"未知指标类型；可选：{', '.join(INDICATOR_TYPES)}")
            continue
        period = resolve(ind.get("period"), f"{path}.period")
        if period is None:
            continue
        if not _num(period) or int(period) != period or not 1 <= period <= MAX_PERIOD:
            bad(f"{path}.period", f"周期必须是 1–{MAX_PERIOD} 的整数")
            continue
        if t in ("rsi", "atr") and period < 2:
            bad(f"{path}.period", "该指标周期至少为 2")
        default_src = {"highest": "high", "lowest": "low"}.get(t, "close")
        src = ind.get("source", default_src)
        if src not in PRICE_FIELDS:
            bad(f"{path}.source", f"数据源只能是 {', '.join(PRICE_FIELDS)}")
        if t == "atr" and "source" in ind:
            bad(f"{path}.source", "ATR 使用最高、最低与收盘价，不需要 source")
        resolved_inds[name] = {"type": t, "period": int(period), "source": src}

    # ---- 条件 ----
    counter = {"nodes": 0}
    lookback = {"lag": 0, "consec": 0}

    def operand(o: Any, path: str) -> dict[str, Any] | None:
        counter["nodes"] += 1
        if not isinstance(o, dict):
            bad(path, "操作数必须是对象：{ref} / {price} / {const}")
            return None
        kinds = [k for k in ("ref", "price", "const") if k in o]
        if len(kinds) != 1:
            bad(path, "操作数必须且只能有 ref、price、const 之一")
            return None
        for k in o:
            if k not in ("ref", "price", "const", "lag", "mul"):
                bad(f"{path}.{k}", "未知字段")
        lag = o.get("lag", 0)
        if not isinstance(lag, int) or isinstance(lag, bool) or not 0 <= lag <= MAX_LAG:
            bad(f"{path}.lag", f"lag 必须是 0–{MAX_LAG} 的整数")
            lag = 0
        mul = o.get("mul", 1)
        if not _num(mul) or mul == 0:
            bad(f"{path}.mul", "mul 必须是非零有限数值")
            mul = 1
        out = {"lag": lag, "mul": mul}
        if "ref" in o:
            if o["ref"] not in resolved_inds:
                bad(f"{path}.ref", f"引用了未定义的指标 {o['ref']}")
                return None
            out["ref"] = o["ref"]
        elif "price" in o:
            if o["price"] not in PRICE_FIELDS:
                bad(f"{path}.price", f"价格字段只能是 {', '.join(PRICE_FIELDS)}")
                return None
            out["price"] = o["price"]
        else:
            c = resolve(o["const"], f"{path}.const")
            if not _num(c):
                bad(f"{path}.const", "常量必须是有限数值")
                return None
            out["const"] = c
            if "lag" in o:
                bad(f"{path}.lag", "常量不需要 lag")
        lookback["lag"] = max(lookback["lag"], lag)
        return out

    def ind_type(o: dict[str, Any] | None) -> str | None:
        return resolved_inds[o["ref"]]["type"] if o and "ref" in o else None

    def cond(c: Any, path: str, depth: int) -> dict[str, Any] | None:
        counter["nodes"] += 1
        if depth > MAX_DEPTH:
            bad(path, f"条件嵌套过深（上限 {MAX_DEPTH} 层）")
            return None
        if not isinstance(c, dict) or "op" not in c:
            bad(path, "条件必须是带 op 的对象")
            return None
        op = c["op"]
        allowed = (
            {"op", "left", "right"}
            if op in COMPARE_OPS or op in CROSS_OPS
            else {"op", "conds"}
            if op in ("all", "any")
            else {"op", "cond", "bars"}
            if op in ("not", "consecutive")
            else {"op"}
        )
        for k in c:
            if k not in allowed:
                bad(f"{path}.{k}", f"{op} 条件不接受该字段")
        if op in COMPARE_OPS or op in CROSS_OPS:
            left = operand(c.get("left"), f"{path}.left")
            right = operand(c.get("right"), f"{path}.right")
            if left is None or right is None:
                return None
            if "const" in left and "const" in right:
                bad(path, "两侧都是常量，条件恒定，没有意义")
            for side, other in ((left, right), (right, left)):
                if ind_type(side) == "rsi" and "const" in other and not 0 <= other["const"] / side["mul"] <= 100:
                    bad(path, "RSI 的取值范围是 0–100，与之比较的常量超出范围，条件永远（不）成立")
            if op in CROSS_OPS:
                if "const" in left and "const" in right:
                    pass
                lt, rt = ind_type(left), ind_type(right)
                if lt in ("sma", "ema") and lt == rt:
                    lp = resolved_inds[left["ref"]]
                    rp = resolved_inds[right["ref"]]
                    if lp["period"] == rp["period"] and lp["source"] == rp["source"] and left["lag"] == right["lag"]:
                        bad(path, "两条均线完全相同，交叉永远不会发生")
                    elif lp["period"] >= rp["period"]:
                        bad(
                            path,
                            f"均线交叉的左侧应为快线：{left['ref']}（{lp['period']}）的周期应小于 "
                            f"{right['ref']}（{rp['period']}）",
                        )
                lookback["lag"] = max(lookback["lag"], left["lag"] + 1, right["lag"] + 1)
            return {"op": op, "left": left, "right": right}
        if op in ("all", "any"):
            conds = c.get("conds")
            if not isinstance(conds, list) or not conds:
                bad(f"{path}.conds", f"{op} 至少包含一个条件")
                return None
            if len(conds) > 10:
                bad(f"{path}.conds", "单层最多 10 个条件")
            out = [cond(x, f"{path}.conds[{i}]", depth + 1) for i, x in enumerate(conds)]
            if any(x is None for x in out):
                return None
            return {"op": op, "conds": out}
        if op == "not":
            inner = cond(c.get("cond"), f"{path}.cond", depth + 1)
            return {"op": "not", "cond": inner} if inner else None
        if op == "consecutive":
            n = c.get("bars")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_CONSECUTIVE:
                bad(f"{path}.bars", f"连续 bar 数必须是 1–{MAX_CONSECUTIVE} 的整数")
                n = 1
            before = lookback["lag"]
            inner = cond(c.get("cond"), f"{path}.cond", depth + 1)
            lookback["consec"] = max(
                lookback["consec"], n - 1 + (lookback["lag"] - before if lookback["lag"] > before else 0)
            )
            return {"op": "consecutive", "bars": n, "cond": inner} if inner else None
        bad(f"{path}.op", f"未知运算 {op}；可选：{', '.join([*COMPARE_OPS, *CROSS_OPS, *LOGIC_OPS])}")
        return None

    entry = cond(spec.get("entry"), "entry", 1) if "entry" in spec else None
    if "entry" not in spec:
        bad("entry", "缺少入场条件")
    exit_ = cond(spec.get("exit"), "exit", 1) if spec.get("exit") is not None else None
    if counter["nodes"] > MAX_NODES:
        bad("$", f"条件过于复杂（{counter['nodes']} 个节点，上限 {MAX_NODES}）")
    if entry is not None and exit_ is not None and canonical(entry) == canonical(exit_):
        bad("exit", "离场条件与入场条件完全相同（入场与离场同时满足时离场优先，策略将永远不会持仓）")

    # ---- 仓位 ----
    pos = spec.get("position") or {}
    if not isinstance(pos, dict):
        bad("position", "必须是对象")
        pos = {}
    for k in pos:
        if k not in ("mode", "value", "max_weight", "qty_step", "insufficient_funds"):
            bad(f"position.{k}", "未知字段")
    mode = pos.get("mode")
    value = resolve(pos.get("value"), "position.value")
    max_weight = pos.get("max_weight", 1.0)
    qty_step = pos.get("qty_step", 0.0001)
    if mode not in ("weight", "quantity"):
        bad("position.mode", "仓位模式只能是 weight（目标资金占比）或 quantity（固定数量）")
    if not _num(max_weight) or not 0 < max_weight <= 1:
        bad("position.max_weight", "最大仓位（占权益比例）必须在 (0, 1] 之间；现货不加杠杆")
        max_weight = 1.0
    if not _num(qty_step) or qty_step <= 0:
        bad("position.qty_step", "数量精度必须为正数，例如 0.0001")
        qty_step = 0.0001
    # 只有**真的**写了 rank 对象才算轮动；`"rank": null` 与缺省一样按单标的处理
    # （前端"移除轮动"时会留一个 null，不能因此就放宽 position.value 的校验）。
    rotating = isinstance(spec.get("rank"), dict)
    if not _num(value) or value <= 0:
        bad("position.value", "仓位数值必须为正")
    elif mode == "weight" and value > max_weight + 1e-12 and not rotating:
        # 单标的：value 就是这一个标的的目标占比，不能超过它的上限。
        # 轮动策略里 value 是**整个组合**的目标占比，单个标的的上限由 value/top 决定，
        # 由下面的 rank 分支单独校验（否则"总仓位 0.8、单标的上限 0.5"会被误判为非法）。
        bad("position.value", f"目标资金占比 {value} 超过最大仓位 {max_weight}")
    elif mode == "quantity":
        steps = value / qty_step
        if abs(steps - round(steps)) > 1e-9:
            bad("position.value", f"固定数量 {value} 不是数量精度 {qty_step} 的整数倍")
    if pos.get("insufficient_funds", "reject") not in ("reject", "reduce"):
        bad("position.insufficient_funds", "资金不足处理只能是 reject（拒绝）或 reduce（按可负担数量成交）")

    # ---- 风控 ----
    risk = spec.get("risk") or {}
    if not isinstance(risk, dict):
        bad("risk", "必须是对象")
        risk = {}
    for k in risk:
        if k not in ("stop_loss", "take_profit", "max_trades_per_day", "drawdown_pause"):
            bad(f"risk.{k}", "未知字段")
    sl, tp = risk.get("stop_loss", 0), risk.get("take_profit", 0)
    if not _num(sl) or not 0 <= sl < 1:
        bad("risk.stop_loss", "止损比例应在 [0, 1) 之间，例如 0.05 = 5%")
    if not _num(tp) or tp < 0:
        bad("risk.take_profit", "止盈比例不能为负")
    mtd = risk.get("max_trades_per_day", 0)
    if not isinstance(mtd, int) or isinstance(mtd, bool) or mtd < 0:
        bad("risk.max_trades_per_day", "每日最多成交笔数必须是非负整数（0 = 不限）")
    ddp = risk.get("drawdown_pause", 0)
    if not _num(ddp) or not 0 <= ddp <= 1:
        bad("risk.drawdown_pause", "回撤暂停阈值应在 [0, 1] 之间（0 = 关闭）")
    if exit_ is None and not (_num(sl) and sl > 0) and not (_num(tp) and tp > 0):
        bad("exit", "必须提供离场条件，或设置止损 / 止盈，否则仓位永远不会退出")

    # ---- 轮动排名 ----
    # 放在仓位与风控之后：rank 与 position 的约束互相牵扯（等权时每个标的名义占比
    # 是 value/top，可能超过 max_weight），要等两边都解析完才能一起判。
    rank_raw = spec.get("rank")
    rank: dict[str, Any] | None = None
    if rank_raw is not None:
        if not isinstance(rank_raw, dict):
            bad("rank", "必须是对象：{by, top, order, weighting, min_score}")
        else:
            for k in rank_raw:
                if k not in ("by", "top", "order", "weighting", "min_score"):
                    bad(f"rank.{k}", "未知字段")
            if len(instruments) < 2:
                bad("rank", "轮动排名至少需要 2 个标的；单标的请直接用 instrument 与入场 / 离场条件")
            by = rank_raw.get("by")
            if not isinstance(by, str) or not NAME_RE.fullmatch(by or ""):
                bad("rank.by", "排序依据必须是指标名（小写字母、数字、下划线，以字母开头）")
            elif by not in resolved_inds:
                bad("rank.by", f"指标 {by} 未在 indicators 中定义；可选：{', '.join(resolved_inds) or '（无）'}")
            top = rank_raw.get("top")
            if not isinstance(top, int) or isinstance(top, bool) or top < 1:
                bad("rank.top", "取前 N 名必须是正整数")
                top = None
            elif instruments and top > len(instruments):
                bad(
                    "rank.top",
                    f"取前 {top} 名超过标的数量 {len(instruments)}；"
                    "取不满会静默变成少买几个标的，请改成不超过标的数的值",
                )
                top = None
            order = rank_raw.get("order", "desc")
            if order not in RANK_ORDERS:
                bad("rank.order", f"排序方向只能是 {', '.join(RANK_ORDERS)}（desc = 得分高的优先）")
                order = "desc"
            weighting = rank_raw.get("weighting", "equal")
            if weighting not in RANK_WEIGHTINGS:
                bad("rank.weighting", f"仓位分配方式只能是 {', '.join(RANK_WEIGHTINGS)}")
                weighting = "equal"
            min_score = rank_raw.get("min_score")
            if min_score is not None and not _num(min_score):
                bad("rank.min_score", "入选门槛必须是有限数值（留空表示不设门槛）")
                min_score = None
            if mode == "quantity":
                bad(
                    "position.mode",
                    "轮动策略必须用 weight（目标资金占比）：固定数量无法在名次变化时按比例分配资金",
                )
            if weighting == "score" and order == "asc":
                bad(
                    "rank.weighting",
                    "按得分加权只适用于 desc（得分高的排前面）；升序排序下得分越小反而权重越大，"
                    "与“取前 N 名”的语义冲突，请改用 equal 或改成 desc",
                )
            if top and _num(value) and value > 0:
                per = value / top
                if mode == "weight" and per > max_weight + 1e-12:
                    bad(
                        "position.value",
                        f"目标资金占比 {value} 平均分给 {top} 个标的后每个约 {per:.4f}，"
                        f"超过最大仓位 {max_weight}；请降低 position.value 或提高 max_weight",
                    )
            if _num(value) and value > 1 + 1e-12:
                # 现货不加杠杆：组合总仓位不可能超过 100% 权益。
                bad("position.value", f"轮动组合的总资金占比 {value} 超过 100%；现货不加杠杆")
            rank = {
                "by": by if isinstance(by, str) else "",
                "top": top,
                "order": order,
                "weighting": weighting,
                "min_score": min_score,
                "tie_break": TIE_BREAK,
            }

    warnings: list[str] = []
    unused = sorted(set(values) - used_params)
    if unused:
        warnings.append(f"声明了但未使用的参数：{', '.join(unused)}")
    if P:
        raise DslError(P)

    def ind_lookback(i: dict[str, Any]) -> int:
        return i["period"] * 3 + 1 if i["type"] in ("ema", "rsi", "atr") else i["period"] + 1

    warmup = (
        max((ind_lookback(i) for i in resolved_inds.values()), default=1) + lookback["lag"] + lookback["consec"] + 1
    )
    resolved = {
        "instrument": instruments[0] if instruments else "",
        "instruments": instruments,
        "rotation": rank is not None,
        "rank": rank,
        "timeframe": spec["timeframe"],
        "indicators": resolved_inds,
        "entry": entry,
        "exit": exit_,
        "position": {
            "mode": mode,
            "value": value,
            "max_weight": max_weight,
            "qty_step": qty_step,
            "insufficient_funds": pos.get("insufficient_funds", "reject"),
        },
        "risk": {"stop_loss": sl, "take_profit": tp, "max_trades_per_day": mtd, "drawdown_pause": ddp},
        "window": warmup + 2,
    }
    normalized = json.loads(canonical(spec))
    code_hash = hashlib.sha256(canonical({**normalized, "params": {**declared, **(params or {})}}).encode()).hexdigest()
    return CompiledDsl(
        spec=normalized,
        resolved=resolved,
        warmup_bars=warmup,
        code_hash=code_hash,
        summary=describe(resolved),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 人类可读描述（表单下方实时显示“这条策略在做什么”）
# ---------------------------------------------------------------------------
def _operand_text(o: dict[str, Any], inds: dict[str, Any]) -> str:
    if "const" in o:
        base = f"{o['const']:g}"
    elif "price" in o:
        base = {"open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价"}[o["price"]]
    else:
        i = inds[o["ref"]]
        base = f"{o['ref']}（{i['type'].upper()} {i['period']}）"
    if o.get("lag"):
        base = f"{o['lag']} 根前的{base}"
    if o.get("mul", 1) != 1:
        base = f"{base} × {o['mul']:g}"
    return base


def _cond_text(c: dict[str, Any] | None, inds: dict[str, Any]) -> str:
    if c is None:
        return "（无）"
    op = c["op"]
    if op in COMPARE_OPS:
        return f"{_operand_text(c['left'], inds)} {COMPARE_OPS[op]} {_operand_text(c['right'], inds)}"
    if op in CROSS_OPS:
        return f"{_operand_text(c['left'], inds)} {CROSS_OPS[op]} {_operand_text(c['right'], inds)}"
    if op == "all":
        return "（" + " 且 ".join(_cond_text(x, inds) for x in c["conds"]) + "）"
    if op == "any":
        return "（" + " 或 ".join(_cond_text(x, inds) for x in c["conds"]) + "）"
    if op == "not":
        return f"并非 {_cond_text(c['cond'], inds)}"
    return f"连续 {c['bars']} 根 bar 满足 {_cond_text(c['cond'], inds)}"


def describe(r: dict[str, Any]) -> list[str]:
    inds = r["indicators"]
    pos = r["position"]
    instruments = r.get("instruments") or [r["instrument"]]
    if len(instruments) > 1:
        head = (
            f"标的池 {len(instruments)} 个：{'、'.join(instruments)}，周期 {r['timeframe']}，"
            "在每根 bar 收盘后判断，下一根开盘成交。"
        )
    else:
        head = f"标的 {r['instrument']}，周期 {r['timeframe']}，在每根 bar 收盘后判断，下一根开盘成交。"
    lines = [head]
    rank = r.get("rank")
    if rank:
        lines.append(
            f"轮动：按 {rank['by']}（{inds[rank['by']]['type'].upper()} {inds[rank['by']]['period']}）"
            f"{RANK_ORDERS[rank['order']]}排序取前 {rank['top']} 名，"
            f"{RANK_WEIGHTINGS[rank['weighting']]}；{TIE_BREAK_LABEL}，保证同分时名次可复现。"
        )
        if rank.get("min_score") is not None:
            lines.append(f"入选门槛：得分必须大于 {rank['min_score']:g}，否则该标的空仓。")
    lines += [
        f"入场：{_cond_text(r['entry'], inds)}。",
        f"离场：{_cond_text(r['exit'], inds)}；入场与离场同时满足时离场优先。",
        (
            f"仓位：目标资金占比 {pos['value']:.0%}（上限 {pos['max_weight']:.0%}）"
            if pos["mode"] == "weight"
            else f"仓位：固定数量 {pos['value']:g}（精度 {pos['qty_step']:g}，最大占比 {pos['max_weight']:.0%}）"
        )
        + f"；资金不足时{'拒绝' if pos['insufficient_funds'] == 'reject' else '按可负担数量成交'}。",
    ]
    rk = r["risk"]
    parts = []
    if rk["stop_loss"]:
        parts.append(f"止损 {rk['stop_loss']:.1%}")
    if rk["take_profit"]:
        parts.append(f"止盈 {rk['take_profit']:.1%}")
    if rk["max_trades_per_day"]:
        parts.append(f"每日最多 {rk['max_trades_per_day']} 笔")
    if rk["drawdown_pause"]:
        parts.append(f"回撤超过 {rk['drawdown_pause']:.0%} 暂停开仓")
    lines.append(
        "风控：" + ("、".join(parts) if parts else "无额外限制") + "（同一根 bar 同时触及止损与止盈时止损优先）。"
    )
    return lines


# ---------------------------------------------------------------------------
# 指标计算（只用传入的已收盘 bar，不读未来）
# ---------------------------------------------------------------------------
def _sma(xs: list[float], p: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= p:
            s -= xs[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out


def _ema(xs: list[float], p: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) < p:
        return out
    a = 2 / (p + 1)
    v = sum(xs[:p]) / p
    out[p - 1] = v
    for i in range(p, len(xs)):
        v = a * xs[i] + (1 - a) * v
        out[i] = v
    return out


def _rsi(xs: list[float], p: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) <= p:
        return out
    gains = [max(0.0, xs[i] - xs[i - 1]) for i in range(1, len(xs))]
    losses = [max(0.0, xs[i - 1] - xs[i]) for i in range(1, len(xs))]
    ag = sum(gains[:p]) / p
    al = sum(losses[:p]) / p

    def val(g: float, l_: float) -> float:
        if l_ == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l_)

    out[p] = val(ag, al)
    for i in range(p + 1, len(xs)):
        ag = (ag * (p - 1) + gains[i - 1]) / p
        al = (al * (p - 1) + losses[i - 1]) / p
        out[i] = val(ag, al)
    return out


def _atr(h: list[float], lo: list[float], c: list[float], p: int) -> list[float | None]:
    n = len(c)
    out: list[float | None] = [None] * n
    if n <= p:
        return out
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, n)]
    v = sum(tr[1 : p + 1]) / p
    out[p] = v
    for i in range(p + 1, n):
        v = (v * (p - 1) + tr[i]) / p
        out[i] = v
    return out


def _rolling(xs: list[float], p: int, fn) -> list[float | None]:
    return [fn(xs[i - p + 1 : i + 1]) if i >= p - 1 else None for i in range(len(xs))]


def compute_series(ind: dict[str, Any], bars: dict[str, list[float]]) -> list[float | None]:
    t, p, src = ind["type"], ind["period"], ind["source"]
    xs = bars[src]
    if t == "sma":
        return _sma(xs, p)
    if t == "ema":
        return _ema(xs, p)
    if t == "rsi":
        return _rsi(xs, p)
    if t == "atr":
        return _atr(bars["high"], bars["low"], bars["close"], p)
    if t == "highest":
        return _rolling(xs, p, max)
    if t == "lowest":
        return _rolling(xs, p, min)
    if t == "return":
        return [xs[i] / xs[i - p] - 1 if i >= p and xs[i - p] else None for i in range(len(xs))]
    raise DslError([{"path": "indicators", "message": f"未知指标 {t}"}])


class _Frame:
    """一根 bar 时刻的求值环境：指标序列按需计算并缓存。"""

    def __init__(self, resolved: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        self.r = resolved
        self.cols = {k: [float(b[k]) for b in rows] for k in PRICE_FIELDS}
        self.n = len(rows)
        self.cache: dict[str, list[float | None]] = {}

    def series(self, name: str) -> list[float | None]:
        if name not in self.cache:
            self.cache[name] = compute_series(self.r["indicators"][name], self.cols)
        return self.cache[name]

    def value(self, o: dict[str, Any], extra_lag: int = 0) -> float | None:
        if "const" in o:
            return o["const"]
        idx = self.n - 1 - o["lag"] - extra_lag
        if idx < 0:
            return None
        v = self.cols[o["price"]][idx] if "price" in o else self.series(o["ref"])[idx]
        return None if v is None else v * o["mul"]

    def test(self, c: dict[str, Any] | None, lag: int = 0) -> bool | None:
        """返回 True / False；数据不足（指标未就绪）返回 None，视为不满足。"""
        if c is None:
            return False
        op = c["op"]
        if op in COMPARE_OPS:
            a, b = self.value(c["left"], lag), self.value(c["right"], lag)
            if a is None or b is None:
                return None
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        if op in CROSS_OPS:
            a1, b1 = self.value(c["left"], lag), self.value(c["right"], lag)
            a0, b0 = self.value(c["left"], lag + 1), self.value(c["right"], lag + 1)
            if None in (a0, a1, b0, b1):
                return None
            return (a0 <= b0 and a1 > b1) if op == "cross_above" else (a0 >= b0 and a1 < b1)
        if op == "all":
            vals = [self.test(x, lag) for x in c["conds"]]
            return None if any(v is None for v in vals) else all(vals)
        if op == "any":
            vals = [self.test(x, lag) for x in c["conds"]]
            return True if any(v for v in vals) else (None if any(v is None for v in vals) else False)
        if op == "not":
            v = self.test(c["cond"], lag)
            return None if v is None else not v
        if op == "consecutive":
            for k in range(c["bars"]):
                v = self.test(c["cond"], lag + k)
                if not v:
                    return v
            return True
        return None


def evaluate(resolved: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """在一段已收盘 bar 上判断入场 / 离场（供测试与表单“试算”使用）。"""
    f = _Frame(resolved, rows)
    return {
        "entry": bool(f.test(resolved["entry"])),
        "exit": bool(f.test(resolved["exit"])),
        "values": {k: (f.series(k)[-1] if f.n else None) for k in resolved["indicators"]},
    }


def _scores_from_frames(resolved: dict[str, Any], frames: dict[str, Any]) -> dict[str, float | None]:
    """从已构造的 `_Frame` 里取每个标的的排序得分（缺行情 / 指标未就绪 → None）。

    单列成函数是为了让"试算"与"回测 / 实时执行"走**同一段**取分逻辑：
    两条路径若各写一份，很容易在边界（预热不足、某标的数据缺失）上产生分歧。
    """
    by = (resolved.get("rank") or {}).get("by")
    out: dict[str, float | None] = {}
    for name in resolved.get("instruments") or [resolved["instrument"]]:
        frame = frames.get(name)
        if not by or frame is None:
            out[name] = None
            continue
        series = frame.series(by)
        out[name] = series[-1] if series else None
    return out


def rank_scores(
    resolved: dict[str, Any], rows_by_instrument: dict[str, list[dict[str, Any]]]
) -> dict[str, float | None]:
    """每个标的在当前时刻的排序得分（指标未就绪或没有行情时为 None）。

    只读各标的**已收盘**的 bar：`rows_by_instrument` 由调用方按"截至当前时刻"截断，
    这里不再做时间对齐，避免出现"用未来 bar 排名"的隐性泄漏。
    """
    frames = {name: _Frame(resolved, rows) for name, rows in rows_by_instrument.items() if rows}
    return _scores_from_frames(resolved, frames)


def select_rotation(resolved: dict[str, Any], scores: dict[str, float | None]) -> dict[str, Any]:
    """按得分选出前 N 名并分配权重（纯函数：不读时钟、不读账户，只依赖入参）。

    与 `rank_order()` 一样，这里是确定性的核心：同样的 scores 必然得到同样的 picks 与 weights。
    """
    rank = resolved["rank"]
    pos = resolved["position"]
    order = rank_order(scores, order=rank["order"])
    picks: list[str] = []
    for name in order:
        s = scores.get(name)
        if s is None:
            continue  # 数据不足：不参与排名，也不占用名额
        if rank.get("min_score") is not None and s <= rank["min_score"]:
            continue
        picks.append(name)
        if len(picks) >= rank["top"]:
            break
    raw = _rank_weights(picks, scores, rank["weighting"])
    # 再乘上 position.value（总仓位上限），最后按 max_weight 逐个裁剪。
    # 这里裁剪而**不重新归一化**：归一化会把超出上限的部分偷偷摊给别的标的，
    # 用户看到的"总仓位 1.0"就与实际下单不一致了——那属于静默改口径。
    weights = {name: min(w * pos["value"], pos["max_weight"]) for name, w in raw.items()}
    return {"order": order, "picks": picks, "weights": weights, "tie_break": rank["tie_break"]}


def evaluate_rotation(resolved: dict[str, Any], rows_by_instrument: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """轮动求值：算出每个标的的目标权重（0 表示不持有），以及名次与得分。

    返回的 `weights` 已经过 `max_weight` 裁剪；`order` 是**完整名次**（含未入选的标的），
    便于前端与测试解释"为什么选了它"。名次由 `rank_order()` 决定，平局按标的代码字典序，
    因此同样的输入必然得到同样的名次与同样的目标权重。
    """
    if not resolved.get("rank"):
        raise DslError([{"path": "rank", "message": "这份 DSL 没有 rank 节点，不能做轮动求值"}])
    scores = rank_scores(resolved, rows_by_instrument)
    return {"scores": scores, **select_rotation(resolved, scores)}


# ---------------------------------------------------------------------------
# 运行：鸭子类型实现 StrategyModule 接口，由同一套 runner / runtime 驱动
# ---------------------------------------------------------------------------
class DslModule:
    """DSL 策略的执行体。没有任何用户代码：只解释已校验的语法树。"""

    lang = "dsl"

    def __init__(
        self, compiled: CompiledDsl, *, name: str = "dsl_strategy", params: dict[str, Any] | None = None
    ) -> None:
        self.compiled = compiled
        self.r = compiled.resolved
        self.name = name
        self.path = None
        self.filename = f"<dsl:{name}>"
        self.params = {**(compiled.spec.get("params") or {}), **(params or {})}
        self.source = canonical(compiled.spec)
        self.scheduled: list = []
        # 轮动专用：上一根 bar 算出的目标权重（按标的）。放在模块实例上而不是 g 里，
        # 是因为它不是"策略状态"而是解释器的执行记忆；检查点恢复时由 initialize 重建，
        # 只要保证"恢复后第一根一律重新对齐一次"即可，不会漏单。
        self._last_targets: dict[str, float] = {}
        self._fns = {"initialize": self._initialize, "handle_data": self._handle_data}

    def fn(self, name: str):
        return self._fns.get(name)

    def has(self, name: str) -> bool:
        return name in self._fns

    def _initialize(self, ctx: Any) -> None:
        from ..sdk import api

        r = self.r
        pos, risk = r["position"], r["risk"]
        api.set_option("insufficient_funds", pos["insufficient_funds"])
        api.set_option("qty_step", pos["qty_step"])
        api.set_option("max_position_weight", pos["max_weight"])
        if risk["max_trades_per_day"]:
            api.set_option("max_trades_per_day", risk["max_trades_per_day"])
        if risk["drawdown_pause"]:
            api.set_option("drawdown_pause", risk["drawdown_pause"])
        if r.get("rotation"):
            # 轮动：止损 / 止盈要覆盖**每一个**标的，否则换手后新买入的标的没有保护。
            for inst in self._universe(ctx):
                if risk["stop_loss"]:
                    api.set_stop_loss(inst, risk["stop_loss"])
                if risk["take_profit"]:
                    api.set_take_profit(inst, risk["take_profit"])
            # initialize 在检查点恢复时也会重跑：清空"上一根目标"，
            # 让恢复后的第一根 bar 无条件重新对齐一次仓位，避免因状态丢失而漏单。
            self._last_targets = {}
            return
        inst = self._instrument(ctx)
        if risk["stop_loss"]:
            api.set_stop_loss(inst, risk["stop_loss"])
        if risk["take_profit"]:
            api.set_take_profit(inst, risk["take_profit"])

    def _universe(self, ctx: Any) -> list[str]:
        """实际可交易的标的池：以运行配置为准，回落到 DSL 声明的 instruments。

        回测 / 实时模拟的标的由运行清单决定（`ctx.securities`）。DSL 声明的标的若不在本次
        运行的行情里，读它的 bar 只会得到空列表，排名里恒为 None —— 那等于静默少买，
        所以这里取"声明过的"与"本次运行有的"交集，只对真有行情的标的下单。
        """
        declared = self.r.get("instruments") or [self.r["instrument"]]
        secs = [s for s in (getattr(ctx, "securities", None) or []) if s]
        if not secs:
            return list(declared)
        return [s for s in declared if s in secs]

    def _instrument(self, ctx: Any) -> str:
        want = self.r["instrument"]
        secs = list(getattr(ctx, "securities", []) or [])
        return want if want in secs or not secs else secs[0]

    def _handle_data(self, ctx: Any) -> None:
        if self.r.get("rotation"):
            self._handle_rotation(ctx)
            return
        from ..sdk import api

        inst = self._instrument(ctx)
        rows = _rows(api.history(inst, self.r["window"], list(PRICE_FIELDS)))
        if not rows:
            return
        frame = _Frame(self.r, rows)
        held = ctx.position(inst).amount > 0
        pos = self.r["position"]
        if held and frame.test(self.r["exit"]):
            api.order_target(inst, 0, reason="dsl_exit")
        elif not held and frame.test(self.r["entry"]) and not frame.test(self.r["exit"]):
            if pos["mode"] == "weight":
                api.order_target_percent(inst, pos["value"], reason="dsl_entry")
            else:
                api.order_target(inst, pos["value"], reason="dsl_entry")

    def _handle_rotation(self, ctx: Any) -> None:
        """轮动：先按指标排序选出目标持仓，再对**每一个**标的把仓位调到目标值。

        下单规则（三条，合起来既能跟住名次、又不会每根 bar 刷单）：

        1. 目标权重与上一根**不同** → 调仓（名次变化、入选 / 掉出、离场条件触发）；
        2. 目标为正但**当前空仓** → 建仓。这条是为了处理"仓位被止损 / 止盈打掉"：
           目标权重没变，但持仓已经归零，若不补这一条策略会永远空仓——
           跑起来不报错、只是再也不交易，属于最难发现的静默故障。
        3. 目标为零但**仍有持仓** → 清仓。

        其余情况（目标没变且持仓状态一致）不下单：每根 bar 无脑对齐虽然也能工作，
        但会产生大量"差额取整后为 0"的委托（被 ZERO_QUANTITY 拒绝），
        既污染信号与拒绝记录，又让成交序列依赖调仓容差这种成本参数。

        **先卖后买**，且各组内按标的代码字典序：

        - 先卖后买不是风格问题。换手时资金全在旧标的上，如果先提交买单，平台在下一根
          开盘撮合时买单排在卖单前面、可用现金仍是 0，买单会因资金不足被拒（默认
          `insufficient_funds=reject` 是**拒绝**而不是缩量），于是换手静默失败——
          策略看起来在跑，实际再也不会买入新标的。先卖后买才能让卖出回流的现金供买入使用。
        - 组内顺序固定为字典序：资金不足时先处理的标的会先占用现金，
          顺序若不确定，"逐笔成交一致"就不再成立。
        """
        from ..sdk import api

        universe = self._universe(ctx)
        if not universe:
            return
        frames: dict[str, Any] = {}
        rows_by_instrument: dict[str, list[dict[str, Any]]] = {}
        for name in universe:
            rows = _rows(api.history(name, self.r["window"], list(PRICE_FIELDS)))
            if rows:
                rows_by_instrument[name] = rows
                frames[name] = _Frame(self.r, rows)
        if not frames:
            return
        scores = _scores_from_frames(self.r, frames)
        plan = select_rotation(self.r, scores)
        targets = plan["weights"]

        previous = self._last_targets
        current: dict[str, float] = {}
        changes: list[tuple[str, float, bool]] = []  # (标的, 目标权重, 是否卖出)
        for name in sorted(universe):
            frame = frames.get(name)
            if frame is None:
                continue  # 该标的当前没有可用行情，不动它（不猜价格）
            held = ctx.position(name).amount > 0
            target = targets.get(name, 0.0)
            # 离场条件优先：排名仍入选但触及离场条件时清仓，与单标的版本"离场优先"一致。
            if target > 0 and frame.test(self.r["exit"]):
                target = 0.0
            if target > 0 and not held and not frame.test(self.r["entry"]):
                target = 0.0  # 入场条件未满足：不建仓（离场条件已在上一步判断过）
            current[name] = target
            # 上一根没有记录时按"当时空仓"处理（`_last_targets` 初始为空、恢复后也会清空）。
            # 若拿 `None` 去比，`target != None` 恒为真，会在第一根 bar 上给每个标的都
            # 发一个目标为 0 的信号——全是 ZERO_QUANTITY 拒绝，纯噪音。
            before = previous.get(name, 0.0)
            changed = target != before
            # 空仓要建、有仓要清：这两种"目标与持仓不符"必须下单，否则会永远停在那儿
            needs_action = (target > 0 and not held) or (target <= 0 and held)
            if not changed and not needs_action:
                continue
            changes.append((name, target, target < before))

        self._last_targets = current
        # 卖出组排在前面；组内按标的代码字典序。
        for name, target, _is_sell in sorted(changes, key=lambda c: (not c[2], c[0])):
            api.order_target_percent(name, target, reason="dsl_rotation" if target > 0 else "dsl_exit")


def _rows(data: Any) -> list[dict[str, Any]]:
    """把 attribute_history 的返回统一成 [{open, high, low, close}, ...]。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "close" in data:
        n = len(data["close"])
        return [{k: data[k][i] for k in PRICE_FIELDS} for i in range(n)]
    return []


def load_dsl(
    source: str | dict[str, Any], *, name: str = "dsl_strategy", params: dict[str, Any] | None = None
) -> DslModule:
    spec = parse_dsl_text(source) if isinstance(source, str) else source
    return DslModule(validate(spec, params=params), name=name, params=params)
