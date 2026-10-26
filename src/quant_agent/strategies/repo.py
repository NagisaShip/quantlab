"""策略文件仓库：每个策略 = 一个目录。

对标聚宽的"一个策略一个代码文件"，但把它做实到文件系统层面：

    strategies/<slug>/
        strategy.py     策略源码（人类可读、可编辑、可 git diff）
        meta.json       元数据：名称、团队、类型、来源、版本、状态、批准人
        params.json     参数（规则策略的规则也在这里）
        versions/       每次保存留下快照，可回滚、可对照

这样做的好处很直接：
- 策略是文件，可以进 git、可以 code review、可以离线编辑。
- 平台重启或换机器，策略不丢；导出就是一个能独立运行的 .py。
- "改代码生成新版本"变成一次目录快照，原版本文件不被覆盖。

规则策略（JSON）也会被**编译成一份等价的 Python 源码**写进 strategy.py：
既能被平台解释执行，也能被人读、被人改，改完就自然变成一份代码策略。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / ".quant_agent" / "strategies"

STRATEGY_FILE = "strategy.py"
DSL_FILE = "strategy.json"  # 规则 DSL v2 策略：结构化 JSON（不生成 Python 代码）
META_FILE = "meta.json"
PARAMS_FILE = "params.json"
VERSIONS_DIR = "versions"
SPEC_FILE = "SPEC.md"

# 保存钩子：平台层在这里登记「谁（人还是 Agent）改出了哪个版本」（编辑人员标记、策略血缘图）。
# 命令行与单元测试里没有注册时什么都不做；钩子出错不影响保存本身。
SAVE_HOOKS: list[Any] = []

SLUG_RE = re.compile(r"[^a-zA-Z0-9_\-]+")
VALID_SLUG = re.compile(r"[a-z0-9][a-z0-9_\-]{0,80}")


def slugify(name: str) -> str:
    """把策略名转成安全的目录名。中文名会退化成哈希后缀，避免路径乱码。"""
    ascii_part = SLUG_RE.sub("-", name).strip("-_").lower()
    if not ascii_part:
        ascii_part = "strategy"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]
    return f"{ascii_part[:40]}-{digest}"


@dataclass
class StrategyMeta:
    """策略元数据。写在 meta.json 里，跟着策略目录走。"""

    id: str
    slug: str
    name: str
    team: str = "未标注团队"
    kind: str = "code"  # dsl | code | rule | template（dsl 为默认推荐方式）
    source: str = "user"  # user | agent | builtin
    status: str = "draft"  # draft → tested → backtested → live → retired
    human_approved: bool = False
    version: str = ""
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    securities: list[str] = field(default_factory=list)
    notes: str = ""
    prompt_hash: str = ""
    trace_id: str = ""
    author: str = ""
    # 从 .qstrat 导入的策略：包的来源与标准答案核对结果（golden_match=False 的不能上交易所模拟盘）
    package: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyMeta:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class StrategyRecord:
    """读出来的一条策略：元数据 + 源码 + 参数。"""

    meta: StrategyMeta
    source: str
    params: dict[str, Any] = field(default_factory=dict)
    path: str = ""
    version_count: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            **self.meta.to_dict(),
            "path": self.path,
            "params": self.params,
            "version_count": self.version_count,
            "issues": self.issues,
        }
        if include_source:
            out["code"] = self.source
        else:
            out["code_lines"] = self.source.count("\n") + 1
        return out


class StrategyRepository:
    """文件系统策略仓库。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------- 路径 ----------------
    def dir_of(self, slug: str) -> Path:
        # slug 来自 URL：只允许安全字符，杜绝 ".." 之类的路径穿越（delete 会 rmtree）
        if not slug or not VALID_SLUG.fullmatch(slug):
            raise ValueError(f"非法的策略标识：{slug!r}")
        return self.root / slug

    def _ensure(self, slug: str) -> Path:
        d = self.dir_of(slug)
        (d / VERSIONS_DIR).mkdir(parents=True, exist_ok=True)
        return d

    def exists(self, slug: str) -> bool:
        try:
            return (self.dir_of(slug) / META_FILE).exists()
        except ValueError:
            return False

    def unique_slug(self, name: str) -> str:
        """新建策略用：同名时自动加序号，不覆盖已有策略。"""
        base = slugify(name)
        slug, n = base, 2
        while self.exists(slug):
            slug = f"{base}-{n}"
            n += 1
        return slug

    # ---------------- 读写 ----------------
    def list(self, *, kind: str | None = None) -> list[StrategyRecord]:
        out: list[StrategyRecord] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not VALID_SLUG.fullmatch(d.name):
                continue
            rec = self.get(d.name)
            if rec is None:
                continue
            if kind and rec.meta.kind != kind:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.meta.updated_at, reverse=True)
        return out

    def get(self, slug: str) -> StrategyRecord | None:
        try:
            d = self.dir_of(slug)
        except ValueError:
            return None
        meta_path = d / META_FILE
        code_path = d / DSL_FILE if (d / DSL_FILE).exists() else d / STRATEGY_FILE
        if not meta_path.exists() or not code_path.exists():
            return None
        try:
            meta = StrategyMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None
        params_path = d / PARAMS_FILE
        params: dict[str, Any] = {}
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                params = {}
        versions = (
            [
                p
                for p in (d / VERSIONS_DIR).glob("*")
                if p.suffix in (".py", ".json") and not p.name.endswith(".params.json")
            ]
            if (d / VERSIONS_DIR).exists()
            else []
        )
        return StrategyRecord(
            meta=meta,
            source=code_path.read_text(encoding="utf-8"),
            params=params,
            path=str(code_path),
            version_count=len(versions),
        )

    def save(
        self,
        *,
        name: str,
        source: str,
        params: dict[str, Any] | None = None,
        team: str = "未标注团队",
        kind: str = "code",
        origin: str = "user",
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        securities: list[str] | None = None,
        notes: str = "",
        slug: str | None = None,
        human_approved: bool = False,
        prompt_hash: str = "",
        trace_id: str = "",
        snapshot: bool = True,
        author: str = "",
        package: dict[str, Any] | None = None,
    ) -> StrategyRecord:
        """新建或覆盖保存。覆盖时先把旧版本快照到 versions/。"""
        slug = slug or slugify(name)
        d = self._ensure(slug)
        code_path = d / (DSL_FILE if kind == "dsl" else STRATEGY_FILE)
        other = d / (STRATEGY_FILE if kind == "dsl" else DSL_FILE)
        meta_path = d / META_FILE

        previous: StrategyMeta | None = None
        if meta_path.exists():
            try:
                previous = StrategyMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError):
                previous = None

        version = _content_version(source, params or {})
        if snapshot and code_path.exists() and previous is not None and previous.version != version:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            shutil.copy2(code_path, d / VERSIONS_DIR / f"{previous.version or 'v0'}-{stamp}{code_path.suffix}")
            old_params = d / PARAMS_FILE
            if old_params.exists():
                shutil.copy2(old_params, d / VERSIONS_DIR / f"{previous.version or 'v0'}-{stamp}.params.json")

        code_path.write_text(source, encoding="utf-8")
        if other.exists():
            # 语言切换（例如 DSL 转成 Python）：旧文件进版本快照，不留两份当前源码
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            shutil.move(
                str(other),
                str(d / VERSIONS_DIR / f"{(previous.version if previous else 'v0') or 'v0'}-{stamp}{other.suffix}"),
            )
        (d / PARAMS_FILE).write_text(json.dumps(params or {}, ensure_ascii=False, indent=2), encoding="utf-8")

        meta = StrategyMeta(
            id=slug,
            slug=slug,
            name=name,
            team=team,
            kind=kind,
            source=origin,
            status=(previous.status if previous and previous.version == version else "draft"),
            # 批准绑定到具体版本：代码或参数一改，版本号变化，批准自动失效，需要重新审核
            human_approved=human_approved or bool(previous and previous.human_approved and previous.version == version),
            version=version,
            symbol=symbol,
            timeframe=timeframe,
            securities=securities or [symbol],
            notes=notes,
            prompt_hash=prompt_hash,
            trace_id=trace_id,
            author=author or (previous.author if previous else ""),
            # 包信息绑定到版本：导入后再改代码，核对结果就不再代表当前内容
            package=(
                package
                if package is not None
                else (previous.package if previous and previous.version == version else {})
            ),
            created_at=previous.created_at if previous else int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
        )
        meta_path.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        rec = self.get(slug)
        assert rec is not None
        if previous is None or previous.version != version:
            for hook in SAVE_HOOKS:
                try:
                    hook(self, rec, previous.version if previous else "", origin)
                except Exception:
                    import logging

                    logging.getLogger("quantlab.repo").exception("策略保存钩子失败：%s", slug)
        return rec

    def update_meta(self, slug: str, **changes: Any) -> StrategyRecord | None:
        rec = self.get(slug)
        if rec is None:
            return None
        data = rec.meta.to_dict()
        data.update(changes)
        data["updated_at"] = int(time.time() * 1000)
        (self.dir_of(slug) / META_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get(slug)

    def delete(self, slug: str) -> bool:
        try:
            d = self.dir_of(slug)
        except ValueError:
            return False
        if not d.exists():
            return False
        shutil.rmtree(d)
        return True

    def versions(self, slug: str) -> list[dict[str, Any]]:
        """列出历史版本（按时间倒序）。"""
        try:
            d = self.dir_of(slug) / VERSIONS_DIR
        except ValueError:
            return []
        if not d.exists():
            return []
        out: list[dict[str, Any]] = []
        files = [p for p in d.glob("*") if p.suffix in (".py", ".json") and not p.name.endswith(".params.json")]
        for path in sorted(files, key=lambda p: p.name, reverse=True):
            stat = path.stat()
            out.append(
                {
                    "version": path.stem.split("-")[0],
                    "file": path.name,
                    "saved_at": int(stat.st_mtime * 1000),
                    "bytes": stat.st_size,
                    "lines": path.read_text(encoding="utf-8").count("\n") + 1,
                }
            )
        return out

    def read_version(self, slug: str, filename: str) -> str | None:
        """读某个历史版本的源码。文件名必须来自 versions()，防止路径穿越。"""
        try:
            d = self.dir_of(slug) / VERSIONS_DIR
        except ValueError:
            return None
        target = (d / filename).resolve()
        if not str(target).startswith(str(d.resolve())) or not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    def read_spec(self, slug: str) -> str:
        """策略说明书（SPEC.md）。没写过返回空字符串。"""
        p = self.dir_of(slug) / SPEC_FILE
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write_spec(self, slug: str, text: str) -> None:
        d = self.dir_of(slug)
        if not (d / META_FILE).exists():
            raise FileNotFoundError(slug)
        (d / SPEC_FILE).write_text(text, encoding="utf-8")

    def export(self, slug: str) -> str | None:
        """导出成一份可以独立运行的 .py（带文件头注释）。"""
        rec = self.get(slug)
        if rec is None:
            return None
        if rec.meta.kind == "dsl":
            return rec.source
        header = (
            f'"""\n'
            f"{rec.meta.name}\n"
            f"团队：{rec.meta.team}\n"
            f"类型：{rec.meta.kind}　来源：{rec.meta.source}　版本：{rec.meta.version}\n"
            f"标的：{rec.meta.symbol}　周期：{rec.meta.timeframe}\n"
            f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"运行方式：\n"
            f"    from quant_agent.sdk import load_file, run_backtest\n"
            f'    result = run_backtest(load_file("strategy.py"), {{"BTC/USDT": bars}})\n'
            f'"""\n'
        )
        return header + rec.source


def _content_version(source: str, params: dict[str, Any]) -> str:
    """源码 + 参数的 SHA256 前 12 位。任一改变即产生新版本号。"""
    blob = source + "\n---\n" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 规则策略 → Python 源码
# ---------------------------------------------------------------------------
RULE_STRATEGY_TEMPLATE = '''"""{name}

由平台从 JSON 规则编译生成。规则策略与代码策略走完全相同的执行路径，
这份源码可以读、可以改；改完保存，它就变成一份代码策略。

买入条件：{buy}
卖出条件：{sell}
参数：快均线 {fast} / 慢均线 {slow} / RSI {period} / 目标仓位 {weight:.0%}

可用指标：close（收盘价）、sma_fast、sma_slow、rsi、position（当前持仓占净值比例）

语义（与平台规则解释器完全一致）：
- 卖出条件优先：买卖同时满足时离场。
- 无信号时**保持当前持仓数量**，不做无意义调仓。
- 只在信号发生变化时下单，因此用 g.target 记录上一次的目标仓位。
"""

FAST = {fast}
SLOW = {slow}
PERIOD = {period}
WEIGHT = {weight}


def initialize(ctx):
    """策略启动：设置基准与成本假设，注册每根 bar 的回调。"""
    set_benchmark(ctx.securities[0])
    set_order_cost({{"taker_fee": 0.001, "slippage_bps": 5}})
    g.target = 0.0          # 上一次的目标仓位；None 表示"还没建过仓"
    g.in_position = False
    run_daily(handle_bar, time="close")


def handle_bar(ctx):
    security = ctx.securities[0]
    closes = history(security, SLOW, "close")
    if len(closes) < SLOW:
        return

    close = closes[-1]
    sma_fast = sum(closes[-FAST:]) / FAST
    sma_slow = sum(closes) / SLOW
    rsi = _rsi(closes, PERIOD)
    position = ctx.position(security).amount * close / ctx.total_value if ctx.total_value else 0.0

    record(sma_fast=sma_fast, sma_slow=sma_slow, rsi=rsi)

    # 卖出优先；两个条件都不满足时保持现状
    if _eval_sell(close, sma_fast, sma_slow, rsi, position):
        if g.in_position:
            order_target_percent(security, 0.0, reason="sell_rule")
            g.in_position = False
    elif _eval_buy(close, sma_fast, sma_slow, rsi, position):
        if not g.in_position:
            order_target_percent(security, WEIGHT, reason="buy_rule")
            g.in_position = True


def _rsi(closes, period):
    """涨跌幅简单平均的 RSI（非 Wilder 平滑）。"""
    if len(closes) < period + 1:
        return 50.0
    changes = [b - a for a, b in zip(closes[-period - 1:], closes[-period:])]
    gains = sum(max(x, 0.0) for x in changes)
    losses = sum(max(-x, 0.0) for x in changes)
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _eval_buy(close, sma_fast, sma_slow, rsi, position):
    return bool({buy_expr})


def _eval_sell(close, sma_fast, sma_slow, rsi, position):
    return bool({sell_expr})
'''

# 规则里的指标名 → 编译后 Python 中的局部变量名（同名，但显式列出来便于校验）
_RULE_VARS = ("close", "sma_fast", "sma_slow", "rsi", "position")


def rule_to_python(spec: Any) -> str:
    """把一份 RuleSpec 编译成等价的 Python 策略源码。

    只做名字替换与语法校验，不拼接任何用户可控的语句——表达式本身已经被
    `rules.parse_rule` 用 AST 白名单校验过，这里的输出必然是一组合法的比较表达式。
    """
    from ..strategies.rules import _normalize  # 全角符号规范化，与解释器口径一致

    buy = _normalize(spec.buy)
    sell = _normalize(spec.sell)
    _assert_expression_safe(buy)
    _assert_expression_safe(sell)
    return RULE_STRATEGY_TEMPLATE.format(
        name=spec.name,
        buy=buy,
        sell=sell,
        fast=spec.fast,
        slow=spec.slow,
        period=spec.period,
        weight=spec.weight,
        buy_expr=buy,
        sell_expr=sell,
    )


def _assert_expression_safe(expr: str) -> None:
    """编译前再确认一次：表达式只能由指标名、数字、比较与逻辑词组成。"""
    allowed_words = set(_RULE_VARS) | {"and", "or", "not", "True", "False"}
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", expr)
    unknown = [t for t in tokens if t not in allowed_words]
    if unknown:
        raise ValueError(f"规则表达式中出现未授权的名字：{', '.join(sorted(set(unknown)))}")
    if any(bad in expr for bad in ("__", "import", "lambda", "exec", "eval", ";")):
        raise ValueError("规则表达式包含不允许的构造")
