"""从 FastAPI 的 OpenAPI 规范生成 docs/API参考.md（与 /docs 同源，不会漂移）。

用法：python scripts/gen_api_doc.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from quant_agent.api.app import create_app

ORDER = [
    "账号与工作区",
    "运行与任务",
    "规则 DSL 与策略版本",
    "研究中心",
    "策略工作室",
    "真实记录与个人账户",
    "行情与数据",
    "AI 策略助手",
    "策略广场",
    "统一赛道与比较",
    "策略与实验",
    "Agent 角色与团队",
    "长期 Agent 与总览",
    "配置",
]


def main() -> None:
    spec = create_app().openapi()
    groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    count = 0
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            tag = (op.get("tags") or ["其他"])[0]
            groups[tag].append((method.upper(), path, op.get("summary", "")))
            count += 1
    tags = sorted(groups, key=lambda t: (ORDER.index(t) if t in ORDER else len(ORDER), t))
    lines = [
        "# QuantLab v0.1.0-preview.1 · API 参考",
        "",
        "[English](API-Reference.en.md) | [文档索引](README.md) | [返回 README](../README.md)",
        "",
        f"平台共 **{count} 个端点**。本文件由 `scripts/gen_api_doc.py` 从 FastAPI 的 OpenAPI 规范生成，",
        "与运行中服务的 `/docs`（可交互调试）来自同一份定义。",
        "",
        "机器可读版本：`docs/openapi.json`（同一次生成）。",
        "",
        "## 通用约定",
        "",
        "- 所有接口前缀为 `/api`；除登录、注册、`/api/health`、`/api/healthz` 外都需要登录（Cookie `qa_session` 或 `Authorization: Bearer`）。",
        "- 写操作（POST/PUT/PATCH/DELETE）必须带 `X-Requested-With`（防 CSRF）；工作区由 `X-Workspace-Id` 指定，服务端会核对成员关系，审阅者只读。",
        "- 写操作可带 `Idempotency-Key`：24 小时内同一工作区同一键重放第一次的响应，不会重复创建任务。",
        "- 错误统一为 `{code, message, trace_id, details}`，`code` 取值见 `quant_agent/platform/errors.py`。",
        '- 金额、数量、价格在 JSON 中是**十进制字符串**（例如 `"1009.79"`），不要用浮点数解析后再参与记账。',
        "- 回测、参数优化、实时模拟、账户同步都是 **run**：API 只入队，worker 执行。提交接口在短时间内完成时直接返回结果，",
        "  否则返回 `202 + run_id`，用 `GET /api/runs/{id}`、`/api/runs/{id}/events?after=` 或 WebSocket `/api/runs/{id}/ws` 跟踪；",
        "  控制命令 `POST /api/runs/{id}/commands`（pause / resume / cancel / liquidate_and_stop）。",
        "",
    ]
    for tag in tags:
        lines += [f"## {tag}", "", "| 方法 | 路径 | 说明 |", "| --- | --- | --- |"]
        for method, path, summary in sorted(groups[tag], key=lambda r: (r[1], r[0])):
            lines.append(f"| `{method}` | `{path}` | {summary} |")
        lines.append("")
    out = Path(__file__).resolve().parents[1] / "docs" / "API参考.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    (out.parent / "openapi.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {out}（{count} 个端点）")


if __name__ == "__main__":
    main()
