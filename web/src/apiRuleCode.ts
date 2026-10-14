/* 规则 ↔ 代码 统一：规则生成代码、代码识别规则（AST 比较）、DSL 策略转代码策略。 */
import type { DslProblem, StrategyVersion } from './api3'
import { post } from './http'
import type { RuleSpec } from './pages/rulecode/RuleForm'

export interface RuleToCode {
  ok: boolean
  problems: DslProblem[]
  code: string | null
  params?: Record<string, number>
  warmup_bars?: number
  summary?: string[]
}

export interface RuleFromCode {
  /** true：代码恰好是顶部 @rule 描述的规则，可以在规则视图编辑 */
  is_rule: boolean
  spec: RuleSpec | null
  /** is_rule=false 时的原因（没有 @rule 块 / 代码改动超出规则 / 规则不合法） */
  reason: string
  warnings: string[]
  /** 第一处与规则生成代码不一致的位置 */
  diff: string
  problems?: DslProblem[]
  summary?: string[]
  warmup_bars?: number
}

export const ruleCode = {
  toCode: (spec: RuleSpec) => post<RuleToCode>('/api/dsl/to-code', { spec }),
  fromCode: (code: string) => post<RuleFromCode>('/api/dsl/from-code', { code }),
  convertDsl: (slug: string) =>
    post<{ strategy: { slug: string; kind: string }; version: StrategyVersion; code: string }>(`/api/dsl/strategies/${encodeURIComponent(slug)}/to-code`),
}
