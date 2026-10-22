from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import numpy as np

ANON = re.compile(
    r"^(?:客户|供应商|客商|法人|单位|公司|企业|customer|supplier|company|client)[\s\-_:：#第]*[A-ZＡ-Ｚ0-9０-９一二三四五六七八九十甲乙丙丁戊己庚辛壬癸]+(?:名|号|公司|客户|供应商)?$|匿名|未披露|不予披露|不便披露|保密|自然人|个人客户|其他客户|其他供应商|^其他$|^某|^第[一二三四五]名$|^[A-E]公司$",
    re.I,
)
LEGAL = re.compile(r"公司|企业|集团|银行|合作社|厂$|中心$|研究院|大学|学院|医院|委员会|管理局|财政局|政府|事业单位")
INSTITUTION = re.compile(r"大学|学院|研究所|研究院|医院|委员会|管理局|财政局|政府|事业单位")
NAMES = ["大型上市锚点", "上市中小企业代理", "未上市中小企业代理", "事业单位", "规模待核验企业"]


def normal_name(name):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name))).replace("，", ",").strip()


def valid_name(name):
    return (
        len(normal_name(name)) >= 4
        and not ANON.search(normal_name(name))
        and not any(t in name for t in ["合计", "总计", "汇总"])
    )


def num(value):
    try:
        x = float(str(value).replace(",", "").replace("%", ""))
        return x if np.isfinite(x) else np.nan
    except (ValueError, TypeError):
        return np.nan


def stock_code(value):
    value = str(value).strip()
    return value if re.fullmatch(r"\d{6}", value) else ""


def listing_code(value):
    """One unambiguous mainland code; preserve A/H multi-code evidence."""
    candidates = {
        s
        for s in re.findall(r"(?<!\d)\d{6}(?!\d)", str(value))
        if s.startswith(
            ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689", "43", "83", "87", "92")
        )
    }
    return next(iter(candidates)) if len(candidates) == 1 else ""


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checked_path(ctx, reference):
    path = ctx.file(reference["file"])
    if hash_file(path) != reference["sha256"]:
        raise ValueError("二进制工件哈希不符：" + reference["file"])
    return path


def save_json(ctx, value, name="data.json"):
    path = Path(ctx.work) / name
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            default=lambda v: v.item() if isinstance(v, np.generic) else str(v),
        ),
        encoding="utf-8",
    )
    return ctx.save_file(path)


def read_json(ctx, reference):
    return json.loads(checked_path(ctx, reference).read_text(encoding="utf-8"))


def save_frame(ctx, frame, name):
    path = Path(ctx.work) / (name + ".csv.gz")
    frame.to_csv(path, index=False, encoding="utf-8", compression={"method": "gzip", "mtime": 0})
    return ctx.save_file(path)


def read_frame(ctx, reference, **kwargs):
    import pandas as pd

    return pd.read_csv(checked_path(ctx, reference), compression="gzip", **kwargs)


def save_npz(ctx, name, **values):
    path = Path(ctx.work) / (name + ".npz")
    np.savez_compressed(path, **values)
    return ctx.save_file(path)


def load_npz(ctx, reference):
    with np.load(checked_path(ctx, reference), allow_pickle=False) as value:
        return {k: value[k] for k in value.files}


def csv_rows(path, audit=None):
    csv.field_size_limit(10_000_000)
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                if audit is not None:
                    audit["malformed_records"] = audit.get("malformed_records", 0) + 1
                continue
            yield row
