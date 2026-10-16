"""运行结果对象存储（T06）：大结果落盘，数据库只留摘要与引用。

为什么要有这一层
----------------
回测结果里的逐 bar 净值、信号、成交是**大数组**：10 万根 bar 的一次回测，
`equity_curve` / `timestamps` / `drawdown` 加起来就有几 MB。这些数组以前整份塞进
`blobs/run_results/<run_id>.json`，而 `runs` 表里还要留一份摘要——结果是
"数据库里存了本该在对象存储里的东西"，行越写越大、列表查询越来越慢。

本模块把大数组搬到 `<store_root>/results/<workspace_id>/<run_id>.<格式>`，
`runs` 表 / 结果 blob 里只留**摘要 + 引用**（路径、格式、行数、校验和）。
引用里必须写清**实际**用的是哪种格式——本机没装 pyarrow 时落的是降级格式，
调用方不能以为拿到的一定是 Parquet。

存储后端是可配置的抽象
----------------------
- 默认实现 `LocalResultBackend`：本地文件系统，零依赖。
- `S3ResultBackend`：S3 兼容对象存储的**接口位**（boto3 按需导入）。本机没有
  boto3、也没有可用的 S3 端点，因此这条路径**未实测**，实现只保证"接口形状一致"。
  配置项：`QAGENT_RESULTS_BACKEND=s3`、`QAGENT_RESULTS_S3_BUCKET`、
  `QAGENT_RESULTS_S3_PREFIX`、`QAGENT_RESULTS_S3_ENDPOINT`、`QAGENT_RESULTS_S3_REGION`。

为什么按 workspace_id 分目录
----------------------------
工作区隔离是本平台的硬要求。落盘文件不在数据库里，没有 SQL 的 `WHERE workspace_id=?`
兜底，所以隔离必须体现在**路径**上：不同工作区的结果文件物理上就在不同目录，
即使上层校验被绕过，也读不到别人的文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import AppError, ErrorCode

# ---- 格式探测：与 data/cache.py 完全相同的既定模式 ----
# 有 pyarrow 就写真 Parquet；没有就退回等价的列式 JSON（功能等价，只是不压缩）。
# 这里**不把 pyarrow 变成必需依赖**：pyproject.toml 里它是可选 extra（parquet / server）。
try:  # pragma: no cover - 取决于部署环境
    import pyarrow as pa
    import pyarrow.parquet as pq

    HAS_PARQUET = True
except ImportError:  # pragma: no cover
    pa = pq = None
    HAS_PARQUET = False

#: 降级格式的后缀。刻意用 `.jsonl` 而不是 `.json`：调用方一眼能看出"这不是 Parquet"，
#: 同时它本身是合法的 JSON Lines（首行是元数据，其后每行一个数据行），可以逐行流式读。
FALLBACK_FORMAT = "jsonl"

#: 结果文件里的大数组字段名。这些字段**不**留在摘要里，只留在落盘文件中。
SERIES_FIELDS = (
    "equity_curve",
    "timestamps",
    "drawdown",
    "benchmark_curve",
    "unit_nav",
    "fills",
    "signals",
    "logs",
    "daily",
    "records",
)

#: 逐 bar 的数值列（落盘时按列存，读回时按 `name` 逐列分页）。
BAR_SERIES = ("equity_curve", "timestamps", "drawdown", "benchmark_curve", "unit_nav")

#: 行式数组（每个元素是一个对象），分页时按行切片。
ROW_SERIES = ("fills", "signals", "logs", "daily")

#: 逐 bar 数组的默认分页大小上限。
MAX_PAGE = 20_000


def result_format() -> str:
    """当前部署实际会用的落盘格式。返回 "parquet" 或降级格式名。"""
    return "parquet" if HAS_PARQUET else FALLBACK_FORMAT


def _safe(part: str) -> str:
    """把 id 收敛成安全的路径片段；空或全非法字符直接报错，绝不拼出 `..`。"""
    safe = "".join(ch for ch in str(part) if ch.isalnum() or ch in "-_")
    if not safe or safe != str(part):
        raise ValueError(f"非法的路径标识：{part!r}")
    return safe


def _checksum(rows: list[list[Any]]) -> str:
    """行数据的 sha256（对规范化后的 JSON 取摘要），用于校验落盘内容没被改动。"""
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class SeriesRef:
    """一个大数组的引用：行数 + 校验和。摘要里只放这个，不放数据本身。"""

    name: str
    rows: int
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rows": self.rows, "checksum": self.checksum}


@dataclass
class ResultRef:
    """整份结果的引用：路径、**实际**格式、行数与校验和。"""

    workspace_id: str
    run_id: str
    path: str
    format: str
    rows: int
    checksum: str
    bytes: int
    series: dict[str, SeriesRef]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "path": self.path,
            "format": self.format,
            "rows": self.rows,
            "checksum": self.checksum,
            "bytes": self.bytes,
            "series": {k: v.to_dict() for k, v in sorted(self.series.items())},
        }


class ResultBackend(Protocol):
    """结果存储后端。本地文件系统是默认实现；S3 兼容存储实现同一套接口。"""

    name: str

    def write(self, key: str, payload: dict[str, Any], rows: list[list[Any]]) -> tuple[int, str]:
        """写入一份结果，返回 (字节数, 行数据校验和)。"""
        ...

    def read(self, key: str) -> dict[str, Any] | None:
        """读回整份结果（元数据 + 全部行）。"""
        ...

    def read_page(self, key: str, name: str, offset: int, limit: int) -> tuple[list[Any], int]:
        """分页读取一个大数组，返回 (本页数据, 该数组总行数)。"""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...


class LocalResultBackend:
    """本地文件系统后端（默认）。

    有 pyarrow 时写真正的 Parquet（列式 + zstd 压缩）；没有时写等价的 JSON Lines：
    首行元数据（列名 + 行数），之后每行一个数据行。两种格式的**读回结果逐元素相等**，
    差别只在压缩率与是否列式。
    """

    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def path_for(self, key: str, fmt: str | None = None) -> Path:
        fmt = fmt or result_format()
        path = self.root / f"{key}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ---- 写 ----
    def write(self, key: str, payload: dict[str, Any], rows: list[list[Any]]) -> tuple[int, str]:
        columns: list[str] = list(payload.get("columns") or [])
        fmt = result_format()
        path = self.path_for(key, fmt)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with self._lock:
            if fmt == "parquet":
                cols = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
                # 行式数组（成交、日志…）的元素是键不固定的对象：parquet 会推成结构体并给缺的键补 None，
                # 读回就不再逐元素相等。这些列按 JSON 文本存，读回原样还原。
                as_json = [c for c in columns if c in ROW_SERIES]
                for c in as_json:
                    cols[c] = [None if v is None else json.dumps(v, ensure_ascii=False, default=str) for v in cols[c]]
                table = pa.table(cols).replace_schema_metadata(
                    {
                        b"quantlab_lengths": json.dumps(payload.get("lengths") or {}).encode("utf-8"),
                        b"quantlab_json_cols": json.dumps(as_json).encode("utf-8"),
                    }
                )
                pq.write_table(table, tmp, compression="zstd")
            else:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({**payload, "format": fmt}, ensure_ascii=False, separators=(",", ":")))
                    fh.write("\n")
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str))
                        fh.write("\n")
            tmp.replace(path)
            size = path.stat().st_size
        return size, _checksum(rows)

    # ---- 读 ----
    def _rows(self, key: str) -> list[list[Any]]:
        pqp = self.path_for(key, "parquet")
        if pq is not None and pqp.exists():
            table = pq.read_table(pqp)
            d = table.to_pydict()
            names = list(d)
            raw = (table.schema.metadata or {}).get(b"quantlab_json_cols")
            try:
                json_cols = set(json.loads(raw)) if raw else set()
            except (ValueError, TypeError):
                json_cols = set()
            for c in json_cols & set(names):
                d[c] = [None if v is None else json.loads(v) for v in d[c]]
            return [[d[c][i] for c in names] for i in range(table.num_rows)]
        js = self.path_for(key, FALLBACK_FORMAT)
        if not js.exists():
            return []
        out: list[list[Any]] = []
        with open(js, encoding="utf-8") as fh:
            fh.readline()  # 首行是元数据，不是数据行
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def read(self, key: str) -> dict[str, Any] | None:
        """读回元数据 + 数据行。数据行放在 `data` 下，`rows` 保持"行数"语义不被覆盖。"""
        meta = self.meta(key)
        if meta is None:
            return None
        return {**meta, "data": self._rows(key)}

    def meta(self, key: str) -> dict[str, Any] | None:
        """只读元数据（列名、格式、行数、各列原始长度），不解析数据行——分页接口靠它算总数。"""
        pqp = self.path_for(key, "parquet")
        if pq is not None and pqp.exists():
            pf = pq.ParquetFile(pqp)
            raw = (pf.schema_arrow.metadata or {}).get(b"quantlab_lengths")
            try:
                lengths = dict(json.loads(raw)) if raw else {}
            except (ValueError, TypeError):
                lengths = {}
            return {
                "format": "parquet",
                "columns": list(pf.schema_arrow.names),
                "rows": pf.metadata.num_rows,
                "lengths": lengths,
            }
        js = self.path_for(key, FALLBACK_FORMAT)
        if not js.exists():
            return None
        with open(js, encoding="utf-8") as fh:
            head = fh.readline()
        try:
            meta = json.loads(head)
        except json.JSONDecodeError:
            return None
        return {
            "format": FALLBACK_FORMAT,
            "columns": list(meta.get("columns") or []),
            "rows": int(meta.get("rows") or 0),
            "lengths": dict(meta.get("lengths") or {}),
        }

    def read_page(self, key: str, name: str, offset: int, limit: int) -> tuple[list[Any], int]:
        meta = self.meta(key)
        if meta is None:
            return [], 0
        columns: list[str] = meta["columns"]
        lengths: dict[str, int] = dict(meta.get("lengths") or {})
        if name not in columns:
            return [], 0
        # 该列的**真实**行数（不是补齐后的表格宽度），分页总数与切片都以它为准
        total = int(lengths.get(name, meta["rows"]))
        if offset >= total:
            return [], total
        idx = columns.index(name)
        end = min(total, offset + limit)
        rows = self._rows(key)[offset:end]
        return [r[idx] for r in rows], total

    def exists(self, key: str) -> bool:
        return self.meta(key) is not None

    def delete(self, key: str) -> bool:
        hit = False
        for fmt in ("parquet", FALLBACK_FORMAT):
            path = self.path_for(key, fmt)
            if path.exists():
                path.unlink()
                hit = True
        return hit


class S3ResultBackend:
    """S3 兼容对象存储后端（接口位，**本机未实测**）。

    设计意图：把"结果放哪儿"从业务代码里抽出来。要真接 S3，只需
    `pip install boto3` 并设置 `QAGENT_RESULTS_BACKEND=s3` 与 bucket 等环境变量，
    上层（`ResultsStore` / API / worker）一行都不用改。

    诚实声明：本机既没有 boto3，也没有可用的 S3 端点，因此这个类**从未被真实调用过**，
    只有接口形状与 `LocalResultBackend` 对齐。不要把它当成"已验证可用"。
    """

    name = "s3"

    def __init__(
        self, bucket: str, *, prefix: str = "results", endpoint: str | None = None, region: str | None = None
    ) -> None:
        if not bucket:
            raise ValueError("S3 后端必须配置 bucket（QAGENT_RESULTS_S3_BUCKET）")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint
        self.region = region

    def _client(self):  # pragma: no cover - 未实测路径
        import boto3  # 按需导入：没装 boto3 时不影响本地后端

        kwargs: dict[str, Any] = {}
        if self.endpoint:
            kwargs["endpoint_url"] = self.endpoint
        if self.region:
            kwargs["region_name"] = self.region
        return boto3.client("s3", **kwargs)

    def _key(self, key: str) -> str:  # pragma: no cover - 未实测路径
        return f"{self.prefix}/{key}.{result_format()}"

    def write(self, key: str, payload: dict[str, Any], rows: list[list[Any]]) -> tuple[int, str]:  # pragma: no cover
        raise NotImplementedError("S3 后端未实测：请在部署环境安装 boto3 并补全实现")

    def read(self, key: str) -> dict[str, Any] | None:  # pragma: no cover
        raise NotImplementedError("S3 后端未实测：请在部署环境安装 boto3 并补全实现")

    def read_page(self, key: str, name: str, offset: int, limit: int) -> tuple[list[Any], int]:  # pragma: no cover
        raise NotImplementedError("S3 后端未实测：请在部署环境安装 boto3 并补全实现")

    def exists(self, key: str) -> bool:  # pragma: no cover
        return False

    def delete(self, key: str) -> bool:  # pragma: no cover
        raise NotImplementedError("S3 后端未实测：请在部署环境安装 boto3 并补全实现")


def build_backend(root: str | Path) -> ResultBackend:
    """按环境变量选后端。默认本地文件系统；`QAGENT_RESULTS_BACKEND=s3` 才切 S3。"""
    kind = os.environ.get("QAGENT_RESULTS_BACKEND", "local").strip().lower()
    if kind == "s3":
        return S3ResultBackend(
            os.environ.get("QAGENT_RESULTS_S3_BUCKET", ""),
            prefix=os.environ.get("QAGENT_RESULTS_S3_PREFIX", "results"),
            endpoint=os.environ.get("QAGENT_RESULTS_S3_ENDPOINT") or None,
            region=os.environ.get("QAGENT_RESULTS_S3_REGION") or None,
        )
    return LocalResultBackend(root)


def split_series(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """把结果拆成 (摘要, 大数组)。

    摘要保留标量、指标、清单等"小"内容，**原样不动**地进 blob；大数组按**列**抽出
    （列名即原字段名），这样 Parquet 是真正的列式存储，分页读一列不必解析整行。

    摘要刻意不做任何增删：这样 `recombine(*split_series(body)) == body` 逐元素成立，
    "读回 == 写入前"才有可测的定义。行数这类元信息由 `ResultRef` 承载，不混进摘要。
    """
    summary: dict[str, Any] = {}
    series: dict[str, list[Any]] = {}
    for key, value in body.items():
        if key in SERIES_FIELDS and isinstance(value, (list, tuple)):
            series[key] = list(value)
        else:
            summary[key] = value
    return summary, series


def recombine(summary: dict[str, Any], series: dict[str, list[Any]]) -> dict[str, Any]:
    """`split_series` 的逆运算：摘要 + 大数组 → 完整结果。"""
    return {**summary, **series}


def to_columns(series: dict[str, list[Any]]) -> tuple[list[str], list[list[Any]]]:
    """把按字段的大数组转成 (列名, 行) 的表格形状。

    列式存储要求每一列等长；长度不一致时（例如只有部分标的的 records）以最长列为准，
    短列补 None。这样读回时仍是**逐元素相等**的原始数据。
    """
    columns = sorted(series)
    width = max((len(v) for v in series.values()), default=0)
    rows: list[list[Any]] = []
    for i in range(width):
        rows.append([series[c][i] if i < len(series[c]) else None for c in columns])
    return columns, rows


def from_columns(payload: dict[str, Any], rows: list[list[Any]]) -> dict[str, list[Any]]:
    """`to_columns` 的逆运算：按列名把行还原成各字段的数组。

    列式存储要求等长，短列在 `to_columns` 里被补了 None；这里按元数据里记下的
    **原始长度**裁回去，保证读回的数据与落盘前逐元素相等（尾部不会多出 None）。
    """
    columns: list[str] = list(payload.get("columns") or [])
    lengths: dict[str, int] = dict(payload.get("lengths") or {})
    out: dict[str, list[Any]] = {c: [] for c in columns}
    for row in rows:
        for i, c in enumerate(columns):
            out[c].append(row[i] if i < len(row) else None)
    for c, n in lengths.items():
        if c in out:
            out[c] = out[c][:n]
    # 旧 parquet 文件没记原始长度：行式数组（日志、成交…）的元素都是对象，尾部的 None 只可能是补位
    for c in ROW_SERIES:
        if c in out and c not in lengths:
            while out[c] and out[c][-1] is None:
                out[c].pop()
    return out


class ResultsStore:
    """结果对象存储的门面：落盘 + 引用 + 分页读取。

    `results_root` 是结果目录本身（不再往下拼 "results"）。文件路径固定为
    `<results_root>/<workspace_id>/<run_id>.<格式>`；默认部署下
    `results_root = <QAGENT_HOME>/results`，即 T06 要求的
    `QAGENT_HOME/results/<workspace>/<run>.parquet`。
    """

    def __init__(self, results_root: str | Path, *, backend: ResultBackend | None = None) -> None:
        self.root = Path(results_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend = backend or build_backend(self.root)

    # ---- 路径 ----
    def key(self, workspace_id: str, run_id: str) -> str:
        """对象键 = `<workspace_id>/<run_id>`；按工作区分目录是隔离要求，不是可选项。"""
        return f"{_safe(workspace_id)}/{_safe(run_id)}"

    def path(self, workspace_id: str, run_id: str) -> Path:
        if isinstance(self.backend, LocalResultBackend):
            return self.backend.path_for(self.key(workspace_id, run_id))
        return self.root / self.key(workspace_id, run_id)

    # ---- 写 ----
    def put(self, workspace_id: str, run_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], ResultRef]:
        """落盘一份结果，返回 (摘要, 引用)。

        摘要里**不含**大数组；引用里写清实际格式、行数、校验和与逐数组行数。
        """
        summary, series = split_series(body)
        columns, rows = to_columns(series)
        payload = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "format": result_format(),
            "columns": columns,
            "rows": len(rows),
            # 各列原始长度：列式存储会补 None 对齐，读回时按这个裁掉补位
            "lengths": {c: len(v) for c, v in series.items()},
        }
        size, checksum = self.backend.write(self.key(workspace_id, run_id), payload, rows)
        ref = ResultRef(
            workspace_id=workspace_id,
            run_id=run_id,
            path=str(self.path(workspace_id, run_id)),
            format=result_format(),
            rows=len(rows),
            checksum=checksum,
            bytes=size,
            series={c: SeriesRef(c, len(v), _checksum([[x] for x in v])) for c, v in series.items()},
        )
        return summary, ref

    def read_series(self, workspace_id: str, run_id: str) -> dict[str, list[Any]]:
        """读回落盘的大数组；没有落盘文件时返回空字典（老运行只有 blob，没有对象存储文件）。"""
        return self.read(workspace_id, run_id) or {}

    def load_full(self, workspace_id: str, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        """摘要（来自 blob）+ 落盘大数组 → 与写入前逐元素相等的完整结果。

        若落盘文件不存在（升级前的旧运行、或文件被运维清理），返回的就是摘要本身——
        调用方按"没有大数组"处理，不会拿到半截数据还以为完整。
        """
        series = self.read_series(workspace_id, run_id)
        return recombine(summary, series) if series else dict(summary)

    # ---- 读 ----
    def read(self, workspace_id: str, run_id: str) -> dict[str, list[Any]] | None:
        """读回落盘的大数组（按字段名）。摘要不在文件里——它在结果 blob 中，由调用方合并。"""
        payload = self.backend.read(self.key(workspace_id, run_id))
        if payload is None:
            return None
        return from_columns(payload, payload.get("data") or [])

    def meta(self, workspace_id: str, run_id: str) -> dict[str, Any] | None:
        meta = getattr(self.backend, "meta", None)
        return meta(self.key(workspace_id, run_id)) if meta else None

    def page(self, workspace_id: str, run_id: str, name: str, *, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
        """分页读一个大数组。`name` 必须是实际存在的列，否则报 INVALID_PARAMETER。"""
        if offset < 0:
            raise AppError(ErrorCode.INVALID_PARAMETER, "offset 不能为负数", details={"offset": offset})
        if limit < 1 or limit > MAX_PAGE:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                f"limit 必须在 1 到 {MAX_PAGE} 之间",
                details={"limit": limit, "max": MAX_PAGE},
            )
        meta = self.meta(workspace_id, run_id)
        if meta is None:
            raise AppError(ErrorCode.NOT_FOUND, "该运行没有可读取的结果数据")
        available = list(meta.get("columns") or [])
        if name not in available:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                f"结果中没有名为 {name} 的数据列",
                details={"name": name, "available": available},
            )
        items, total = self.backend.read_page(self.key(workspace_id, run_id), name, offset, limit)
        return {
            "name": name,
            "offset": offset,
            "limit": limit,
            "total": total,
            "format": meta.get("format") or result_format(),
            "items": items,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
        }

    def exists(self, workspace_id: str, run_id: str) -> bool:
        return self.backend.exists(self.key(workspace_id, run_id))

    def delete(self, workspace_id: str, run_id: str) -> bool:
        return self.backend.delete(self.key(workspace_id, run_id))
