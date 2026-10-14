"""Agent 角色与团队路由（对应新增需求：Agent 团队调研策略）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..agents.roles import RoleDesigner
from ..agents.team import Team
from ..store import Collections
from .deps import get_services
from .schemas import RoleDraftIn, RoleIn, TeamIn, TeamRunIn

router = APIRouter(prefix="/api/agents", tags=["Agent 角色与团队"])


# ---------------- 角色 ----------------
@router.get("/roles")
def list_roles(tag: str | None = None, source: str | None = None) -> dict:
    s = get_services()
    roles = s.roles.list(tag=tag, source=source)
    return {
        "roles": [{**r.to_dict(), "customized": s.roles.is_customized(r)} for r in roles],
        "tools": s.teams.tools.names(),
        "tool_catalog": s.teams.tools.catalog(),
        "models": s.router.available_models(),
        "efforts": [
            {"id": "low", "label": "低"},
            {"id": "medium", "label": "中"},
            {"id": "high", "label": "高"},
            {"id": "max", "label": "极高"},
        ],
    }


@router.post("/roles")
def create_role(payload: RoleIn) -> dict:
    s = get_services()
    role = s.roles.create_custom(payload.model_dump())
    return {"role": role.to_dict()}


@router.put("/roles/{role_id}")
def update_role(role_id: str, payload: RoleIn) -> dict:
    s = get_services()
    existing = s.roles.get(role_id)
    if existing is None:
        raise HTTPException(404, "角色不存在")
    data = payload.model_dump()
    data["id"] = role_id
    data["builtin"] = existing.builtin
    data["source"] = existing.source if existing.builtin else data.get("source", existing.source)
    if data.get("effort") not in ("", "low", "medium", "high", "max"):
        raise HTTPException(400, "努力程度只能是 low / medium / high / max")
    data["avatar"] = (data.get("avatar") or "")[:4]
    role = s.roles.save(type(existing).from_dict({**existing.to_dict(), **data}))
    return {"role": {**role.to_dict(), "customized": s.roles.is_customized(role)}}


@router.post("/roles/{role_id}/reset", summary="内置角色恢复默认设置")
def reset_role(role_id: str) -> dict:
    """内置角色恢复默认设置（id 不变，团队与流水线里的引用不受影响）。"""
    s = get_services()
    try:
        role = s.roles.reset(role_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"role": {**role.to_dict(), "customized": False}}


@router.delete("/roles/{role_id}")
def delete_role(role_id: str) -> dict:
    s = get_services()
    try:
        ok = s.roles.delete(role_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, "角色不存在")
    return {"ok": True}


@router.post("/roles/duplicate/{role_id}")
def duplicate_role(role_id: str) -> dict:
    """内置角色不可改，但可以复制一份自定义角色再改。"""
    s = get_services()
    src = s.roles.get(role_id)
    if src is None:
        raise HTTPException(404, "角色不存在")
    data = src.to_dict()
    data.update({"id": None, "name": f"{src.name}（副本）", "builtin": False, "source": "user"})
    data.pop("id", None)
    role = s.roles.create_custom({**data, "id": None})
    return {"role": role.to_dict()}


@router.post("/roles/draft")
def draft_role(payload: RoleDraftIn) -> dict:
    """AI 辅助创建角色：一句需求 → 结构化角色卡。save=true 时直接入库。"""
    s = get_services()
    designer = RoleDesigner(s.router)
    role = designer.draft(payload.requirement)
    if payload.save:
        s.roles.save(role)
    return {"role": role.to_dict(), "saved": payload.save}


# ---------------- 团队 ----------------
@router.get("/teams")
def list_teams() -> dict:
    s = get_services()
    return {"teams": [t.to_dict() for t in s.teams.list_teams()]}


@router.post("/teams")
def create_team(payload: TeamIn) -> dict:
    s = get_services()
    team = Team(
        id=payload.id or f"team_{__import__('uuid').uuid4().hex[:8]}",
        name=payload.name,
        description=payload.description,
        mode=payload.mode,
        role_ids=payload.role_ids,
        synthesizer_id=payload.synthesizer_id,
        challenge_rounds=payload.challenge_rounds,
    )
    s.teams.save_team(team)
    return {"team": team.to_dict()}


@router.put("/teams/{team_id}")
def update_team(team_id: str, payload: TeamIn) -> dict:
    s = get_services()
    existing = s.teams.get_team(team_id)
    if existing is None:
        raise HTTPException(404, "团队不存在")
    team = Team(
        id=team_id,
        name=payload.name,
        description=payload.description,
        mode=payload.mode,
        role_ids=payload.role_ids,
        synthesizer_id=payload.synthesizer_id,
        challenge_rounds=payload.challenge_rounds,
        created_at=existing.created_at,
    )
    s.teams.save_team(team)
    return {"team": team.to_dict()}


@router.delete("/teams/{team_id}")
def delete_team(team_id: str) -> dict:
    s = get_services()
    if not s.teams.delete_team(team_id):
        raise HTTPException(404, "团队不存在")
    return {"ok": True}


@router.post("/teams/{team_id}/run")
def run_team(team_id: str, payload: TeamRunIn) -> dict:
    """执行一次团队调研。多角色按团队编排模式协作。"""
    s = get_services()
    team = s.teams.get_team(team_id)
    if team is None:
        raise HTTPException(404, "团队不存在")

    materials: dict = {}
    if payload.include_market_data:
        materials["market"] = s.market_summary(payload.symbol, payload.timeframe)
    if payload.strategy:
        try:
            result = s.run_backtest(
                strategy_name=payload.strategy,
                params=payload.params,
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                persist=False,
            )
            materials["backtest"] = {
                "strategy": payload.strategy,
                "params": payload.params,
                "metrics": result.metrics.to_dict(),
            }
        except Exception as e:
            materials["backtest_error"] = str(e)

    run = s.teams.run(team, payload.topic, context=payload.context, materials=materials)
    return {"run": run.to_dict()}


@router.get("/team-runs")
def list_team_runs(limit: int = Query(30, ge=1, le=200)) -> dict:
    s = get_services()
    return {"runs": s.teams.list_runs(limit=limit)}


@router.get("/team-runs/{run_id}")
def get_team_run(run_id: str) -> dict:
    s = get_services()
    run = s.teams.get_run(run_id)
    if run is None:
        raise HTTPException(404, "团队运行记录不存在")
    return {"run": run}


# ---------------- 单角色调用（用于快速试跑一个角色）----------------
@router.post("/roles/{role_id}/run")
def run_single_role(role_id: str, payload: TeamRunIn) -> dict:
    s = get_services()
    role = s.roles.get(role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    temp_team = Team(id=f"_single_{role_id}", name=f"单角色：{role.name}", mode="sequential", role_ids=[role_id])
    materials: dict = {}
    if payload.include_market_data:
        materials["market"] = s.market_summary(payload.symbol, payload.timeframe)
    run = s.teams.run(temp_team, payload.topic, context=payload.context, materials=materials)
    return {"run": run.to_dict()}


# ---------------- 提示词库 ----------------
@router.get("/prompts")
def list_prompts() -> dict:
    """提示词库：任务级模板（prompts/*.md）+ 角色级提示词（来自角色库，不重复维护）。"""
    from pathlib import Path

    s = get_services()
    root = Path(__file__).resolve().parents[3] / "prompts"
    items = []
    if root.exists():
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            meta, body = _split_frontmatter(text)
            meta.setdefault("id", path.stem)
            meta["file"] = path.name
            meta["kind"] = "task"
            meta["body"] = body
            items.append(meta)

    for role in s.roles.list():
        items.append(
            {
                "id": f"role.{role.id}",
                "version": "1",
                "title": f"{role.name}（角色提示词）",
                "kind": "role",
                "file": f"roles/{role.id}",
                "variables": "",
                "temperature": str(role.temperature),
                "model": role.model or "跟随全局路由",
                "body": role.system_prompt,
                "output_format": role.output_format,
                "tools": role.tools,
                "source": role.source,
            }
        )
    return {"prompts": items}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """解析 `key: value` 头部，用 `---` 与正文分隔（分隔符不要求出现在文件首行）。"""
    lines = text.splitlines()
    meta: dict = {}
    sep = next((i for i, line in enumerate(lines) if line.strip() == "---"), None)
    if sep is None:
        return meta, text.strip()
    for line in lines[:sep]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, "\n".join(lines[sep + 1 :]).strip()


@router.get("/store-stats")
def store_stats() -> dict:
    s = get_services()
    return {
        "roles": s.store.count(Collections.ROLES),
        "teams": s.store.count(Collections.TEAMS),
        "team_runs": s.store.count(Collections.TEAM_RUNS),
        "strategies": s.store.count(Collections.STRATEGIES),
        "runs": s.store.count(Collections.RUNS),
        "notifications": s.store.count(Collections.NOTIFICATIONS),
    }
