"""可复现研究包：把一次回测的代码、数据、资金费、参数、结果与审查打成一个 zip。

与 .qstrat 策略包不同，研究包绑定的是**一次具体回测**，用于复核与重放，不承担策略分发职责。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

FORMAT = "quantlab-research/1"
MAX_BYTES = 80 * 1024 * 1024
ALLOWED = (
    "manifest.json",
    "code.py",
    "spec.json",
    "data.json",
    "funding.json",
    "result.json",
    "review.json",
    "trace.json",
)


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def build(
    *, backtest: dict[str, Any], snapshot: dict[str, Any], trace: dict[str, Any] | None = None
) -> tuple[bytes, dict[str, Any]]:
    manifest = dict(backtest.get("manifest") or {})
    code = str(backtest.get("code") or "")
    params = backtest.get("params") or (manifest.get("params") or {})
    data_sha = _sha(snapshot.get("bars") or {})
    funding_sha = _sha(snapshot.get("funding") or {})
    result_sha = _sha(backtest.get("metrics") or {})
    package_manifest = {
        "format": FORMAT,
        "backtest_id": backtest.get("id"),
        "run_id": backtest.get("run_id"),
        "strategy_name": backtest.get("strategy_name"),
        "strategy_version_id": (backtest.get("row") or {}).get("strategy_version_id"),
        "created_at": int(__import__("time").time() * 1000),
        "platform_version": manifest.get("platform_version"),
        "lang": manifest.get("lang") or (backtest.get("row") or {}).get("lang") or "python",
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "params_sha256": _sha(params),
        "data_sha256": data_sha,
        "funding_sha256": funding_sha,
        "result_sha256": result_sha,
        "review_id": (backtest.get("review") or {}).get("id"),
        "review_verdict": (backtest.get("review") or {}).get("verdict"),
        "has_funding": bool(snapshot.get("funding")),
        "has_trace": bool(trace),
        "manifest": manifest,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(package_manifest, ensure_ascii=False, indent=2))
        z.writestr("code.py", code)
        z.writestr(
            "spec.json", json.dumps(snapshot.get("spec") or backtest.get("spec") or {}, ensure_ascii=False, indent=2)
        )
        z.writestr(
            "data.json", json.dumps(snapshot.get("bars") or {}, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        z.writestr(
            "funding.json",
            json.dumps(snapshot.get("funding") or {}, ensure_ascii=False, separators=(",", ":"), default=str),
        )
        z.writestr(
            "result.json",
            json.dumps(
                {"metrics": backtest.get("metrics") or {}, "row": backtest.get("row") or {}},
                ensure_ascii=False,
                default=str,
            ),
        )
        z.writestr("review.json", json.dumps(backtest.get("review") or {}, ensure_ascii=False, indent=2))
        z.writestr("trace.json", json.dumps(trace or {}, ensure_ascii=False, indent=2))
    return buf.getvalue(), package_manifest


def read(data: bytes) -> dict[str, str]:
    if len(data) > MAX_BYTES:
        raise ValueError("研究包太大")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的研究包（zip 格式）") from exc
    out: dict[str, str] = {}
    total = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        if info.filename not in ALLOWED:
            raise ValueError(f"研究包里有未知文件：{info.filename}")
        total += info.file_size
        if total > MAX_BYTES:
            raise ValueError("研究包解压后过大")
        out[info.filename] = z.read(info).decode("utf-8")
    return out


def inspect(data: bytes) -> dict[str, Any]:
    files = read(data)
    try:
        manifest = json.loads(files["manifest.json"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("manifest.json 缺失或损坏") from exc
    checks: list[dict[str, Any]] = [
        {"name": "格式", "ok": manifest.get("format") == FORMAT, "detail": str(manifest.get("format"))}
    ]
    code = files.get("code.py", "")
    checks.append(
        {
            "name": "代码哈希",
            "ok": hashlib.sha256(code.encode()).hexdigest() == manifest.get("code_sha256"),
            "detail": "与 manifest 一致",
        }
    )
    import json as _json

    try:
        data_obj = _json.loads(files.get("data.json") or "{}")
        funding_obj = _json.loads(files.get("funding.json") or "{}")
        params_obj = _json.loads(files.get("spec.json") or "{}").get("params") or {}
    except _json.JSONDecodeError as exc:
        raise ValueError("研究包中的数据文件损坏") from exc
    checks.append(
        {"name": "数据哈希", "ok": _sha(data_obj) == manifest.get("data_sha256"), "detail": "与 manifest 一致"}
    )
    checks.append(
        {"name": "资金费哈希", "ok": _sha(funding_obj) == manifest.get("funding_sha256"), "detail": "与 manifest 一致"}
    )
    checks.append(
        {"name": "参数哈希", "ok": _sha(params_obj) == manifest.get("params_sha256"), "detail": "与 manifest 一致"}
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "manifest": manifest, "files": files}


def replay_context(data: bytes) -> dict[str, Any]:
    """校验后返回重放所需上下文；调用方负责真正执行回测。"""
    got = inspect(data)
    if not got["ok"]:
        raise ValueError("研究包校验失败：" + "；".join(c["detail"] for c in got["checks"] if not c["ok"]))
    files = got["files"]
    return {
        "manifest": got["manifest"],
        "source": files["code.py"],
        "spec": json.loads(files["spec.json"]),
        "data": json.loads(files["data.json"]),
        "funding": json.loads(files["funding.json"]),
        "result": json.loads(files["result.json"]),
        "review": json.loads(files["review.json"]),
    }


__all__ = ["FORMAT", "build", "inspect", "read", "replay_context"]
