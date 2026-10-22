"""Workspace capability policy, with separate personal navigation preferences."""

from __future__ import annotations

from ..platform import context
from .contracts import WorkflowError

PLUGINS = [
    {
        "id": "workflow",
        "name": "研究工作流",
        "description": "研究路径、模块编排、版本与运行证据",
        "core": True,
        "pages": ["workflows", "flow"],
        "dependencies": [],
    },
    {
        "id": "data",
        "name": "数据与产物",
        "description": "数据来源、文件、研究产物与预览",
        "core": True,
        "pages": ["data"],
        "dependencies": [],
    },
    {
        "id": "experiments",
        "name": "实验管理",
        "description": "参数网格、滚动窗口、多次实验比较",
        "pages": [],
        "dependencies": ["workflow"],
    },
    {
        "id": "charts",
        "name": "图表工作台",
        "description": "图表配置与 matplotlib 导出",
        "pages": [],
        "dependencies": ["workflow", "data"],
    },
    {
        "id": "factor",
        "name": "因子研究",
        "description": "因子分析与专业研究资产",
        "pages": ["factors"],
        "dependencies": ["data"],
    },
    {
        "id": "model",
        "name": "模型研究",
        "description": "模型库、训练与评价",
        "pages": ["modellib"],
        "dependencies": ["data"],
    },
    {
        "id": "portfolio",
        "name": "组合研究",
        "description": "组合构建与风险分析",
        "pages": ["portfolio"],
        "dependencies": ["data"],
    },
    {
        "id": "execution",
        "name": "执行研究",
        "description": "执行仿真与专业执行资产",
        "pages": ["execution"],
        "dependencies": ["data"],
    },
]


def policy(service):
    from hashlib import sha256

    ctx = context.current()
    key = sha256((ctx.user_id or "local").encode()).hexdigest()[:24]
    shared = (service.store.get("research_plugin_policy", "workspace") or {}).get("plugins", {})
    personal = (service.store.get("research_plugin_preferences", key) or {}).get("plugins", {})
    return [
        {
            **p,
            "enabled": True if p.get("core") else shared.get(p["id"], {}).get("enabled", True),
            "agent": personal.get(p["id"], {}).get("agent", True),
            "visible": personal.get(p["id"], {}).get("visible", True),
            "configured": True,
        }
        for p in PLUGINS
    ]


def owner(kind, definition=None):
    definition = definition or {}
    domain = definition.get("asset", {}).get("domain")
    if domain in ("factor", "model", "portfolio", "execution"):
        return domain
    category = definition.get("category", "")
    for label, plugin in (
        ("因子", "factor"),
        ("组合", "portfolio"),
        ("执行", "execution"),
        ("模型", "model"),
        ("神经", "model"),
    ):
        if label in category:
            return plugin
    if any(port.get("type") in ("model", "network") for port in definition.get("outputs", {}).values()):
        return "model"
    if kind.startswith(("source.", "table.")):
        return "data"
    if "chart" in kind:
        return "charts"
    for name in ("factor", "model", "portfolio", "execution"):
        if kind.startswith(
            (
                f"research.{name}",
                f"asset.{name}",
                f"native.{name}",
                f"module.{name}",
                f"module.native_{name}",
                f"research.native_{name}",
            )
        ):
            return name
    return "workflow"


def require(service, plugin, agent=False):
    items = {p["id"]: p for p in policy(service)}
    pending, seen = [plugin], set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        item = items[key]
        if not item["enabled"] or (agent and not item["agent"]):
            raise WorkflowError(
                f"「{item['name']}」已停用{' Agent 调用' if agent and item['enabled'] else ''}，请在设置 → 插件管理中调整"
            )
        pending.extend(item["dependencies"])


def impact(service, plugin):
    registry = service.registry()

    def uses(flow, depth=0):
        if depth > 8:
            return False
        for node in flow["nodes"]:
            try:
                definition = registry.get(node["kind"], str(node["version"]))
            except WorkflowError:
                continue
            if owner(node["kind"], definition) == plugin:
                return True
            if definition.get("subflow") and uses(definition["subflow"]["workflow"], depth + 1):
                return True
        return False

    return [{"id": r["id"], "title": r["title"]} for r in service.workflows() if uses(r["workflow"])]


def _update(service, plugin, patch):
    from hashlib import sha256

    if plugin not in {p["id"] for p in PLUGINS}:
        raise WorkflowError("未知插件")
    if any(k not in ("enabled", "agent", "visible") or not isinstance(v, bool) for k, v in patch.items()):
        raise WorkflowError("仅支持 enabled、agent、visible 布尔开关")
    ctx = context.current()
    if "enabled" in patch:
        if not ctx.is_ws_admin:
            raise WorkflowError("只有管理员可以启用或停用工作区能力")
        item = next(p for p in PLUGINS if p["id"] == plugin)
        if item.get("core") and not patch["enabled"]:
            raise WorkflowError("研究核心能力保持启用，可隐藏导航入口")
        current = {p["id"]: p for p in policy(service)}
        if patch["enabled"] and any(not current[d]["enabled"] for d in item["dependencies"]):
            raise WorkflowError("请先启用依赖插件")
        if not patch["enabled"] and any(p["enabled"] and plugin in p["dependencies"] for p in current.values()):
            raise WorkflowError("请先停用依赖此插件的能力")
        state = service.store.get("research_plugin_policy", "workspace") or {"id": "workspace", "plugins": {}}
        state["plugins"].setdefault(plugin, {})["enabled"] = patch["enabled"]
        service.store.upsert("research_plugin_policy", state)
    key = sha256((ctx.user_id or "local").encode()).hexdigest()[:24]
    state = service.store.get("research_plugin_preferences", key) or {"id": key, "plugins": {}}
    state["plugins"].setdefault(plugin, {}).update({k: v for k, v in patch.items() if k != "enabled"})
    service.store.upsert("research_plugin_preferences", state)
    return {"plugins": policy(service), "affected": impact(service, plugin)}


def update(service, plugin, patch):
    from ..store import _FileLock

    with _FileLock(service.root / "plugin-policy.lock"):
        return _update(service, plugin, patch)


def configured_policy(service):
    from .environments import EnvironmentManager

    rows = policy(service)
    needs = {
        "charts": "matplotlib",
        "model": "scikit-learn",
        "factor": "numpy",
        "portfolio": "numpy",
        "execution": "numpy",
    }
    try:
        packages = EnvironmentManager(service.root).info()["packages"]
        for item in rows:
            dependency = needs.get(item["id"])
            item["configured"] = not dependency or dependency in packages
            item["configuration_note"] = f"缺少 {dependency}" if not item["configured"] else ""
    except WorkflowError as exc:
        for item in rows:
            if item["id"] in needs:
                item.update(configured=False, configuration_note=str(exc))
    return rows


def guard_tool(service, name):
    name = name.removeprefix("platform_")
    if name == "workflow_plugins":
        return
    if name.startswith("workflow_"):
        require(service, "workflow", agent=True)
        plugin = (
            "charts"
            if "chart" in name
            else "experiments"
            if "experiment" in name or name == "workflow_compare"
            else "workflow"
        )
        if name in (
            "workflow_artifact",
            "workflow_artifacts",
            "workflow_files",
            "workflow_import_data",
            "workflow_local_files",
        ):
            plugin = "data"
        require(service, plugin, agent=True)


def require_flow(service, flow, agent=False, depth=0):
    if depth > 8:
        raise WorkflowError("子流程超过8层")
    registry = service.registry()
    for node in flow["nodes"]:
        definition = registry.get(node["kind"], str(node["version"]))
        require(service, owner(node["kind"], definition), agent=agent)
        if definition.get("subflow"):
            require_flow(service, definition["subflow"]["workflow"], agent, depth + 1)


def tool_plugin(name):
    if name in ("list_factors", "list_models", "list_portfolios", "list_executions"):
        return {
            "list_factors": "factor",
            "list_models": "model",
            "list_portfolios": "portfolio",
            "list_executions": "execution",
        }[name]
    return ""


def agent_tool_allowed(tool, states):
    """Filter model-visible schemas with the same policy as execution guards."""
    name = tool.name
    if name == "workflow_plugins":
        return True
    required = []
    if name.startswith("workflow_"):
        required.append("workflow")
        if "chart" in name:
            required.append("charts")
        elif "experiment" in name or name == "workflow_compare":
            required.append("experiments")
        elif name in (
            "workflow_artifact",
            "workflow_artifacts",
            "workflow_files",
            "workflow_import_data",
            "workflow_local_files",
        ):
            required.append("data")
    plugin = tool_plugin(name) or ("data" if tool.scene == "data" else "")
    if plugin:
        required.append(plugin)
    seen = set()
    while required:
        key = required.pop()
        if key in seen:
            continue
        seen.add(key)
        state = states[key]
        if not state["enabled"] or not state["agent"]:
            return False
        required.extend(state["dependencies"])
    return True
