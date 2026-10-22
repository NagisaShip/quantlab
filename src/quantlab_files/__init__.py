"""Bounded previews and explicit partitions for files of arbitrary byte size."""

from __future__ import annotations

import csv
import datetime
import itertools
import json
import math
from pathlib import Path


def clean(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def rows(path, encoding="utf-8-sig", columns=None):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        with path.open(encoding=encoding, newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t" if suffix == ".tsv" else ",")
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("字段为空或重名")
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(f"第{reader.line_num}行列数不匹配")
                yield {k: v for k, v in row.items() if not columns or k in columns}
    elif suffix in (".xlsx", ".xlsm"):
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            values = book.active.iter_rows(values_only=True)
            names = [str(v) if v is not None else "" for v in next(values, [])]
            if not names or "" in names or len(set(names)) != len(names):
                raise ValueError("Excel首行字段为空或重名")
            for row in values:
                yield {k: clean(v) for k, v in zip(names, row, strict=True) if not columns or k in columns}
        finally:
            book.close()
    elif suffix == ".parquet":
        import pyarrow.parquet as pq

        for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=columns or None):
            for row in batch.to_pylist():
                yield {k: clean(v) for k, v in row.items()}
    elif suffix in (".jsonl", ".ndjson"):
        with path.open(encoding=encoding) as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("JSONL每行必须是对象")
                    yield {k: v for k, v in row.items() if not columns or k in columns}
    else:
        raise ValueError("支持CSV、TSV、XLSX/XLSM、Parquet和JSONL；旧版XLS请另存XLSX")


def read(path, *, limit=20, offset=0, encoding="utf-8-sig", columns=None, on_limit="partition"):
    if not 1 <= limit <= 1000000 or offset < 0:
        raise ValueError("limit为1—1000000，offset不得为负")
    iterator = rows(path, encoding, columns)
    try:
        sample = list(itertools.islice(iterator, offset, offset + limit + 1))
    finally:
        iterator.close()
    has_more = len(sample) > limit
    if has_more and on_limit == "error":
        raise ValueError("数据超过配置行数，未静默截断；请显式选择分区读取或提高max_rows")
    total = None if has_more else offset + len(sample)
    if Path(path).suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        total = pq.ParquetFile(path).metadata.num_rows
    if not sample and offset and (total is None or total < offset):
        raise ValueError("读取起点超出文件数据范围")
    return {
        "data": sample[:limit],
        "fields": list(sample[0]) if sample else [],
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "scope": "explicit partition" if has_more or offset else "complete file",
        "note": "只扫描所需范围，total=null表示未扫描文件尾，不等于文件总行数",
    }


def source(ctx, inputs, params):
    result = read(
        ctx.file(params["file"]),
        limit=params.get("max_rows", 100000),
        offset=params.get("start_row", 0),
        encoding=params.get("encoding", "utf-8-sig"),
        columns=params.get("columns"),
        on_limit=params.get("on_limit", "error"),
    )
    return {"data": result.pop("data"), "scope": result}
