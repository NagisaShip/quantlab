"""API 层导出。

注意：不要在此处 `from .app import app`——那会让包属性 `app` 遮蔽子模块
`quant_agent.api.app`，导致 `python -m quant_agent.api.app` 与 uvicorn 的
字符串导入失效。应用实例请用 `get_app()`。
"""

from .deps import AppServices, get_services, reset_services, set_services


def get_app(*, with_scheduler: bool = False):
    """延迟导入并构建 FastAPI 应用（避免导入期副作用）。"""
    from .app import create_app

    return create_app(with_scheduler=with_scheduler)


__all__ = ["AppServices", "get_app", "get_services", "reset_services", "set_services"]
