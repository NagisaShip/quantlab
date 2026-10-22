"""FastAPI 应用工厂。

- 挂载所有 API 路由
- 若存在前端构建产物（web/dist），直接作为静态站点服务，实现单端口交付
- 提供 /api/health 供运维探活
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..platform import context
from ..platform.errors import STATUS_TO_CODE, AppError, ErrorCode, error_body
from . import (
    routes_agents,
    routes_ai,
    routes_approvals,
    routes_arena,
    routes_auth,
    routes_capabilities,
    routes_community,
    routes_config,
    routes_demo,
    routes_dsl,
    routes_extensions,
    routes_freqtrade,
    routes_lab,
    routes_lifecycle,
    routes_live,
    routes_market,
    routes_media,
    routes_mentor,
    routes_microlive,
    routes_missions,
    routes_node,
    routes_onboarding,
    routes_ops,
    routes_public,
    routes_realperp,
    routes_research,
    routes_runs,
    routes_strategies,
    routes_studio,
    routes_tasks,
    routes_workflows,
    summaries,
)

WEB_DIST = (
    (Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]) / "web" / "dist"
)


class _LocalRuntimeOnly:
    """非本地部署：``GET /api/local/runtime`` 一律 404。

    账号 / 云端部署没有「本机受管解释器」的概念；把判断放在最外层中间件，
    是为了不先返回 401（避免暴露这个只读诊断接口是否存在）。本地模式仍交给
    ``PlatformMiddleware._local`` 处理，行为不变。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path", "").rstrip("/") == "/api/local/runtime":
            from .deps import get_services

            if get_services().local is None:
                payload = error_body(ErrorCode.NOT_FOUND, "接口不存在", context.current().trace_id or "")
                await JSONResponse(payload, status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(*, with_scheduler: bool = False) -> FastAPI:
    from .routes_research_plugins import enforce

    app = FastAPI(
        dependencies=[Depends(enforce)],
        lifespan=_background if with_scheduler else None,
        title="QuantLab v0.1.0-preview.1 API",
        description=(
            "交易所行情接入、策略管理、回测与仿真、Agent 团队调研、长期 Agent 与通知。\n\n仅为研究结果，非投资建议。"
        ),
        version="0.1.0-preview.1",
    )

    # 不开放跨域：前端与 API 同源部署（开发时由 Vite 代理）。需要跨域时由反向代理显式配置白名单。
    from .middleware import PlatformMiddleware

    app.add_middleware(PlatformMiddleware)
    # 最后添加的中间件最外层：账号部署在鉴权之前就把 /api/local/runtime 归为 404
    app.add_middleware(_LocalRuntimeOnly)
    _install_error_handlers(app)

    # 中文端点摘要必须在 include_router 之前挂上（之后 FastAPI 会把路由包一层）
    summaries.apply(
        routes_auth.router,
        routes_config.router,
        routes_market.router,
        routes_strategies.router,
        routes_agents.router,
        routes_arena.router,
        routes_studio.router,
        routes_research.router,
        routes_ai.router,
        routes_community.router,
        routes_public.router,
        routes_ops.router,
        routes_runs.router,
        routes_tasks.router,
        routes_dsl.router,
        routes_live.router,
        routes_demo.router,
        routes_onboarding.router,
    )

    app.include_router(routes_auth.router)
    app.include_router(routes_onboarding.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_tasks.router)
    app.include_router(routes_approvals.router)
    from . import routes_local

    app.include_router(routes_local.router)
    app.include_router(routes_local.envrouter)
    app.include_router(routes_workflows.router)
    from . import routes_research_plugins

    app.include_router(routes_research_plugins.router)
    app.include_router(routes_dsl.router)
    app.include_router(routes_live.router)
    app.include_router(routes_config.router)
    app.include_router(routes_market.router)
    app.include_router(routes_media.router)
    app.include_router(routes_strategies.router)
    app.include_router(routes_agents.router)
    app.include_router(routes_arena.router)
    app.include_router(routes_studio.router)
    app.include_router(routes_demo.router)
    app.include_router(routes_research.router)
    app.include_router(routes_ai.router)
    app.include_router(routes_lab.router)
    app.include_router(routes_extensions.router)
    try:  # 因子库与信息源依赖 numpy；没装时其余功能照常
        from . import routes_datahub, routes_execution, routes_factors, routes_models, routes_portfolio, routes_rd

        app.include_router(routes_factors.router)
        app.include_router(routes_datahub.router)
        app.include_router(routes_models.router)
        app.include_router(routes_portfolio.router)
        app.include_router(routes_execution.router)
        app.include_router(routes_rd.router)
        from . import routes_validation

        app.include_router(routes_validation.router)
    except ImportError:  # pragma: no cover
        pass
    app.include_router(routes_missions.router)
    app.include_router(routes_community.router)
    app.include_router(routes_public.router)
    from . import routes_admin, routes_collab

    app.include_router(routes_collab.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_ops.router)
    app.include_router(routes_node.router)
    app.include_router(routes_freqtrade.router)
    app.include_router(routes_lifecycle.router)
    app.include_router(routes_capabilities.router)
    app.include_router(routes_microlive.router)
    app.include_router(routes_realperp.router)
    app.include_router(routes_mentor.router)

    # 构建目录可能正在被 npm/Vite 清空重建；只有 index.html 与 assets 都就绪时才挂静态站点，
    # 避免测试或 API 启动因为一个瞬时缺失的目录而整体失败。
    assets_dir = WEB_DIST / "assets"
    if WEB_DIST.is_dir() and (WEB_DIST / "index.html").is_file():
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        dist_root = WEB_DIST.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith("api/"):
                ctx = context.current()
                return JSONResponse(error_body(ErrorCode.NOT_FOUND, "接口不存在", ctx.trace_id), status_code=404)
            candidate = (WEB_DIST / full_path).resolve()
            # 只服务构建目录内的文件，防止 ../ 路径穿越
            if full_path and candidate.is_file() and str(candidate).startswith(str(dist_root)):
                return FileResponse(candidate)
            return FileResponse(
                WEB_DIST / "index.html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
    else:

        @app.get("/", include_in_schema=False)
        def root() -> dict:
            return {
                "message": "前端未构建。请在 web/ 目录执行 npm install && npm run build，"
                "或运行 npm run dev 使用开发服务器。",
                "docs": "/docs",
            }

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """所有错误统一为 {code, message, trace_id, details}。"""
    log = logging.getLogger("quantlab.api")

    def respond(status: int, code: str, message: str, details=None) -> JSONResponse:
        trace_id = context.current().trace_id
        return JSONResponse(
            error_body(code, message, trace_id, details), status_code=status, headers={"x-trace-id": trace_id}
        )

    @app.exception_handler(AppError)
    async def _app_error(_req: Request, exc: AppError):
        return respond(exc.status_code, exc.code, exc.message, exc.details)

    from ..workflows.contracts import WorkflowError

    @app.exception_handler(WorkflowError)
    async def _workflow_error(_req: Request, exc: WorkflowError):
        return respond(400, ErrorCode.INVALID_PARAMETER, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_req: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "请求无效"
        details = exc.detail if not isinstance(exc.detail, str) else None
        return respond(
            exc.status_code, STATUS_TO_CODE.get(exc.status_code, ErrorCode.INVALID_PARAMETER), message, details
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError):
        fields = [
            {
                "field": ".".join(str(x) for x in e.get("loc", ())[1:]) or "body",
                "message": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        summary = "；".join(f"{f['field']}：{f['message']}" for f in fields[:5])
        return respond(422, ErrorCode.INVALID_PARAMETER, f"参数无效：{summary}", {"fields": fields})

    from ..data.service import DataUnavailable
    from ..exchanges.base import UnsupportedFeature
    from ..sdk import StrategyLoadError

    @app.exception_handler(DataUnavailable)
    async def _data_unavailable(_req: Request, exc: DataUnavailable):
        text = str(exc)
        if "429" in text or "418" in text or "rate" in text.lower():
            return respond(503, ErrorCode.UPSTREAM_RATE_LIMITED, f"上游接口限流：{text}")
        return respond(502, ErrorCode.UPSTREAM_UNAVAILABLE, text)

    @app.exception_handler(UnsupportedFeature)
    async def _unsupported(_req: Request, exc: UnsupportedFeature):
        return respond(400, ErrorCode.UNSUPPORTED_CAPABILITY, str(exc))

    @app.exception_handler(StrategyLoadError)
    async def _strategy_error(_req: Request, exc: StrategyLoadError):
        return respond(400, ErrorCode.INVALID_PARAMETER, str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception):
        trace_id = context.current().trace_id
        log.exception("未处理的异常 trace_id=%s", trace_id)
        # 不回显异常文本：可能含路径、SQL 或凭证片段
        return respond(500, ErrorCode.INTERNAL_ERROR, "服务内部错误，请把 trace_id 提供给管理员排查")


@asynccontextmanager
async def _background(_app: FastAPI):
    """控制面不运行任何后台长任务。

    回测、参数优化、实时模拟、定时 Agent 任务都是 run，由独立 worker 认领执行
    （`qagent worker`；docker-compose 里的 worker 服务）。这里只在启动时探测一次策略隔离能力。
    """
    from ..platform import isolation, watchdog
    from .deps import get_services

    isolation.probe()
    # 节点失联看门狗跑在 API 进程里：worker 挂了它还在，并且自己把告警发出去
    dog = watchdog.Watchdog(get_services)
    dog.start()
    yield
    dog.stop.set()


app = create_app(with_scheduler=os.environ.get("QAGENT_BACKGROUND", "1") == "1")


def main() -> None:
    """uvicorn 入口：python -m quant_agent.api.app"""
    import os

    import uvicorn

    uvicorn.run(
        "quant_agent.api.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )


if __name__ == "__main__":
    main()
