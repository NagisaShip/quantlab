"""本地假的 [OI] 兼容服务，用于端到端验证 LLM 链路（不依赖任何真实密钥）。

用法：python3 /tmp/mock_llm.py 8899
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

REPLIES = {
    "trend": (
        "A. 策略逻辑说明\n"
        "当 10 期均线上穿 30 期均线时建立 70% 仓位，下穿或触发 8% 止损时平仓。\n\n"
        "B. Python 代码\n"
        "```python\n"
        "class Strategy:\n"
        "    def initialize(self, ctx):\n"
        "        self.fast, self.slow = 10, 30\n"
        "        self.entry = None\n\n"
        "    def on_bar(self, ctx, bar):\n"
        "        bars = ctx.history(bar.symbol, self.slow)\n"
        "        if len(bars) < self.slow:\n"
        "            return\n"
        "        closes = [b.close for b in bars]\n"
        "        fast_ma = sum(closes[-self.fast:]) / self.fast\n"
        "        slow_ma = sum(closes) / self.slow\n"
        "        if self.entry and bar.close < self.entry * 0.92:\n"
        "            ctx.set_target_position(0.0)\n"
        "            self.entry = None\n"
        "            return\n"
        "        if fast_ma > slow_ma and ctx.target_position == 0.0:\n"
        "            ctx.set_target_position(0.7)\n"
        "            self.entry = bar.close\n"
        "        elif fast_ma < slow_ma:\n"
        "            ctx.set_target_position(0.0)\n"
        "            self.entry = None\n"
        "```\n\n"
        "C. 参数表\n"
        "| 参数 | 默认值 | 范围 | 含义 |\n"
        "| --- | --- | --- | --- |\n"
        "| fast | 10 | 5-20 | 快线周期 |\n"
        "| slow | 30 | 20-60 | 慢线周期 |\n\n"
        "D. 风险与局限\n"
        "横盘期会被反复止损；样本量不足时结论不可信。仅为研究结果，非投资建议。\n"
    ),
    "role": json.dumps(
        {
            "name": "滑点审计员",
            "title": "检查撮合假设与滑点成本",
            "description": "专门检查策略是否把价格触及当成全部成交，并估算真实滑点成本。",
            "system_prompt": (
                "你是滑点审计员，服务于量化研究团队。\n\n"
                "职责：检查撮合假设、估算滑点与半价差、评估容量约束。\n\n"
                "硬性约束：\n"
                "- 禁止调用交易所、网络或任何下单接口。\n"
                "- 禁止使用未来数据。\n"
                "- 不得给出投资建议或收益承诺。\n"
                "- 必须区分保守 / 基准 / 乐观三档假设。"
            ),
            "temperature": 0.2,
            "tools": ["market_summary", "get_metrics"],
            "output_format": "①撮合假设 ②滑点拆解 ③三档区间",
            "tags": ["执行", "成本"],
        },
        ensure_ascii=False,
    ),
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        text = json.dumps(body, ensure_ascii=False)

        if "角色卡设计师" in text:
            reply = REPLIES["role"]
        elif (
            "市场分析师" in text
            or "策略设计师" in text
            or "回测分析师" in text
            or "风控官" in text
            or "研究组长" in text
        ):
            reply = "【本角色分析】已基于给定材料完成分析。\n①结论 ②依据 ③风险与不确定项\n\n仅为研究结果，非投资建议。"
        elif "简报" in text:
            reply = "【策略简报】2026-10-12\n· 净值：10,120.50（+1.20%）\n· 最大回撤：0.80%\n· 交易：14 笔，胜率 57%\n· 异常：无\n仅为研究结果，非投资建议。"
        else:
            reply = REPLIES["trend"]

        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", "mock-model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 240, "total_tokens": 360},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"mock [OI] server on :{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
