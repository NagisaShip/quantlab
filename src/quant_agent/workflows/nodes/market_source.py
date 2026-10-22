NODE = {
    "kind": "source.market",
    "version": "1",
    "title": "直连行情数据源",
    "category": "数据源",
    "description": (
        "直接读取交易所公开K线（Binance / OKX / Bybit），无需先导入文件；"
        "只取已收盘bar，记录来源、抓取时间与缺口/重复。每次运行重新抓取，结果冻结进本次运行工件；"
        "取不到真实行情时如实报错，不静默伪造。"
    ),
    "inputs": {},
    "outputs": {
        "data": {"type": "table", "schema": {"type": "array"}},
        "source": {"type": "report"},
    },
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "title": "标的",
                "default": "BTC/USDT",
                "minLength": 3,
                "description": "如 BTC/USDT、ETH/USDT；也可写 BTCUSDT",
            },
            "timeframe": {
                "type": "string",
                "title": "周期",
                "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                "default": "1h",
            },
            "bars": {
                "type": "integer",
                "title": "K线根数",
                "minimum": 50,
                "maximum": 5000,
                "default": 1500,
                "description": "最近N根已收盘K线；与回测窗口一致时建议1500",
            },
            "exchange": {
                "type": "string",
                "title": "交易所",
                "enum": ["auto", "binance", "okx", "bybit"],
                "default": "auto",
                "description": "auto=依次尝试 Binance / OKX / Bybit",
            },
            "source": {
                "type": "string",
                "title": "取数方式",
                "enum": ["auto", "exchange", "synthetic"],
                "default": "auto",
                "description": "auto=优先真实行情；exchange=只接受真实行情；synthetic=显式合成数据（仅离线演示，会在来源中标注）",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    "requirements": [],
    "capabilities": {"network": True, "native": "market_bars", "cache": False},
    "resources": {"scope": "platform market cache", "rows": "explicit bars parameter; no silent sampling"},
    "examples": [
        {"description": "最近1500根BTC/USDT 1小时K线", "parameters": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 1500}}
    ],
}


def execute(ctx, inputs, params):
    # 直连行情由平台运行时注入的原生实现执行（需要交易所适配器与本地缓存）；
    # 独立运行包 / 自定义执行环境没有这些能力，必须明确失败，不能伪造行情。
    raise RuntimeError(
        "「直连行情数据源」需要 QuantLab 平台的行情服务（交易所适配器与本地缓存）；"
        "离线运行包请改用「CSV数据源」并冻结数据"
    )
