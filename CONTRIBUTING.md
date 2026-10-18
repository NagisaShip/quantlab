# 贡献指南 / Contributing Guide

[English](#english) | [中文](#中文)

---

## 中文

感谢您对 QuantLab 的关注！我们欢迎所有形式的贡献。

**主要贡献者：GitHub 用户 [jamesusa99](https://github.com/jamesusa99) 与 [NagisaShip](https://github.com/NagisaShip)。**

### 🎯 贡献方式

#### 1. 报告问题
- 使用 GitHub Issues 报告 Bug
- 提供复现步骤、环境信息、错误日志
- 搜索是否已有相同问题

#### 2. 提出建议
- 使用 GitHub Discussions 讨论新功能
- 说明使用场景和预期效果
- 考虑向后兼容性

#### 3. 提交代码
- Fork 仓库并创建特性分支
- 遵循代码规范
- 添加测试覆盖
- 更新相关文档

### 🔧 开发环境设置

#### 后端
```bash
# 安装依赖
pip install -r requirements-dev.lock
pip install --no-deps -e .

# 运行测试
pytest

# 代码检查
ruff check src tests scripts
ruff format --check src tests scripts
```

> 项目没有单独的 mypy 门禁；类型检查以 CI 中的 ruff 与测试为准。

#### 前端
```bash
cd web

# 安装依赖
npm ci

# 开发服务器
npm run dev

# 类型检查 + 构建（npm run build 内含 tsc -b）
npm run build
```

> 前端没有单独的 `lint` / `type-check` 脚本；请以 `npm run build` 作为类型与构建门禁。

### 📝 代码规范

#### Python
- 遵循 PEP 8
- 使用 `ruff` 格式化代码
- 类型注解使用 Python 3.11+ 语法
- 文档字符串遵循 Google Style

```python
def calculate_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    """计算夏普比率。

    Args:
        returns: 收益率序列
        risk_free: 无风险利率

    Returns:
        夏普比率

    Raises:
        ValueError: 收益率序列为空
    """
    ...
```

#### TypeScript
- 使用 `strict` 模式
- 优先使用函数组件 + Hooks
- Props 接口统一命名为 `<ComponentName>Props`
- 使用 `const` 声明常量

```typescript
interface BacktestCardProps {
  runId: string
  onSelect: (id: string) => void
}

export function BacktestCard({ runId, onSelect }: BacktestCardProps) {
  // ...
}
```

### 🧪 测试要求

#### 单元测试
- 新功能必须有测试覆盖
- 测试文件命名：`test_<module>.py`
- 使用 `pytest` fixtures 复用设置

```python
def test_ledger_golden_fixture(ledger):
    """黄金样例：899.9 + 110 - 0.11 = 1009.79"""
    ledger.deposit('USDT', Decimal('1000'))
    ledger.buy('BTC', Decimal('1'), Decimal('100'), fee=Decimal('0.1'))
    assert ledger.cash('USDT') == Decimal('899.9')
    # ...
```

#### 集成测试
- API 端点测试使用 `TestClient`
- 数据库测试使用事务回滚

#### 验收测试
- 关键业务逻辑必须有验收测试
- 参考 `tests/test_acceptance.py`

### 📚 文档要求

#### 代码注释
- 复杂逻辑必须有注释说明
- 优先写清晰的代码，而不是过多注释

#### API 文档
- 所有端点必须有 docstring
- 使用 Pydantic 模型定义请求/响应

#### 用户文档
- 新功能需要更新 `docs/` 下的相关文档
- 提供使用示例和截图

### 🔀 Pull Request 流程

1. **创建分支**
   ```bash
   git checkout -b feature/amazing-feature
   ```

2. **开发与测试**
   ```bash
   # 后端
   pytest
   ruff check src tests scripts
   ruff format --check src tests scripts

   # 前端（含类型检查）
   npm run build
   ```

3. **提交代码**
   ```bash
   git commit -m "feat: add amazing feature

   - 详细描述改动内容
   - 说明设计决策
   - 关联相关 Issue

   Co-Authored-By: Your Name <email@example.com>"
   ```

4. **推送并创建 PR**
   ```bash
   git push origin feature/amazing-feature
   ```

5. **PR 标题格式**
   - `feat: 新功能`
   - `fix: Bug 修复`
   - `docs: 文档更新`
   - `refactor: 代码重构`
   - `test: 测试补充`
   - `chore: 构建/工具链更新`

6. **PR 描述模板**
   ```markdown
   ## 改动说明
   简要描述这个 PR 的内容

   ## 关联 Issue
   Closes #123

   ## 改动类型
   - [ ] Bug 修复
   - [ ] 新功能
   - [ ] 破坏性变更
   - [ ] 文档更新

   ## 测试
   - [ ] 添加了单元测试
   - [ ] 所有测试通过
   - [ ] 手动测试通过

   ## 检查清单
   - [ ] 代码遵循项目规范
   - [ ] 更新了相关文档
   - [ ] 没有引入新的警告
   ```

### 🎨 Commit 信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Type:**
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档
- `style`: 格式（不影响代码运行）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试
- `chore`: 构建/工具

**示例:**
```
feat(backtest): 支持多周期回测

- 添加 multi_timeframe 参数
- 实现周期间数据对齐
- 更新回测引擎文档

Closes #42
```

### 🏗️ 项目结构

```
quantlab/
├── src/quant_agent/       # 后端源码
│   ├── agents/            # AI Agent
│   ├── api/               # API 端点
│   ├── engine/            # 回测/模拟引擎
│   ├── platform/          # 平台基础设施
│   ├── sdk/               # 策略 SDK
│   └── strategies/        # 策略模板
├── web/src/               # 前端源码
│   ├── components/        # React 组件
│   ├── pages/             # 页面
│   ├── api.ts             # API 客户端
│   └── i18n/              # 国际化
├── tests/                 # 测试
├── docs/                  # 文档
└── scripts/               # 脚本
```

### 🐛 调试技巧

#### 后端调试
```python
# 启用调试日志
export LOG_LEVEL=DEBUG
qagent serve

# 使用 pytest 的详细输出
pytest -vv -s

# 只运行特定测试
pytest tests/test_ledger.py::test_golden_fixture
```

#### 前端调试
```bash
# 开发模式（热重载）
npm run dev

# 查看构建警告
npm run build

# 检查 bundle 大小
npm run build -- --report
```

### 📖 推荐阅读

- [平台介绍](docs/平台介绍.md) - 了解架构
- [策略协议与数据口径](docs/策略协议与数据口径.md) - 核心协议
- [测试与验证报告](docs/测试与验证报告.md) - 测试体系

### ❓ 常见问题

#### Q: 如何添加新的 API 端点？
1. 在 `src/quant_agent/api/routes_*.py` 添加路由
2. 定义 Pydantic 模型（请求/响应）
3. 添加集成测试
4. 文档会自动生成

#### Q: 如何添加新的策略模板？
1. 在 `src/quant_agent/strategies/sdk_templates.py` 添加模板
2. 添加单元测试验证模板可运行
3. 更新前端模板列表

#### Q: 如何添加新的翻译？
1. 在代码中使用中文文案
2. 在 `web/src/i18n/en.json` 添加英文翻译
3. 测试两种语言下的显示效果

### 🤝 行为准则

- 尊重所有贡献者
- 建设性的讨论和反馈
- 专注于代码和想法，而不是个人
- 遵守开源社区规范

### 📧 联系方式

- GitHub Issues: 技术问题和 Bug 报告
- GitHub Discussions: 功能讨论和问答
- Email: quantlab@example.com

---

## English

Thank you for your interest in QuantLab! We welcome all forms of contributions.

**Primary contributors: GitHub users [jamesusa99](https://github.com/jamesusa99) and [NagisaShip](https://github.com/NagisaShip).**

### 🎯 Ways to Contribute

#### 1. Report Issues
- Use GitHub Issues to report bugs
- Provide reproduction steps, environment info, and error logs
- Search for existing issues first

#### 2. Suggest Features
- Use GitHub Discussions for feature requests
- Explain use cases and expected behavior
- Consider backward compatibility

#### 3. Submit Code
- Fork the repository and create a feature branch
- Follow code style guidelines
- Add test coverage
- Update relevant documentation

### 🔧 Development Setup

#### Backend
```bash
# Install dependencies
pip install -r requirements-dev.lock
pip install --no-deps -e .

# Run tests
pytest

# Code quality checks
ruff check src tests scripts
ruff format --check src tests scripts
```

> This project does not have a separate mypy gate; CI uses ruff and the test suite.

#### Frontend
```bash
cd web

# Install dependencies
npm ci

# Dev server
npm run dev

# Type check + build (npm run build runs tsc -b)
npm run build
```

> The frontend has no separate `lint` / `type-check` scripts; use `npm run build` as the type and build gate.

### 📝 Code Style

#### Python
- Follow PEP 8
- Use `ruff` for formatting
- Type hints with Python 3.11+ syntax
- Docstrings in Google Style

```python
def calculate_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    """Calculate Sharpe ratio.

    Args:
        returns: Return series
        risk_free: Risk-free rate

    Returns:
        Sharpe ratio

    Raises:
        ValueError: If returns is empty
    """
    ...
```

#### TypeScript
- Use `strict` mode
- Prefer function components with Hooks
- Name props interfaces as `<ComponentName>Props`
- Use `const` for constants

```typescript
interface BacktestCardProps {
  runId: string
  onSelect: (id: string) => void
}

export function BacktestCard({ runId, onSelect }: BacktestCardProps) {
  // ...
}
```

### 🧪 Testing Requirements

#### Unit Tests
- New features must have test coverage
- Test files: `test_<module>.py`
- Use `pytest` fixtures for setup reuse

```python
def test_ledger_golden_fixture(ledger):
    """Golden test: 899.9 + 110 - 0.11 = 1009.79"""
    ledger.deposit('USDT', Decimal('1000'))
    ledger.buy('BTC', Decimal('1'), Decimal('100'), fee=Decimal('0.1'))
    assert ledger.cash('USDT') == Decimal('899.9')
    # ...
```

#### Integration Tests
- API endpoint tests use `TestClient`
- Database tests use transaction rollback

#### Acceptance Tests
- Critical business logic must have acceptance tests
- See `tests/test_acceptance.py` for examples

### 📚 Documentation

#### Code Comments
- Complex logic must be documented
- Prefer clear code over excessive comments

#### API Documentation
- All endpoints must have docstrings
- Use Pydantic models for request/response

#### User Documentation
- New features require updates to `docs/`
- Provide usage examples and screenshots

### 🔀 Pull Request Process

1. **Create Branch**
   ```bash
   git checkout -b feature/amazing-feature
   ```

2. **Develop & Test**
   ```bash
   # Backend
   pytest
   ruff check src tests scripts
   ruff format --check src tests scripts

   # Frontend (includes type check)
   npm run build
   ```

3. **Commit**
   ```bash
   git commit -m "feat: add amazing feature

   - Detailed description
   - Design decisions
   - Related issues

   Co-Authored-By: Your Name <email@example.com>"
   ```

4. **Push & Create PR**
   ```bash
   git push origin feature/amazing-feature
   ```

5. **PR Title Format**
   - `feat: new feature`
   - `fix: bug fix`
   - `docs: documentation update`
   - `refactor: code refactoring`
   - `test: add tests`
   - `chore: build/tooling`

6. **PR Description Template**
   ```markdown
   ## Description
   Brief description of changes

   ## Related Issue
   Closes #123

   ## Type of Change
   - [ ] Bug fix
   - [ ] New feature
   - [ ] Breaking change
   - [ ] Documentation update

   ## Testing
   - [ ] Added unit tests
   - [ ] All tests pass
   - [ ] Manual testing done

   ## Checklist
   - [ ] Follows project style
   - [ ] Updated documentation
   - [ ] No new warnings
   ```

### 🎨 Commit Convention

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting
- `refactor`: Refactoring
- `perf`: Performance
- `test`: Testing
- `chore`: Build/tooling

**Example:**
```
feat(backtest): support multi-timeframe backtesting

- Add multi_timeframe parameter
- Implement timeframe data alignment
- Update backtest engine docs

Closes #42
```

### 🏗️ Project Structure

```
quantlab/
├── src/quant_agent/       # Backend source
│   ├── agents/            # AI agents
│   ├── api/               # API endpoints
│   ├── engine/            # Backtest/simulation
│   ├── platform/          # Infrastructure
│   ├── sdk/               # Strategy SDK
│   └── strategies/        # Strategy templates
├── web/src/               # Frontend source
│   ├── components/        # React components
│   ├── pages/             # Pages
│   ├── api.ts             # API client
│   └── i18n/              # Internationalization
├── tests/                 # Tests
├── docs/                  # Documentation
└── scripts/               # Scripts
```

### 🐛 Debugging Tips

#### Backend
```python
# Enable debug logging
export LOG_LEVEL=DEBUG
qagent serve

# Verbose pytest output
pytest -vv -s

# Run specific test
pytest tests/test_ledger.py::test_golden_fixture
```

#### Frontend
```bash
# Dev mode with HMR
npm run dev

# Check build warnings
npm run build

# Analyze bundle size
npm run build -- --report
```

### 📖 Recommended Reading

- [Platform Introduction](docs/平台介绍.md) - Architecture overview
- [Strategy Protocol](docs/策略协议与数据口径.md) - Core protocol
- [Test & Verification Report](docs/测试与验证报告.md) - Testing system

### ❓ FAQ

#### Q: How to add a new API endpoint?
1. Add route in `src/quant_agent/api/routes_*.py`
2. Define Pydantic models (request/response)
3. Add integration tests
4. Documentation auto-generates

#### Q: How to add a new strategy template?
1. Add template in `src/quant_agent/strategies/sdk_templates.py`
2. Add unit tests to verify it runs
3. Update frontend template list

#### Q: How to add translations?
1. Use Chinese text in code
2. Add English translation in `web/src/i18n/en.json`
3. Test both languages

### 🤝 Code of Conduct

- Respect all contributors
- Constructive discussion and feedback
- Focus on code and ideas, not people
- Follow open source community norms

### 📧 Contact

- GitHub Issues: Technical issues and bug reports
- GitHub Discussions: Feature discussions and Q&A
- Email: quantlab@example.com

---

Thank you for contributing to QuantLab! 🎉
