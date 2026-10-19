# QuantLab v0.1.0-preview.1 · 文档索引

> 当前为预览版，接口、数据口径与功能边界仍可能调整。功能边界以代码、测试和 `src/quant_agent/platform/features.py` 的能力注册表为准。

[English](README.en.md) | [项目 README](../README.md) | [English README](../README.en.md)

## 建议阅读顺序

1. [平台介绍](平台介绍.md) / [English](Platform-Introduction.en.md)：最新截图、功能地图与使用场景。
2. [测试与验证报告](测试与验证报告.md) / [English](Test-and-Verification-Report.en.md)：静态检查、测试、构建、E2E 与发布清单。
3. [策略协议与数据口径](策略协议与数据口径.md) / [English](Strategy-Protocol-and-Data-Conventions.en.md)：事件、信号、撮合、账本与指标。
4. [交易所模拟盘与策略包](交易所模拟盘与策略包.md) / [English](Exchange-Demo-Trading-and-Strategy-Packages.en.md)：`.qstrat`、`qrun`、终端与模拟盘。
5. [运维与恢复手册](运维与恢复手册.md) / [English](Operations-and-Recovery-Manual.en.md)：部署、恢复、备份与真实资金开关。
6. [隔离能力矩阵](隔离能力矩阵.md) / [English](Isolation-Capability-Matrix.en.md)：Linux / Windows 隔离能力。
7. [个人账户接入教程](个人账户接入教程.md) / [English](Personal-Account-Integration-Guide.en.md)：CSV 与只读 Key。
8. [插件与扩展指南](插件与扩展指南.md) / [English](Plugin-and-Extension-Guide.en.md)：API 约定、策略插件、数据源接入、内容块与能力开关；哪些扩展方式目前不支持。
9. [API 参考](API参考.md) / [English](API-Reference.en.md)：613 个操作；机器可读版本 [openapi.json](openapi.json)。

## 当前能力边界

- 现货：Binance / OKX / Bybit 公共行情、回测、纸面模拟与交易所模拟盘。
- 永续：线性 USDT-M、币本位反向和交割；支持逐仓/全仓、单向/对冲；资金费、MMR 档位、强平费用与滑点进入账本。
- `.qstrat`：支持现货与线性/反向/交割永续包；导入时复核标的后缀、合约类型、保证金/持仓模式与标准答案。
- Freqtrade：`can_short` / `leverage()` 仅用于衍生品标的；现货仍只做多。
- 真实资金：微实盘仅 Binance / OKX 现货；真实永续仅 Binance / OKX 线性 USDT-M，两者默认关闭且使用独立安全闭环。
- 研究近似：交易所特定 MMR、标记价、交割价、强平规则仍需用账户账单核验，不能描述为交易所生产级风险引擎。

## 文档维护规则

- 中文文档与英文文档成对维护；修改能力边界时两边同时更新。
- 不宣称代码尚未实现或受功能开关阻断的能力。
- 功能开关、交易所范围、合约类型和强平假设必须与 `platform/features.py`、`exchanges/contracts.py` 一致。
- 测试数量、API 操作数和截图以最近一次完整运行结果为准。
- 产品路线、一次性设计稿和临时补丁说明不进入长期文档目录。

## 联系方式

联系邮箱：[luoyangyuan110@gmail.com](mailto:luoyangyuan110@gmail.com)
