# 贡献指南

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp agent.config.example.toml agent.config.toml
pytest -q

# 前端（可选，后端已能单端口托管构建产物）
cd web && npm install && npm run dev
```

## 铁律（提交前自检）

1. **策略红线**：新增/修改策略代码不得调用交易所、网络或文件系统；只能通过 `StrategyContext` 访问时钟、数据与状态，只输出 `set_target_position`。契约检查（`api/deps.py::check_strategy_code`）会对生成代码做静态检查。
2. **禁止未来数据**：任何策略只能使用已收盘的 bar；`ctx.history()` 已保证不返回未来 bar，不要在策略内绕过它。
3. **密钥不入库**：所有密钥走环境变量 / 系统钥匙串，使用 `${ENV_VAR}` 注入；禁止明文写进 `agent.config.toml`、代码或测试。
4. **AI 生成物需审批**：Agent 产出的策略版本 `human_approved` 默认为 false；不得在自动流程中直接启用实时模拟或实盘。
5. **合规文案**：通知与调研文案不得出现收益承诺；统一附"仅为研究结果，非投资建议"（见 `notify.DISCLAIMER`）。
6. **降级不许报错**：没有密钥、模型不可达、数据取不到，都必须是可读的降级路径（模板产出、合成数据兜底），不能是 500 或空白页。

## 代码风格

- 行宽 120，`ruff check` 必须通过。
- 公开函数/类写中文或英文 docstring 均可，团队内保持一致。
- 金额、数量、费用一律用 `Decimal` 或整数最小单位，禁止浮点记账（参见 `exchanges/base.py` 注释）。
- 前端：TypeScript 严格模式，`npm run build` 必须通过（含 `tsc -b`）。

## 分支与提交

- 分支：`feature/<描述>`、`fix/<描述>`。
- 提交信息用祈使句，例如：`add OKX websocket reconnect`。
- 提 PR 前确保 `pytest -q`、`ruff check src tests`、`npm run build` 全绿。

## 新增一个交易所适配器

1. 在 `src/quant_agent/exchanges/` 下新建文件，实现 `ExchangeAdapter` 协议的方法。
2. 不支持的接口**显式抛** `UnsupportedFeature`，禁止用近似语义悄悄替代。
3. 补一条"能力矩阵"注释：市场类型、订单类型、最小下单额、限流权重、测试环境域名。
4. 在 `exchanges/__init__.py` 导出，并在 `tests/` 补契约测试（含未完成 K 线标记、鉴权失败等边界）。

## 新增一个提示词模板

1. 在 `prompts/` 下新建 `<id>.md`，含 frontmatter（id/version/title/temperature/variables）。
2. 模板内用 `{{变量}}` 占位；约束与输出格式要写死。
3. 如涉及新 Agent，在对应 Agent 的 `SYSTEM_PROMPT` 或 `prompt_id` 中引用。
4. 前端「提示词库」页会自动列出，无需改前端代码。

## 新增一个 Agent 角色

1. 在 `src/quant_agent/agents/roles.py` 的 `_BUILTIN_SPECS` 里加一条（id、name、title、system_prompt、temperature、tools、output_format、tags）。
2. `system_prompt` 里**不要**重复写"禁止下单 / 禁止未来数据 / 禁止收益承诺"——团队运行时会统一前置（`team.TEAM_SAFETY_PREFIX`）。
3. 角色提示词会自动出现在前端「提示词库」和「角色库」，无需改前端代码。
4. 若新角色应参与默认团队，更新 `agents/team.py::builtin_teams`。
5. 补测试：`tests/test_api.py` 里角色数断言可能需要同步。

## 前端约定

- 页面放在 `web/src/pages/`，在 `App.tsx` 的 `NAV` 里注册。
- 所有 API 调用集中在 `web/src/api.ts`，不要在页面里直接 `fetch`。
- 视觉语言见 `web/src/styles.css` 顶部注释：冷调画布 + 白色图纸面板 + 发丝分隔线，不用阴影与圆角；
  数字一律用 `.num`（等宽 + tabular-nums）保证列对齐。
- 新增图表放在 `web/src/components/charts.tsx`，用内联 SVG，不引入图表库。
- 空状态必须给出**下一步动作**，不能只说"暂无数据"。

