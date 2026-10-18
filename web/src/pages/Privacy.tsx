/** 平台隐私政策：数据分类、用途、保留期、反馈与删除权利。 */
import { Markdown } from '../components/markdown'
import { Heading } from '../components/ui'
import { lang } from '../i18n'

const ZH = `## 1. 适用范围

本政策适用于 QuantLab 部署实例中的账号、工作区、AI 研究室、策略与回测、文件上传、团队协作和反馈功能。自托管部署时，实际运营者是对应部署的维护者。

## 2. 我们处理哪些数据

- **账号与工作区**：用户名、邮箱、角色、登录会话和安全审计记录。
- **研究内容**：对话、Agent 输出、策略代码、回测参数与结果、因子 / 模型 / 组合 / 执行方案。
- **文件与附件**：你主动上传的图片、CSV、PDF、文本和压缩包。
- **用量与计费**：模型调用次数、token、费用、CPU 秒、回测次数、优化组合数。
- **反馈**：你对 Agent 消息的点赞 / 点踩、评分、意见和提交时间。
- **技术日志**：请求时间、错误、IP、浏览器信息，用于安全和故障排查。

## 3. 用途

我们使用这些数据提供研究功能、隔离工作区、执行限额、恢复异常任务、改进模型与产品体验，并保障平台安全。不会出售你的个人数据。

## 4. 模型服务商

调用第三方模型时，必要的对话内容和工具结果会发送给对应服务商。具体处理方式受该服务商的隐私政策约束。工作区管理员应仅配置可信的服务商。

## 5. 保留与删除

工作区管理员可以设置数据保留天数，并开启磁盘占用达到阈值后的自动清理。自动清理只删除超过保留期的数据，运行中的对话和任务不会被删除。

管理员可以关闭“保留 Agent 历史”或“保留上传文件”。关闭后，后台维护任务会尽快清理已结束对话的历史消息和旧附件；为了不影响正在进行的任务，至少会保留一个维护周期。你也可以在“设置 → 隐私与存储”中手动预览并清理指定分类的数据。

## 6. 你的权利

你可以请求访问、导出、更正或删除自己的数据。工作区管理员可以在存储管理中按分类清理；账号删除请通过部署维护者处理。审计记录可能因安全和合规要求保留更长时间。

## 7. 工作区管理员的可见范围

工作区管理员为了管理成员、审核内容、保障安全和排查故障，可以查看：

- 工作区成员名单、角色、邀请状态、登录会话与安全审计记录；
- 工作区内的研究内容：策略、回测、因子 / 模型 / 组合 / 执行方案、帖子、评论、群组消息和上传附件；即使某个策略或回测被设为“仅自己可见”，工作区管理员仍可能为管理、安全或故障排查查看；
- 工作区级与成员级的模型调用、token、费用、CPU 秒、回测次数和存储占用；
- 举报、审核、封禁与管理员操作记录。

工作区管理员不能查看：

- 其他工作区的数据；
- 自己未加入的私聊内容；
- 个人模型服务商密钥的明文；界面只显示是否配置和掩码；
- 部署维护者掌握的服务器级密钥与底层基础设施凭证。

成员把策略、回测、研究记录或文件上传到工作区、群组或策略广场前，应假定工作区管理员可以看到这些内容。管理员对内容的查看、删除、封禁和设置变更会按平台能力记入审计日志。

## 8. 安全

平台使用工作区隔离、加密凭证、会话校验、CSRF 防护和审计日志保护数据。请妥善保管账号密码和 API Key，不要在不信任的设备上保持登录。

## 9. 政策更新

本政策可能随功能变化更新。重要变更会在平台内提示。继续使用平台即表示你接受更新后的政策。

> 本政策是平台运行说明，不构成法律意见；正式对外运营时请由运营主体按所在地法律审阅。
`
const EN = `## 1. Scope

This policy applies to accounts, workspaces, AI Lab conversations, strategies and backtests, uploaded files, collaboration, and feedback in this QuantLab deployment. For self-hosted deployments, the operator of that deployment is the data controller.

## 2. Data we process

- **Account and workspace data**: username, email, role, sessions, and security audit records.
- **Research content**: conversations, Agent output, strategy code, backtest parameters and results, and factor / model / portfolio / execution artifacts.
- **Files and attachments**: images, CSV, PDF, text, and archives you upload.
- **Usage and metering**: model calls, tokens, cost, CPU seconds, backtests, and optimization combinations.
- **Feedback**: message likes/dislikes, ratings, comments, and timestamps.
- **Technical logs**: request time, errors, IP address, and browser information for security and troubleshooting.

## 3. Purposes

We use this data to provide research features, isolate workspaces, enforce quotas, recover interrupted jobs, improve models and product experience, and protect the platform. We do not sell personal data.

## 4. Model providers

When you call a third-party model, the necessary conversation content and tool results are sent to that provider. Their privacy policy applies. Workspace admins should configure only trusted providers.

## 5. Retention and deletion

Workspace admins can set a retention period and enable automatic cleanup when disk usage crosses a threshold. Automatic cleanup only removes data older than the retention period; running conversations and jobs are not deleted.

Admins can disable “retain Agent history” or “retain uploaded files”. After that, background maintenance removes old messages and attachments from completed conversations as soon as practical, while keeping at least one maintenance cycle to avoid disrupting active work. You can also preview and clean specific categories under Settings → Privacy & Storage.

## 6. Your rights

You may request access, export, correction, or deletion of your data. Workspace admins can clean data by category in storage management; account deletion is handled by the deployment operator. Audit records may be retained longer for security and compliance.

## 7. Workspace admin visibility

Workspace admins may access the following to manage members, moderate content, protect the workspace, and troubleshoot:

- member lists, roles, invitation status, sessions, and security audit records;
- workspace research content: strategies, backtests, factor / model / portfolio / execution artifacts, posts, comments, group messages, and uploaded attachments. Even when a strategy or backtest is set to “only me”, a workspace admin may still access it for administration, security, or troubleshooting;
- workspace-level and member-level model calls, tokens, cost, CPU seconds, backtests, and storage usage;
- reports, moderation, bans, and administrator action records.

Workspace admins cannot access:

- data in other workspaces;
- private messages they have not joined;
- plaintext personal model provider keys; the UI shows only whether a key is configured and a masked value;
- server-level secrets and infrastructure credentials held by the deployment operator.

Before uploading strategies, backtests, research records, or files to a workspace, group, or Strategy Square, members should assume that workspace admins can see that content. Admin access, deletion, bans, and settings changes are recorded in the audit log where the platform supports it.

## 8. Security

QuantLab uses workspace isolation, encrypted credentials, session validation, CSRF protection, and audit logs. Keep your password and API keys safe, and do not stay signed in on untrusted devices.

## 9. Changes

This policy may be updated as features change. Material changes will be announced in the product. Continued use means you accept the updated policy.

> This policy describes platform operation and is not legal advice. A production operator should review it under applicable law.
`

export default function PrivacyPage() {
  return (
    <div className="privacy-page">
      <Heading title={lang === 'en' ? 'Privacy Policy' : '隐私政策'} description={lang === 'en' ? 'How this deployment handles research, files, feedback, and retention.' : '说明本部署如何处理研究数据、文件、反馈与保留策略。'} />
      <div className="panel">
        <div className="panel-body"><Markdown text={lang === 'en' ? EN : ZH} /></div>
      </div>
    </div>
  )
}
