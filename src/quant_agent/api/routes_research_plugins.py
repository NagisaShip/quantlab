"""Plugin settings remain available even when optional research tools are off."""

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from ..workflows.agent import service
from ..workflows.plugins import configured_policy, impact, update

router = APIRouter(prefix="/api/auth/research-plugins", tags=["研究插件"])


def enforce(connection: HTTPConnection):
    """Disable new work, preserving historical reads, task cancellation and settings."""
    from ..platform.context import current
    from ..workflows.plugins import require

    if connection.scope["type"] == "websocket" or not current().workspace_id:
        return
    if connection.scope.get("method") in ("GET", "HEAD", "OPTIONS"):
        return
    path = connection.url.path
    if path.endswith(("/cancel", "/stop")):
        return
    for prefix, plugin in (
        ("/api/factors", "factor"),
        ("/api/models", "model"),
        ("/api/portfolio", "portfolio"),
        ("/api/execution", "execution"),
        ("/api/workflows/charts", "charts"),
    ):
        if path == prefix or path.startswith(prefix + "/"):
            require(service(), plugin)
    if path.startswith("/api/workflows/") and "/experiments" in path:
        require(service(), "experiments")


class Patch(BaseModel):
    enabled: bool | None = None
    agent: bool | None = None
    visible: bool | None = None


@router.get("")
def listing():
    return {"plugins": configured_policy(service())}


@router.get("/{plugin}/impact")
def affected(plugin: str):
    return {"workflows": impact(service(), plugin)}


@router.put("/{plugin}")
def save(plugin: str, payload: Patch):
    return update(service(), plugin, payload.model_dump(exclude_none=True))
