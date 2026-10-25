# Runtime 对照与包装层审计

本目录仅用于实验，不改变产品模型路由、预算账本或发布依赖。原始请求只经过进程内本地网关，第三方 SDK 使用占位密钥；真实凭据仅从 `OPENCODE_API_KEY` 读取，不落报告。

在已安装 QuantLab 开发依赖的环境中创建独立环境，避免替换产品依赖：

```powershell
python -m venv --system-site-packages .tmp/runtime-evaluation/venv
.tmp/runtime-evaluation/venv/Scripts/python.exe -m pip install -r scripts/runtime_eval/requirements.txt
# 通过本机安全方式设置当前进程的 OPENCODE_API_KEY，不要写入脚本或 Git。
.tmp/runtime-evaluation/venv/Scripts/python.exe scripts/runtime_eval/live_compare.py --output .tmp/my-live-run --repeats 3 --max-requests 240
.tmp/runtime-evaluation/venv/Scripts/python.exe scripts/runtime_eval/mock_compare.py --output .tmp/my-mock-run --repeats 20
.tmp/runtime-evaluation/venv/Scripts/python.exe scripts/runtime_eval/mock_compare.py --output .tmp/my-journal-pair --compare-journal-ref 0f5a3df --repeats 30
python scripts/runtime_eval/audit_scheduled.py --output .tmp/my-schedule-audit
python -X utf8 scripts/runtime_eval/document_smoke.py --output .tmp/my-chart-documents
.tmp/runtime-evaluation/venv/Scripts/python.exe scripts/runtime_eval/document_compare.py --output .tmp/my-document-run
```

- `live_compare.py`：4 个固定任务，每个配置 3 次，模型固定 `deepseek-v4.1-flash`。旧循环来自本地 Git `188f997`；使用当前公共消息/HTTP 传输层以隔离循环差异。不是完整旧版应用重建。LangGraph 另测 SQLite checkpointer，Pydantic AI 测默认非持久配置，不把两者宣称为功能等价的持久内核。
- `mock_compare.py`：统一 3 模型回合与 2 工具调用，2 次预热和 20 次正式样本；包含客户端创建、HTTP 序列化和存储，并非纯调度器纳秒基准。
  `--compare-journal-ref` 用同一当前 Agent、相同模型和持久化路径规则，随机交替比较指定 Git 版本的 Journal 与当前实现，隔离连接复用的影响；只加载本地可信 Git 版本。
- `cases.py`：固定资料、受限算术、4 组隐藏代码输入和结构化输出验收。代码评估是受限本地测试，不是通用安全执行环境。报告保留两项评分器修正，不能把评分器限制误算为模型错误。
- `audit_scheduled.py`：真实 Worker 和新进程探针，只用捕获通知器，**不发送外部通知**。每次使用新输出目录。
- `document_compare.py`：各执行器生成 Markdown/slide plan，统一交给当前平台 Word/PPT 构建器。测的是文稿规划与共同渲染整合，不是每个框架独立文档引擎，也没有进行模型视觉自检。不要修饰候选结果来抬高评分。环境无法渲染时保留失败，另列诊断条件。
- `summarize.py`：重新评分已保存输出，不调用模型。默认读取本次 `live-final` 与 `mock-final-v2`；原始评分保留在结果中，复核评分另存 `validated_grade`。

真实模型结果会波动。先检查 `run-status.json`、样本数、服务商状态和用量缺失，再解释分数。不得把少量任务的成功率视为 SWE-bench、生产 SLO 或框架排名。运行预算跨恢复累计；规格或代码变化时必须使用新输出目录。
