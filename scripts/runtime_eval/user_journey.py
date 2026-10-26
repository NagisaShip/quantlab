"""Real-provider user journey. Credentials are read only from the process environment."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
ap = argparse.ArgumentParser()
ap.add_argument("--output", required=True)
ap.add_argument(
    "--stage",
    choices=["baseline", "lab", "review", "snapshot", "resume", "postfix", "final", "verify"],
    default="baseline",
)
args = ap.parse_args()
out = Path(args.output).resolve()
out.mkdir(parents=True, exist_ok=True)
os.environ.update(
    QAGENT_BACKGROUND="0",
    QAGENT_WORKER="inline",
    QAGENT_STREAM="0",
    QAGENT_ALLOW_SYNTHETIC="0",
    QAGENT_FREE_SEARCH="0",
    QAGENT_SEED_BUILTINS="0",
    QAGENT_SECRET_KEY=base64.urlsafe_b64encode(b"q" * 32).decode(),
)
# Do not import conftest: its synthetic-market defaults are unsuitable here.
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.router import AgentConfig, ProviderConfig

key = os.environ["OPENCODE_API_KEY"]
model = "deepseek-v4.1-flash"
services = AppServices.build(store_root=str(out / "store"))
(out / ".gitignore").write_text("store/\n", encoding="utf-8")


def resolve(_):
    return AgentConfig(
        providers={
            "journey": ProviderConfig(
                "journey", "openai_compatible", "https://opencode.ai/zen/go/v1", key, models=[model]
            )
        },
        routing={x: f"journey/{model}" for x in ["default", "code_gen", "reasoning", "report"]},
        limits={"max_tokens_per_run": 200000},
        trace={"store_prompts": False, "redact_keys": True},
    )


services.router.resolver = resolve
set_services(services)


def safe(value):
    return json.dumps(value, ensure_ascii=False, default=str).replace(key, "[REDACTED]")


def call(client, name, method, path, payload=None):
    started = time.monotonic()
    try:
        r = client.request(method, path, json=payload) if payload is not None else client.request(method, path)
        try:
            body = r.json()
        except Exception:
            body = {"text": r.text}
        row = {
            "name": name,
            "method": method,
            "path": path,
            "request": payload,
            "http": r.status_code,
            "seconds": round(time.monotonic() - started, 3),
            "body": body,
        }
    except Exception as exc:
        row = {
            "name": name,
            "method": method,
            "path": path,
            "request": payload,
            "seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
        body = {}
    (out / f"{name}.json").write_text(safe(row), encoding="utf-8")
    with (out / "index.jsonl").open("a", encoding="utf-8") as f:
        f.write(safe({k: v for k, v in row.items() if k != "body"}) + "\n")
    print(safe({k: v for k, v in row.items() if k not in ("body", "request")}), flush=True)
    return body


SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT"]
# Freeze the end before the run and exclude the currently forming 4h candle.
end = 1792972800000
base = {"symbols": SYMS, "timeframe": "4h", "bars_limit": 1200, "end": end, "source": "exchange", "cost_bps": 10}
with TestClient(create_app(), raise_server_exceptions=False) as c:
    c.headers.update({"X-Requested-With": "user-journey"})
    auth = {"email": "journey@example.test", "password": "Journey-Test-2026!", "name": "真实流程评估"}
    r = c.post("/api/auth/register", json=auth)
    if r.status_code != 200:
        r = c.post("/api/auth/login", json=auth)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed {r.status_code}")
    me = c.get("/api/auth/me").json()
    c.headers.update({"X-Workspace-Id": me["workspace"]["id"]})
    if args.stage == "verify":
        f = json.loads((out / "factor_0_save.json").read_text(encoding="utf-8"))["body"]["factor"]["id"]
        m = json.loads((out / "model_ridge_save.json").read_text(encoding="utf-8"))["body"]["model"]["id"]
        pf = json.loads((out / "portfolio_equal_save.json").read_text(encoding="utf-8"))["body"]["scheme"]["id"]
        call(c, "verified_factor_active_rejected", "POST", f"/api/factors/{f}/status", {"status": "active"})
        call(c, "verified_model_active_rejected", "PATCH", f"/api/models/{m}", {"status": "active"})
        call(c, "verified_portfolio_active_rejected", "PATCH", f"/api/portfolio/{pf}", {"status": "active"})
        call(c, "verified_model_contract", "GET", f"/api/models/{m}/report")
        call(
            c,
            "verified_risk_off_diversification",
            "POST",
            "/api/portfolio/evaluate",
            {
                **base,
                "config": {
                    "optimizer": "risk_parity",
                    "constraints": {"max_weight": 0.3},
                    "risk": {"vol_target": 0.35, "dd_stop": 0.15},
                },
            },
        )
        call(c, "verified_final_usage", "GET", "/api/workspace/usage?days=1")
    elif args.stage == "final":
        prompts = {
            "model": "只归档，不增加训练或其他实验，也不继续旧计划。使用restore_version(version=1)恢复原ridge，"
            "然后save_work；再restore_version(version=2)恢复原gbdt，然后save_work。"
            "从restore_version的baseline字段说明两者基线，列出两个不同的候选ID。禁止write_model_spec或train_model。",
            "factor": "只读核对，不继续旧计划。用restore_version(version=6)恢复原平滑低波动版本，不调用evaluate_factor。"
            "按metric_contract说明ls_return与ls_total_return的周期和费用口径。"
            "说明ICIR的绝对差距与相对差距，不能用零假设方差宣称目标不可达。不要启用资产。",
        }
        for kind, prompt in prompts.items():
            pid = json.loads((out / f"lab_{kind}_create.json").read_text(encoding="utf-8"))["body"]["project"]["id"]
            call(
                c,
                f"final_{kind}_chat",
                "POST",
                f"/api/lab/projects/{pid}/messages",
                {"text": prompt, "wait": True, "max_tool_calls": 12},
            )
            call(c, f"final_{kind}_snapshot", "GET", f"/api/lab/projects/{pid}")
        call(c, "final_usage", "GET", "/api/workspace/usage?days=1")
    elif args.stage == "postfix":
        for domain in ["factors", "models", "portfolio"]:
            rows = call(c, f"postfix_before_{domain}", "GET", f"/api/{domain}")
            for asset in rows.get({"factors": "factors", "models": "models", "portfolio": "schemes"}[domain], []):
                if asset.get("status") == "active":
                    path = f"/api/{domain}/{asset['id']}"
                    call(
                        c,
                        f"postfix_candidate_{asset['id']}",
                        "POST" if domain == "factors" else "PATCH",
                        path + "/status" if domain == "factors" else path,
                        {"status": "candidate"},
                    )
        prompts = {
            "factor": "不增加实验。将已有版本1、2、3分别恢复并调用save_work保存为三个不同的候选因子。然后恢复版本6。"
            "请按工具metric_contract说明版本6的ls_return究竟是年化还是累计、是否已扣费，不要再次减费用。"
            "说明零假设方差不能当作噪声下界。不要扩大币池或启用任何资产。",
            "model": "不增加实验。将已有版本1(ridge)和2(gbdt)分别恢复并调用save_work归档为不同的候选模型。"
            "读取真实报告中的baseline字段，说明两者基线分别是什么。不要追逐默认目标、换标签或宣称模型可交易。",
            "portfolio": "不增加实验。将已有等权版本7和风险平价版本8分别恢复并调用save_work，保存为两个不同的候选方案。"
            "确认两个独立资产ID、单币30%约束。不要把研究检查通过当作实盘批准。",
            "strategy": "保留现有项目名称。重试save_work，把当前已评估代码保存为策略草稿；不要重新生成代码。"
            "汇总已有默认和3倍成本结果，准确说明未完成独立样本外验证。不要新增实验。",
        }
        for kind, prompt in prompts.items():
            pid = json.loads((out / f"lab_{kind}_create.json").read_text(encoding="utf-8"))["body"]["project"]["id"]
            call(
                c,
                f"postfix_{kind}_chat",
                "POST",
                f"/api/lab/projects/{pid}/messages",
                {"text": prompt, "wait": True, "max_tool_calls": 30},
            )
            call(c, f"postfix_{kind}_snapshot", "GET", f"/api/lab/projects/{pid}")
        call(c, "postfix_strategy_library", "GET", "/api/studio/strategies")
        call(c, "postfix_usage", "GET", "/api/workspace/usage?days=1")
    elif args.stage == "resume":
        call(
            c,
            "unlimited_usage_limits",
            "PUT",
            "/api/workspace/usage/limits",
            {
                k: None
                for k in [
                    "max_cpu_seconds_per_day",
                    "max_backtests_per_day",
                    "max_optimize_combinations_per_day",
                    "max_llm_calls_per_day",
                    "max_llm_tokens_per_day",
                    "max_download_bytes_per_day",
                    "max_storage_bytes",
                ]
            },
        )
        for kind in ["portfolio", "strategy"]:
            original = json.loads((out / f"lab_{kind}_create.json").read_text(encoding="utf-8"))
            pid = original["body"]["project"]["id"]
            prompt = (
                "继续完成原任务。恢复单币上限30%，六币、4h、每天调仓、真实行情、10bps成本。"
                "分别评估等权和风险平价并保存两个候选方案，输出风险贡献与压力测试。"
                if kind == "portfolio"
                else "继续完成原任务：BTC/ETH 20/60双均线，每币45%，不做空，真实4h行情，"
                "下一根开盘成交，回测默认和3倍成本并保存草稿。"
            )
            prompt += " 本次完成这些比较和候选归档即可，不增加试验或改变币池，不必追求默认收益目标。"
            call(
                c,
                f"resumed_{kind}_chat",
                "POST",
                f"/api/lab/projects/{pid}/messages",
                {"text": prompt, "wait": True, "max_tool_calls": 30},
            )
            call(c, f"resumed_{kind}_snapshot", "GET", f"/api/lab/projects/{pid}")
        call(c, "unlimited_usage_snapshot", "GET", "/api/workspace/usage?days=1")
    elif args.stage == "baseline":
        for domain in ["factors", "models", "portfolio"]:
            call(c, domain + "_meta", "GET", f"/api/{domain}/meta")
        expressions = ["returns(30)", "-returns(5)", "-ts_std(ret, 30)"]
        for i, expr in enumerate(expressions):
            call(c, f"factor_{i}_validate", "POST", "/api/factors/validate", {"expression": expr})
            call(c, f"factor_{i}_evaluate", "POST", "/api/factors/evaluate", {**base, "expression": expr})
            call(
                c,
                f"factor_{i}_save",
                "POST",
                "/api/factors",
                {
                    "name": ["趋势动量30", "短期反转5", "低波动30"][i],
                    "expression": expr,
                    "universe": SYMS,
                    "timeframe": "4h",
                    "evaluate": False,
                    "note": "体验测试候选；评估见独立报告",
                },
            )
        call(c, "factor_future_guard", "POST", "/api/factors/validate", {"expression": "delay(close, -1)"})
        for kind in ["ridge", "gbdt"]:
            trained = call(
                c,
                f"model_{kind}_train",
                "POST",
                "/api/models/train",
                {**base, "model": kind, "features": expressions, "folds": 4, "shuffle_test": True, "wait": True},
            )
            rid = trained.get("job", {}).get("run_id")
            if rid:
                call(c, f"model_{kind}_report", "GET", f"/api/models/runs/{rid}")
                call(c, f"model_{kind}_save", "POST", "/api/models", {"run_id": rid, "name": f"体验_{kind}"})
        for opt in ["equal", "risk_parity", "hrp"]:
            evaluated = call(
                c,
                f"portfolio_{opt}",
                "POST",
                "/api/portfolio/evaluate",
                {
                    **base,
                    "config": {
                        "optimizer": opt,
                        "constraints": {"max_weight": 0.30, "max_gross": 1.0, "long_only": True},
                        "rebalance": 6,
                    },
                },
            )
            rid = evaluated.get("run", {}).get("id")
            if rid:
                call(c, f"portfolio_{opt}_save", "POST", "/api/portfolio", {"run_id": rid, "name": f"体验_{opt}"})
        for i, desc in enumerate(
            [
                "构建BTC和ETH的双均线趋势策略：20/60根4小时均线，趋势向上各持有45%，其余现金。不要做空，不用未来数据，下一根开盘成交。",
                "构建BTC和ETH的布林带均值回归策略，20根均线，2倍标准差触发买入，回归均线退出，每个币最多40%，不要做空。",
            ]
        ):
            gen = call(
                c,
                f"strategy_{i}_generate",
                "POST",
                "/api/ai/generate",
                {"description": desc, "securities": SYMS[:2], "timeframe": "4h", "save": True, "name": f"体验策略{i}"},
            )
            code = gen.get("code")
            if code:
                for scale in [1, 3]:
                    call(
                        c,
                        f"strategy_{i}_backtest_cost{scale}",
                        "POST",
                        "/api/research/backtests",
                        {
                            "code": code,
                            "name": f"体验策略{i}",
                            "securities": SYMS[:2],
                            "timeframe": "4h",
                            "bars_limit": 1200,
                            "end": end,
                            "source": "exchange",
                            "fill_at": "next_open",
                            "cost_scale": scale,
                            "wait_seconds": 120,
                        },
                    )
    elif args.stage == "snapshot":
        call(c, "workspace_usage_snapshot", "GET", "/api/workspace/usage?days=1")
        for kind in ["factor", "model", "portfolio", "strategy"]:
            created = json.loads((out / f"lab_{kind}_create.json").read_text(encoding="utf-8"))
            pid = created["body"]["project"]["id"]
            call(c, f"lab_{kind}_snapshot", "GET", f"/api/lab/projects/{pid}")
        call(c, "strategy_library_snapshot", "GET", "/api/strategies")
    elif args.stage == "review":
        for opt in ["equal", "risk_parity", "hrp"]:
            for cost in [10, 30]:
                call(
                    c,
                    f"corrected_portfolio_{opt}_{cost}bps",
                    "POST",
                    "/api/portfolio/evaluate",
                    {
                        **base,
                        "cost_bps": cost,
                        "config": {
                            "optimizer": opt,
                            "rebalance": 6,
                            "constraints": {"max_weight": 0.30, "max_gross": 1.0, "long_only": True},
                        },
                    },
                )
        call(
            c,
            "portfolio_model_link",
            "POST",
            "/api/portfolio/evaluate",
            {
                **base,
                "config": {
                    "optimizer": "mean_var",
                    "expected": "model:体验_ridge",
                    "rebalance": 6,
                    "constraints": {"max_weight": 0.30, "max_gross": 1.0, "long_only": True},
                },
            },
        )
        call(
            c,
            "portfolio_risk_check",
            "POST",
            "/api/portfolio/risk",
            {"weights": {s: 1 / 6 for s in SYMS}, "timeframe": "4h", "bars_limit": 1200, "source": "exchange"},
        )
        factors = call(c, "review_factor_list", "GET", "/api/factors").get("factors", [])
        f = next((f for f in factors if f["name"] == "趋势动量30"), None)
        if f:
            fid = f["id"]
            call(
                c,
                "factor_refresh_before_promotion",
                "POST",
                f"/api/factors/{fid}/refresh",
                {"source": "exchange", "bars_limit": 1200, "cost_bps": 10},
            )
            call(c, "factor_active_gate", "POST", f"/api/factors/{fid}/status", {"status": "active"})
            call(c, "factor_restore_candidate", "POST", f"/api/factors/{fid}/status", {"status": "candidate"})
    else:
        tasks = {
            "factor": "请作为研究助手，构建并评估30期动量、5期反转、30期低波动三个因子，使用真实4小时行情，比较样本外IC、换手和10bps成本后表现，保存候选因子。明确不足，不要把无效因子说成有效。",
            "model": "请构建一个ridge与一个gbdt预测模型，用收益动量、反转、波动率特征预测下一根4小时收益。使用真实行情、4折滚动样本外、泄漏检查和打乱标签对照，保存候选模型并比较结果。",
            "portfolio": "请构建等权与风险平价两个组合，6个币，单币不超过30%，不加杠杆，每天调仓，真实4小时行情，10bps成本。评估收益回撤、风险贡献和压力测试，保存候选方案，比较差异。",
            "strategy": "请构建BTC和ETH的20/60双均线趋势策略，每币最多45%，禁止做空。用真实4小时行情回测，下一根开盘成交，比较默认与3倍成本，保存草稿并说明是否达到实盘研究门槛。",
        }
        for kind, prompt in tasks.items():
            created = call(
                c,
                f"lab_{kind}_create",
                "POST",
                "/api/lab/projects",
                {
                    "kind": kind,
                    "title": f"真实体验_{kind}",
                    "idea": prompt,
                    "securities": SYMS if kind != "strategy" else SYMS[:2],
                    "timeframe": "4h",
                    "bars_limit": 1200,
                    "data_source": "exchange",
                    "model": f"journey/{model}",
                    "max_tool_calls": 30,
                    "max_rounds": 3,
                    "permission": "auto",
                    "instructions": "本次是独立测试，允许创建候选资产、编辑及回测。只做研究，禁止实盘交易和外部通知。请实际调用工具执行，数据不足应如实说明。",
                },
            )
            pid = created.get("project", {}).get("id")
            if pid:
                call(
                    c,
                    f"lab_{kind}_chat",
                    "POST",
                    f"/api/lab/projects/{pid}/messages",
                    {"text": prompt, "wait": True, "max_tool_calls": 30},
                )
                call(c, f"lab_{kind}_final", "GET", f"/api/lab/projects/{pid}")
    for domain in ["factors", "models", "portfolio"]:
        call(c, args.stage + "_inventory_" + domain, "GET", f"/api/{domain}")
reset_services()
