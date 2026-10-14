"""策略包（.qstrat）：每个人的策略用同一种文件交换。

一个 .qstrat 是一个 zip，里面只允许这几样东西：

    manifest.toml   名称、作者、版本、SDK 接口版本、市场类型、标的、周期、参数、数据依赖、默认风控
    strategy.py     策略源码（DSL 策略为 strategy.json）
    params.json     参数
    SPEC.md         策略说明书：逻辑、适用行情、什么时候该停
    golden.json     标准答案：在包内数据片段上的回测指纹
    data/*.csv      跑标准答案用的数据片段（K 线）
    lineage.json    （可选）研究纪律记录：试验次数、DSR、紧封测试结果

为什么要有“标准答案”：导入方在自己的机器上用同一份数据重跑一遍，指纹对得上，
才说明拿到的就是作者跑的那一份代码和参数——没有被改过，也不依赖作者机器上的东西。
指纹对不上的包照样可以导入、可以回测，但不能进入交易所模拟盘。

导入是不可信输入：zip 条目数、单个文件大小、总大小都有上限；只认白名单文件名，
不解压到磁盘（全部在内存里读），所以不存在路径穿越。代码先过静态安全检查，
再在隔离子进程里试跑。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import tomllib

from .. import __version__
from ..data.quality import TIMEFRAME_MS
from ..exchanges.base import Bar

FORMAT = "qstrat/1"
#: 策略 SDK 的接口版本。只在 SDK 出现不兼容变更时加 1；包里的版本比平台新就拒绝运行。
SDK_API = 1
EXTENSION = ".qstrat"

#: 标准答案的数据片段：固定起点、固定长度的确定性行情。任何机器上生成的都一样。
GOLDEN_START = 1_704_067_200_000  # 2024-01-01 00:00 UTC
GOLDEN_BARS = 400
GOLDEN_WARMUP = 60
GOLDEN_CASH = 10_000.0

MAX_ENTRIES = 64
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 8_000_000
_ALLOWED = re.compile(
    r"^(manifest\.toml|strategy\.py|strategy\.json|params\.json|SPEC\.md|golden\.json|"
    r"lineage\.json|data/[A-Za-z0-9_\-]+\.csv)$"
)

#: 源码里出现这些调用，说明策略依赖平台上的研究资产；导入方没有同名资产就跑不起来
_REQUIRES = {
    "factor(": "factor",
    "predict(": "model",
    "optimize(": "portfolio",
    "set_execution(": "execution",
}


class PackageError(ValueError):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    level: str = "error"  # error：不能导入 | warning：能导入但有限制

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "level": self.level}


@dataclass
class Inspection:
    """导入前的检查报告。`importable` 决定能不能入库，`demo_ready` 决定能不能上模拟盘。"""

    manifest: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    spec: str = ""
    lineage: dict[str, Any] | None = None
    golden: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] | None = None
    checks: list[Check] = field(default_factory=list)
    data: dict[str, list[Bar]] = field(default_factory=dict)

    @property
    def importable(self) -> bool:
        return all(c.ok for c in self.checks if c.level == "error")

    @property
    def code_match(self) -> bool:
        return bool(self.golden.get("code_hash")) and self.golden.get("code_hash") == code_hash(
            self.source, self.params
        )

    @property
    def golden_match(self) -> bool:
        return self.code_match and bool(self.actual) and self.actual.get("hash") == self.golden.get("hash")

    @property
    def demo_ready(self) -> bool:
        return self.importable and self.golden_match

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "params": self.params,
            "spec": self.spec,
            "lineage": self.lineage,
            "golden": self.golden,
            "actual": self.actual,
            "golden_match": self.golden_match,
            "code_match": self.code_match,
            "importable": self.importable,
            "demo_ready": self.demo_ready,
            "checks": [c.to_dict() for c in self.checks],
            "source_lines": self.source.count("\n") + 1 if self.source else 0,
        }


# ---------------------------------------------------------------------------
# 数据片段与指纹
# ---------------------------------------------------------------------------
def golden_data(securities: list[str], timeframe: str) -> dict[str, list[Bar]]:
    """标准答案用的确定性行情（与“演示行情”同一个生成函数，按时间点确定）。"""
    from ..engine.live import clock_synthetic_bars

    step = TIMEFRAME_MS.get(timeframe, 3_600_000)
    end = GOLDEN_START + GOLDEN_BARS * step
    return {s: clock_synthetic_bars(s, timeframe, GOLDEN_START, end) for s in securities}


def bars_to_csv(bars: list[Bar]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["open_time", "close_time", "open", "high", "low", "close", "volume"])
    for b in bars:
        w.writerow([b.open_time, b.close_time, repr(b.open), repr(b.high), repr(b.low), repr(b.close), repr(b.volume)])
    return buf.getvalue()


def csv_to_bars(text: str, symbol: str, timeframe: str) -> list[Bar]:
    out: list[Bar] = []
    rows = csv.DictReader(io.StringIO(text))
    for i, r in enumerate(rows):
        try:
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=int(r["open_time"]),
                    close_time=int(r["close_time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["volume"]),
                    closed=True,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageError(f"数据片段 {symbol} 第 {i + 2} 行格式不对：{exc}") from exc
    return out


def _sym_file(symbol: str) -> str:
    return "data/" + re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_") + ".csv"


def fingerprint(body: dict[str, Any]) -> dict[str, Any]:
    """回测结果的指纹：成交逐笔（时间、标的、方向、数量、价格）+ 期末权益。

    数量和价格先规整到固定位数再哈希：同一份代码在不同 CPU 上的浮点尾数差异不应该算“不一致”。
    """
    fills = body.get("fills") or []
    rows = [
        [
            int(f.get("ts") or 0),
            f.get("security") or "",
            f.get("side") or "",
            f"{float(f.get('amount') or 0):.8f}",
            f"{float(f.get('price') or 0):.6f}",
        ]
        for f in fills
    ]
    eq = body.get("equity_curve") or []
    final = f"{float(eq[-1]):.4f}" if eq else "0"
    blob = json.dumps({"fills": rows, "final_equity": final}, separators=(",", ":"))
    return {
        "fills": int(body.get("fills_total") or len(fills)),
        "final_equity": final,
        "hash": hashlib.sha256(blob.encode()).hexdigest()[:24],
        "errors": len(body.get("errors") or []),
    }


def code_hash(source: str, params: dict[str, Any] | None) -> str:
    """代码 + 参数的哈希：标准答案和它绑定，改一个字符都会对不上。"""
    blob = source.replace("\r\n", "\n") + "\n---\n" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def run_golden(
    *, lang: str, source: str, name: str, params: dict[str, Any] | None, data: dict[str, list[Bar]], timeframe: str
) -> dict[str, Any]:
    """在隔离子进程里把策略在数据片段上跑一遍，返回指纹。"""
    from ..engine.research import BacktestSpec
    from ..platform import isolation
    from ..platform.sandbox_exec import bars_to_wire

    spec = BacktestSpec(
        securities=list(data),
        timeframe=timeframe,
        bars_limit=max((len(v) for v in data.values()), default=0),
        warmup_bars=GOLDEN_WARMUP,
        initial_cash=GOLDEN_CASH,
        source="csv",
        params=dict(params or {}),
    )
    body = isolation.run_batch(
        {
            "op": "backtest",
            "lang": lang,
            "source": source,
            "name": name,
            "spec": spec.to_dict(),
            "bars": bars_to_wire(data),
            "params": params or None,
            "run_id": "golden",
            "strategy_version_id": "",
            "time_limit": 60.0,
            "max_rows": 100_000,
        },
        timeout=120,
    )
    return {**fingerprint(body), "code_hash": code_hash(source, params)}


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
def requirements_of(source: str) -> list[str]:
    return sorted({v for k, v in _REQUIRES.items() if k in source})


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)  # JSON 字符串转义是 TOML 基本字符串的子集


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_\-]+", k) else json.dumps(k, ensure_ascii=False)


def dump_manifest(m: dict[str, Any]) -> str:
    lines = ["# 策略包清单（qstrat/1）。改动任何字段都不影响标准答案的校验：校验只看代码、参数和数据。"]
    tables = {k: v for k, v in m.items() if isinstance(v, dict)}
    for k, v in m.items():
        if k not in tables:
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    for k, v in tables.items():
        lines.append("")
        lines.append(f"[{_toml_key(k)}]")
        for kk, vv in v.items():
            lines.append(f"{_toml_key(kk)} = {_toml_value(vv)}")
    return "\n".join(lines) + "\n"


def default_spec(
    *, name: str, notes: str, securities: list[str], timeframe: str, params: dict[str, Any], source: str
) -> str:
    """没有写过说明书的策略：从元数据和源码文档字符串拼一份初稿，提醒作者补全。"""
    doc = ""
    m = re.match(r'\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', source, re.S)
    if m:
        doc = m.group(1).strip()
    rows = "\n".join(f"| {k} | {json.dumps(v, ensure_ascii=False)} |" for k, v in params.items()) or "| （无） | |"
    return (
        f"# {name}\n\n"
        f"标的：{'、'.join(securities)}　周期：{timeframe}\n\n"
        f"## 策略逻辑\n\n{doc or notes or '（作者还没有写。请说明入场、出场和仓位规则。）'}\n\n"
        f"## 参数\n\n| 参数 | 默认值 |\n| --- | --- |\n{rows}\n\n"
        "## 适用行情\n\n（作者还没有写。）\n\n"
        "## 什么时候应该停\n\n（作者还没有写。例如：回撤超过多少、连续亏损多少次、市场状态变成什么。）\n"
    )


def build(
    *,
    name: str,
    slug: str,
    kind: str,
    source: str,
    params: dict[str, Any],
    securities: list[str],
    timeframe: str,
    author: str = "",
    team: str = "",
    version: str = "",
    notes: str = "",
    spec: str | None = None,
    lineage: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    golden: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """打包。`golden` 不传就现场在隔离进程里跑一次标准答案。"""
    from .lang import lang_of

    lang = lang_of(kind)
    securities = list(securities) or ["BTC/USDT"]
    data = golden_data(securities, timeframe)
    if golden is None:
        golden = run_golden(lang=lang, source=source, name=slug, params=params, data=data, timeframe=timeframe)
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "name": name,
        "slug": slug,
        "version": version,
        "author": author,
        "team": team,
        "kind": kind,
        "lang": lang,
        "sdk_api": SDK_API,
        "platform": __version__,
        "market": "spot",
        "securities": securities,
        "timeframe": timeframe,
        "requires": requirements_of(source),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "golden": {"start": GOLDEN_START, "bars": GOLDEN_BARS, "warmup": GOLDEN_WARMUP, "cash": GOLDEN_CASH},
    }
    if risk:
        manifest["risk"] = {k: v for k, v in risk.items() if isinstance(v, (int, float, bool, str))}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.toml", dump_manifest(manifest))
        z.writestr("strategy.json" if lang == "dsl" else "strategy.py", source)
        z.writestr("params.json", json.dumps(params or {}, ensure_ascii=False, indent=2))
        z.writestr(
            "SPEC.md",
            spec
            or default_spec(
                name=name, notes=notes, securities=securities, timeframe=timeframe, params=params or {}, source=source
            ),
        )
        z.writestr("golden.json", json.dumps(golden, ensure_ascii=False, indent=2))
        for sec, bars in data.items():
            z.writestr(_sym_file(sec), bars_to_csv(bars))
        if lineage:
            z.writestr("lineage.json", json.dumps(lineage, ensure_ascii=False, indent=2))
    return buf.getvalue(), manifest


# ---------------------------------------------------------------------------
# 导入检查
# ---------------------------------------------------------------------------
def read_entries(data: bytes) -> dict[str, str]:
    """安全地读出 zip 里的白名单文件（全部在内存里，不落盘）。"""
    if len(data) > MAX_TOTAL_BYTES:
        raise PackageError(f"策略包太大（{len(data) // 1024} KB），上限 {MAX_TOTAL_BYTES // 1024} KB")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageError("不是有效的 .qstrat 文件（不是 zip 格式）") from exc
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        raise PackageError(f"策略包里文件太多（{len(infos)} 个），上限 {MAX_ENTRIES}")
    out: dict[str, str] = {}
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        if not _ALLOWED.match(info.filename):
            raise PackageError(f"策略包里有不认识的文件：{info.filename[:80]}")
        if info.file_size > MAX_FILE_BYTES:
            raise PackageError(f"{info.filename} 太大（{info.file_size // 1024} KB）")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise PackageError("策略包解压后总大小超过上限")
        with z.open(info) as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:  # 不信任 zip 头里自报的大小
            raise PackageError(f"{info.filename} 太大")
        try:
            out[info.filename] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError(f"{info.filename} 不是 UTF-8 文本") from exc
    return out


def inspect(data: bytes, *, run: bool = True) -> Inspection:
    """导入前检查：格式 → 兼容性 → 安全检查 → 数据依赖 → 隔离试跑并比对标准答案。"""
    from .lang import issues, lang_of

    rep = Inspection()
    files = read_entries(data)

    try:
        manifest = tomllib.loads(files.get("manifest.toml", ""))
    except tomllib.TOMLDecodeError as exc:
        rep.checks.append(Check("清单格式", False, f"manifest.toml 解析失败：{exc}"))
        return rep
    rep.manifest = manifest
    fmt_ok = manifest.get("format") == FORMAT
    rep.checks.append(Check("清单格式", fmt_ok, FORMAT if fmt_ok else f"不支持的格式：{manifest.get('format')!r}"))
    if not fmt_ok:
        return rep

    sdk = int(manifest.get("sdk_api") or 0)
    rep.checks.append(
        Check(
            "SDK 兼容",
            0 < sdk <= SDK_API,
            f"包需要 SDK 接口 v{sdk}，平台提供 v{SDK_API}" + ("" if sdk <= SDK_API else "：请先升级平台"),
        )
    )
    market = manifest.get("market") or "spot"
    rep.checks.append(Check("市场类型", market == "spot", "现货" if market == "spot" else f"平台暂不支持 {market}"))

    lang = manifest.get("lang") or lang_of(manifest.get("kind"))
    code_name = "strategy.json" if lang == "dsl" else "strategy.py"
    rep.source = files.get(code_name, "")
    rep.checks.append(Check("策略代码", bool(rep.source.strip()), code_name if rep.source else f"缺少 {code_name}"))
    try:
        rep.params = json.loads(files.get("params.json") or "{}")
        if not isinstance(rep.params, dict):
            raise ValueError("应为对象")
        rep.checks.append(Check("参数", True, f"{len(rep.params)} 个"))
    except ValueError as exc:
        rep.checks.append(Check("参数", False, f"params.json 无效：{exc}"))
    rep.spec = files.get("SPEC.md", "")
    rep.checks.append(Check("说明书", bool(rep.spec.strip()), "有" if rep.spec.strip() else "缺少 SPEC.md", "warning"))
    if files.get("lineage.json"):
        try:
            rep.lineage = json.loads(files["lineage.json"])
        except ValueError:
            rep.lineage = None

    if rep.source:
        found = issues(lang, rep.source)
        rep.checks.append(
            Check("安全检查", not found, "通过（禁网络、禁文件、禁下单接口）" if not found else "；".join(found[:5]))
        )

    req = list(manifest.get("requires") or [])
    if req:
        rep.checks.append(
            Check(
                "研究资产依赖",
                False,
                "用到了平台上的研究资产（" + "、".join(req) + "）；本机没有同名资产时运行会报错",
                "warning",
            )
        )

    timeframe = str(manifest.get("timeframe") or "1h")
    securities = [str(s) for s in (manifest.get("securities") or [])] or ["BTC/USDT"]
    try:
        rep.data = {s: csv_to_bars(files[_sym_file(s)], s, timeframe) for s in securities if _sym_file(s) in files}
    except PackageError as exc:
        rep.checks.append(Check("数据片段", False, str(exc)))
        return rep
    missing = [s for s in securities if s not in rep.data]
    rep.checks.append(
        Check("数据片段", not missing, f"{len(rep.data)} 个标的" if not missing else f"缺少 {'、'.join(missing)}")
    )
    try:
        rep.golden = json.loads(files.get("golden.json") or "{}")
    except ValueError:
        rep.golden = {}
    if not rep.golden.get("hash"):
        rep.checks.append(Check("标准答案", False, "缺少 golden.json，无法核对", "warning"))

    if run and rep.importable and not missing:
        try:
            rep.actual = run_golden(
                lang=lang,
                source=rep.source,
                name=str(manifest.get("slug") or "strategy"),
                params=rep.params,
                data=rep.data,
                timeframe=timeframe,
            )
        except Exception as exc:  # 试跑失败不阻止入库：可能是依赖缺失，界面会说清楚
            rep.checks.append(Check("隔离试跑", False, f"{type(exc).__name__}: {str(exc)[:200]}", "warning"))
        else:
            rep.checks.append(
                Check(
                    "隔离试跑",
                    not rep.actual.get("errors"),
                    f"{rep.actual['fills']} 笔成交，期末权益 {rep.actual['final_equity']}"
                    + (f"；运行中有 {rep.actual['errors']} 条错误" if rep.actual.get("errors") else ""),
                    "warning",
                )
            )
            if rep.golden.get("hash") and not rep.code_match:
                rep.checks.append(
                    Check(
                        "标准答案",
                        False,
                        "代码或参数和作者打包时不一样（被改过）。可以导入和回测，但不能上模拟盘",
                        "warning",
                    )
                )
            elif rep.golden.get("hash"):
                same = rep.golden_match
                rep.checks.append(
                    Check(
                        "标准答案",
                        same,
                        "与作者的结果一致"
                        if same
                        else f"不一致：作者 {rep.golden.get('fills')} 笔 / {rep.golden.get('final_equity')}，"
                        f"本机 {rep.actual['fills']} 笔 / {rep.actual['final_equity']}。可以导入和回测，但不能上模拟盘",
                        "warning",
                    )
                )
    return rep
