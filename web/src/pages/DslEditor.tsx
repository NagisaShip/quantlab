import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { v3 } from '../api2'
import { v4, type DslProblem, type DslValidation } from '../api3'
import { ruleCode } from '../apiRuleCode'
import { ApiError } from '../http'
import { Callout, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import { makeErrAt, RuleForm, type RuleSpec as Spec } from './rulecode/RuleForm'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

export default function DslEditorPage({ slug, onNavigate, notify, setBusy }: PageProps & { slug?: string }) {
  const [spec, setSpec] = useState<Spec | null>(null)
  const [text, setText] = useState('')
  const [textError, setTextError] = useState('')
  const [view, setView] = useState<'form' | 'json'>('form')
  const [name, setName] = useState('')
  const [val, setVal] = useState<DslValidation | null>(null)
  const [examples, setExamples] = useState<Record<string, Spec>>({})
  const [indicatorTypes, setIndicatorTypes] = useState<{ type: string; label: string }[]>([])
  const [savedSlug, setSavedSlug] = useState(slug)
  const timer = useRef<number>()

  useEffect(() => {
    v4.dslSchema().then((s) => {
      setExamples(s.examples as unknown as Record<string, Spec>)
      setIndicatorTypes(s.indicators)
      if (!slug) {
        const first = Object.values(s.examples)[0] as unknown as Spec
        setSpec(first)
        setName(first.name ?? '规则策略')
      }
    }).catch((e) => notify(e.message, true))
    if (slug) {
      v3.strategy(slug).then((r) => {
        // 已转成代码策略：去策略编辑器（那里有规则 / 代码切换）
        if (r.strategy.kind !== 'dsl') { onNavigate(`editor/${slug}`); return }
        try {
          const sp = JSON.parse(r.strategy.code ?? "") as Spec
          setSpec(sp)
          setName(r.strategy.name)
        } catch {
          notify('该策略不是 DSL 策略', true)
        }
      }).catch((e) => notify(e.message, true))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, notify])

  // 表单与 JSON 是同一个对象：表单改动同步到 JSON 文本；JSON 合法时同步回表单
  useEffect(() => {
    if (spec && view === 'form') setText(JSON.stringify(spec, null, 2))
  }, [spec, view])

  // 防抖校验（服务端是唯一裁判）
  useEffect(() => {
    if (!spec) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      v4.dslValidate(spec).then(setVal).catch(() => undefined)
    }, 350)
    return () => window.clearTimeout(timer.current)
  }, [spec])

  const problems = val?.problems ?? []
  const errAt = useMemo(() => makeErrAt(problems), [problems])

  if (!spec) return <Loading />

  const save = async (andRun: boolean) => {
    try {
      const r = await v4.dslSave({ name: name || '规则策略', spec, slug: savedSlug })
      setSavedSlug(r.strategy.slug)
      notify(`已保存（版本 v${r.version.version_no}）`)
      if (andRun) {
        setBusy({ title: '回测运行中', note: '由 worker 执行 DSL 解释器，不执行任何代码' })
        try {
          const bt = await v3.runBacktest({ slug: r.strategy.slug, persist: true })
          if (bt.id) onNavigate(`backtest/${bt.id}`)
          else notify('回测已排队，稍后在回测记录中查看')
        } finally {
          setBusy(null)
        }
      } else if (!savedSlug) {
        window.history.replaceState(null, '', `#/dsl/${r.strategy.slug}`)
      }
    } catch (e) {
      const ps = e instanceof ApiError ? (e.details as { problems?: DslProblem[] }).problems : undefined
      if (ps) setVal({ ok: false, problems: ps })
      notify((e as Error).message, true)
    }
  }

  // 规则 → 代码：先保存规则，再把同一个策略原地转成带 @rule 注释的 Python 代码策略
  const toCode = async () => {
    if (!val?.ok) { notify('规则未通过校验，修正后才能转为代码', true); return }
    if (!await appConfirm('转为代码策略后在代码编辑器中编辑。代码保持与 @rule 注释一致时，仍可切回规则视图。')) return
    try {
      const r = await v4.dslSave({ name: name || '规则策略', spec, slug: savedSlug })
      await ruleCode.convertDsl(r.strategy.slug)
      onNavigate(`editor/${r.strategy.slug}`)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  return (
    <>
      <Heading title={savedSlug ? `编辑规则策略` : '新建规则策略'}
        description="表单与 JSON 是同一份规则；切到「代码」会生成带 @rule 注释的 Python 策略。"
        actions={
          <div className="button-row">
            <Segments value="rule" ariaLabel="编辑方式" onChange={(v) => { if (v === 'code') toCode() }}
              options={[{ value: 'rule', label: '规则' }, { value: 'code', label: '代码' }]} />
            {Object.keys(examples).length > 0 && (
              <select onChange={(e) => { const ex = examples[e.target.value]; if (ex) { setSpec(ex); setName(ex.name ?? name) } e.target.value = '' }} defaultValue="">
                <option value="" disabled>载入示例…</option>
                {Object.entries(examples).map(([k, v]) => <option key={k} value={k}>{v.name ?? k}</option>)}
              </select>
            )}
            <button className="button" onClick={() => save(false)}>保存</button>
            <button className="button primary" disabled={!val?.ok} onClick={() => save(true)}>保存并回测</button>
          </div>
        } />
      <div className="dsl-layout">
        <section className="panel">
          <div className="panel-heading">
            <div><h2>规则定义</h2></div>
            <Segments value={view} onChange={(v) => {
              if (v === 'form' && textError) { notify('JSON 有语法错误，修正后才能切回表单', true); return }
              setView(v)
            }} options={[{ value: 'form', label: '逐项表单' }, { value: 'json', label: 'JSON 视图' }]} />
          </div>
          <div className="panel-body">
            {view === 'json' ? (
              <>
                <textarea className="json-view" spellCheck={false} value={text} onChange={(e) => {
                  setText(e.target.value)
                  try {
                    setSpec(JSON.parse(e.target.value))
                    setTextError('')
                  } catch (err) {
                    setTextError((err as Error).message)
                  }
                }} />
                {textError && <Callout tone="fail">JSON 语法错误：{textError}</Callout>}
              </>
            ) : (
              <RuleForm spec={spec} onChange={setSpec} errAt={errAt} indicatorTypes={indicatorTypes} name={name} onNameChange={setName} />
            )}
          </div>
        </section>
        <aside>
          <Panel title={val ? (val.ok ? <>校验通过 <Tag>预热 {val.warmup_bars} 根</Tag></> : <>未通过校验 <Tag tone="fail">{problems.length} 处</Tag></>) : '校验中…'}>
            {val?.ok ? (
              <>
                <ol className="summary-lines">{val.summary?.map((s) => <li key={s}>{s}</li>)}</ol>
                {val.warnings?.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
                <p className="hint mono">code_hash {val.code_hash?.slice(0, 16)}…</p>
              </>
            ) : (
              problems.map((p, i) => <div className="problem" key={i}><code>{p.path}</code><span>{p.message}</span></div>)
            )}
          </Panel>
          {savedSlug && <VersionsPanel slug={savedSlug} notify={notify} />}
        </aside>
      </div>
    </>
  )
}

function VersionsPanel({ slug, notify }: { slug: string; notify: PageProps['notify'] }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof v4.versions>> | null>(null)
  const load = useCallback(() => { v4.versions(slug).then(setData).catch(() => undefined) }, [slug])
  useEffect(load, [load])
  const LABEL = useMemo(() => ({ DRAFT: '草稿', VALIDATED: '已校验', FROZEN: '已冻结', ARCHIVED: '已归档' }), [])
  if (!data) return null
  return (
    <Panel title="版本（内容不可改写）" subtitle="改规则或参数产生新版本；冻结版本才能进入纸面模拟。" flush>
      <table>
        <tbody>
          {data.versions.map((v) => (
            <tr key={v.id}>
              <td>v{v.version_no}{v.id === data.current && <small> 当前</small>}</td>
              <td><Tag tone={v.status === 'FROZEN' ? 'info' : v.status === 'DRAFT' ? 'fail' : undefined}>{LABEL[v.status]}</Tag></td>
              <td className="mono muted">{v.code_hash.slice(0, 10)}</td>
              <td className="right">
                {v.status === 'VALIDATED' && <button className="button small" onClick={async () => {
                  try { await v4.freezeVersion(v.id); load() } catch (e) { notify((e as Error).message, true) }
                }}>冻结</button>}
                {v.status !== 'ARCHIVED' && <button className="button small ghost" onClick={async () => {
                  if (!await appConfirm('归档后该版本不能再发起新的运行（历史运行仍可追溯）')) return
                  try { await v4.archiveVersion(v.id); load() } catch (e) { notify((e as Error).message, true) }
                }}>归档</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  )
}
