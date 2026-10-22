"""Small discoverable examples for humans and agents; no guessed graph dialect."""

from copy import deepcopy

from .contracts import FORMAT, WorkflowError

EXAMPLE = {
    "format": FORMAT,
    "title": "可复现收益统计",
    "nodes": [
        {
            "id": "source",
            "kind": "source.csv",
            "version": "1",
            "params": {"file": "已导入的文件名.csv"},
            "position": {"x": 0, "y": 0},
        },
        {"id": "summary", "kind": "table.summary", "version": "1", "params": {}, "position": {"x": 300, "y": 0}},
    ],
    "edges": [{"source": "source", "source_port": "data", "target": "summary", "target_port": "data"}],
}


def help_for(topic="workflow"):
    if topic == "experiments":
        return {
            "grid": {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": [10, 20]}]},
            "rolling": {
                "mode": "rolling",
                "windows": [
                    {
                        "name": "窗口1",
                        "updates": [
                            {"node": "split", "parameter": "train_end", "value": 2020},
                            {"node": "split", "parameter": "validation_end", "value": 2022},
                        ],
                    }
                ],
            },
            "constraints": "axes为1—4项，每项node/parameter/values；最多8个组合；先plan再submit，提交使用当前expected_revision",
            "submit_example": {
                "workflow_id": "真实工作流ID",
                "expected_revision": 1,
                "plan": {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": [10, 20]}]},
            },
        }
    if topic == "evidence":
        return {
            "child": "workflow_child_evidence(child_run_id=...)自动查父运行；也可workflow_inspect(workflow_id=...,child_run_id=...)省略run_id",
            "explicit": "workflow_inspect(workflow_id=...,run_id=父运行ID,child_run_id=子运行ID,node_id=内部节点ID)",
            "security": "父子关系与工作区均由服务端校验；资产历史摘要不是本轮运行证据",
        }
    if topic == "network":
        from quantlab_research.network import DEFAULT, NODE_FIELDS

        return {
            "format": "quantlab.network/1",
            "example": deepcopy(DEFAULT),
            "node_fields": NODE_FIELDS,
            "connections": "组件的inputs填写上游组件ID；Input声明shape，不含batch。output填写唯一输出组件ID。也支持edges:[{source,target}]，不能与inputs冲突。",
            "aliases": "input→Input，dense→Linear；Linear必须声明out_features。",
        }
    if topic == "artifacts":
        return {
            "display": "workflow_artifact会将真实表格、指标、图片与文件直接展示在对话；workflow_chart_preview生成matplotlib图并展示。不要只报文件路径。",
            "discovery": "workflow_artifacts按workflow_id/run_id/query检索产物；workflow_cancel取消任务；workflow_export导出冻结运行；workflow_notes保存带运行引用的结论。",
            "namespace": "workflow.run.output",
            "source": "workflow_inspect(workflow_id,run_id).run.nodes[node_id].outputs[port].hash",
            "not_artifacts": "资产sha256、code_hash、文件sha256不是运行工件hash。",
            "preview": "workflow_artifact(artifact_hash,offset=0,limit=20)读取表格；workflow_chart_preview(config=...,data=[...])可在没有运行时预览少量显式数据。",
        }
    if topic != "workflow":
        raise WorkflowError("topic支持workflow、network、artifacts、experiments、evidence")
    return {
        "format": FORMAT,
        "example": deepcopy(EXAMPLE),
        "create": "workflow_create(workflow=...)直接创建首版；兼容workflow_revise(workflow_id='',expected_revision=0,workflow=...)。",
        "update": "workflow_revise(workflow_id=真实ID,expected_revision=目录中的revision,workflow=...)；修订冲突不会覆盖。",
        "edges": "推荐source/source_port/target/target_port；兼容from:{node,port},to:{node,port}并在保存时规范化。",
        "steps": [
            "workflow_catalog分页选卡片",
            "workflow_sources读契约和入口",
            "workflow_create",
            "workflow_validate",
            "workflow_run",
            "workflow_inspect携带run_id",
            "workflow_artifact读取outputs中的hash",
        ],
        "data": (
            "source.market 直连交易所公开K线（Binance/OKX/Bybit，参数 symbol/timeframe/bars/exchange/source），"
            "无需先导入文件；source.csv/source.file 用GUI导入或workflow_files查询文件名。"
            "自定义数据源可返回显式示例数据，资产定义快照本身不是行情表。"
        ),
        "market_example": {
            "id": "source",
            "kind": "source.market",
            "version": "1",
            "params": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 1500, "exchange": "auto", "source": "auto"},
        },
    }
