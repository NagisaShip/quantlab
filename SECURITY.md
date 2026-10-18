# 安全策略 / Security Policy

[中文](#中文) | [English](#english)

## 中文

QuantLab 涉及研究工作区隔离、模型与交易所密钥，以及默认关闭的真实资金通道。请负责任地报告安全问题。

### 报告方式

- **不要**在公开 Issue 中披露可利用细节、密钥或用户数据。
- 请发送邮件至 **luoyangyuan110@gmail.com**，主题以 `[SECURITY]` 开头。
- 请附上受影响版本、复现步骤、影响范围和建议修复方式；我们会尽快确认并回复。
- 在修复发布前，请不要公开漏洞细节。

### 重点范围

- 认证、会话、CSRF、TOTP 与权限绕过；
- 跨工作区数据读取或写入；
- 密钥、交易所凭证、邀请码或会话令牌泄露；
- Python 策略沙箱逃逸、命令注入、路径穿越与 SSRF；
- 微实盘 / 真实永续的安全联锁被绕过（止损、限额、确认码、急停）；
- 存储型 XSS、富文本 / Markdown / SVG 注入。

### 部署者注意事项

- 不要提交 `.env`、`agent.config.toml`、`.run/`、`.run-keys/` 或任何真实密钥；
- 生产环境必须替换示例数据库密码并妥善保护 `secrets/secret.key`；
- 真实资金通道默认关闭，开启前请阅读 `docs/运维与恢复手册.md`；
- 一旦怀疑密钥泄露，请立即在服务商和交易所侧撤销并轮换。

本项目不提供漏洞赏金，但会在修复后致谢负责任的报告者（如您愿意署名）。

## English

QuantLab handles workspace isolation, model and exchange credentials, and real-money channels that are disabled by default. Please report security issues responsibly.

### Reporting

- **Do not** disclose exploit details, credentials, or user data in a public issue.
- Email **luoyangyuan110@gmail.com** with a subject starting with `[SECURITY]`.
- Include the affected version, reproduction steps, impact, and any suggested fix. We will acknowledge and follow up as soon as possible.
- Please keep details private until a fix is released.

### In scope

- Authentication, sessions, CSRF, TOTP, and authorization bypasses;
- Cross-workspace data access;
- Leakage of API keys, exchange credentials, invite codes, or session tokens;
- Python strategy sandbox escapes, command injection, path traversal, and SSRF;
- Bypass of micro-live / real-perpetual interlocks (stop-loss, limits, confirmation codes, kill switch);
- Stored XSS and unsafe rich-text / Markdown / SVG rendering.

### Deployment notes

- Never commit `.env`, `agent.config.toml`, `.run/`, `.run-keys/`, or real credentials;
- Replace the example database password and protect `secrets/secret.key` in production;
- Real-money channels are off by default; read `docs/Operations-and-Recovery-Manual.en.md` before enabling them;
- If a credential may have leaked, revoke and rotate it at the provider and exchange immediately.

This project does not offer a bug bounty, but we will credit responsible reporters after a fix unless they prefer to remain anonymous.
