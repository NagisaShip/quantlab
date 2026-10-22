NODE = {
    "kind": "config.value",
    "version": "1",
    "title": "共享配置",
    "category": "配置",
    "description": "提供一个JSON对象，供下游参数绑定。",
    "inputs": {},
    "outputs": {"config": {"type": "json"}},
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "object", "default": {}}},
        "additionalProperties": False,
    },
    "requirements": [],
}


def execute(ctx, inputs, params):
    return {"config": params["value"]}
