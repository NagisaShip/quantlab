import { useMemo, useState, type ReactNode } from 'react'
import { copyText, downloadBlob, fmtValue, looksLikeTs, toCsv, type Fmt } from './protocol'

/**
 * 增强型表格：横向滚动、固定表头、点击表头排序、全文筛选、点击单元格复制、导出 CSV；
 * 超过一页的行分页显示（大表不一次性渲染几千行）。Markdown 表格与 ql:data_table 都用它。
 */

export interface Column { key: string; label?: string; format?: Fmt | string; digits?: number; align?: 'left' | 'right' }

const PAGE = 50

export function DataTable({ columns, rows, title, renderCell, note, maxHeight = 420, compact }: {
  columns: Column[]
  rows: Record<string, unknown>[]
  title?: ReactNode
  /** 自定义单元格（Markdown 表格用它渲染行内格式） */
  renderCell?: (row: Record<string, unknown>, col: Column) => ReactNode
  note?: ReactNode
  maxHeight?: number
  compact?: boolean
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null)
  const [q, setQ] = useState('')
  const [page, setPage] = useState(0)
  const [flash, setFlash] = useState('')

  const numeric = useMemo(() => {
    const out: Record<string, boolean> = {}
    for (const c of columns) {
      const vals = rows.slice(0, 50).map((r) => r[c.key]).filter((v) => v !== null && v !== undefined && v !== '')
      out[c.key] = vals.length > 0 && vals.every((v) => typeof v === 'number' || (typeof v === 'string' && /^[-+]?[\d,.]+%?$/.test(v.trim())))
    }
    return out
  }, [columns, rows])

  const text = (r: Record<string, unknown>, c: Column) => {
    const v = r[c.key]
    if (c.format) return fmtValue(v, c.format, c.digits)
    if (looksLikeTs(v) && /time|date|ts|时间|日期/i.test(c.key)) return fmtValue(v, 'datetime')
    return v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
  }

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    let out = needle ? rows.filter((r) => columns.some((c) => text(r, c).toLowerCase().includes(needle))) : rows
    if (sort) {
      const toNum = (v: unknown) => (typeof v === 'number' ? v : Number(String(v ?? '').replace(/[,%\s]/g, '')))
      out = [...out].sort((a, b) => {
        const x = a[sort.key], y = b[sort.key]
        if (numeric[sort.key]) {
          const nx = toNum(x), ny = toNum(y)
          return ((Number.isFinite(nx) ? nx : -Infinity) - (Number.isFinite(ny) ? ny : -Infinity)) * sort.dir
        }
        return String(x ?? '').localeCompare(String(y ?? ''), 'zh-CN') * sort.dir
      })
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, columns, q, sort, numeric])

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE))
  const cur = Math.min(page, pages - 1)
  const visible = filtered.length > PAGE ? filtered.slice(cur * PAGE, cur * PAGE + PAGE) : filtered

  const exportCsv = () => {
    const name = (typeof title === 'string' ? title : 'table').replace(/[\\/:*?"<>|\s]+/g, '_')
    downloadBlob(`${name || 'table'}.csv`, '﻿' + toCsv(columns.map((c) => c.label || c.key), filtered.map((r) => columns.map((c) => r[c.key]))), 'text/csv;charset=utf-8')
  }
  const copyCell = async (v: string) => {
    if (await copyText(v)) {
      setFlash(v.length > 24 ? v.slice(0, 24) + '…' : v)
      setTimeout(() => setFlash(''), 1200)
    }
  }
  const toggleSort = (key: string) => {
    setSort((s) => (!s || s.key !== key ? { key, dir: numeric[key] ? -1 : 1 } : s.dir === -1 ? { key, dir: 1 } : null))
    setPage(0)
  }

  const showTools = rows.length > 5 || columns.length > 4
  return (
    <div className={`qb-table${compact ? ' compact' : ''}`}>
      {(title || showTools) && (
        <div className="qb-table-head">
          {title && <b className="qb-title">{title}</b>}
          <span className="qb-muted">{filtered.length === rows.length ? `${rows.length} 行` : `${filtered.length} / ${rows.length} 行`}</span>
          <span className="qb-grow" />
          {showTools && (
            <input className="qb-filter" type="search" placeholder="筛选…" value={q} aria-label="筛选表格" onChange={(e) => { setQ(e.target.value); setPage(0) }} />
          )}
          <button type="button" className="qb-icon-btn" onClick={exportCsv}>导出 CSV</button>
        </div>
      )}
      <div className="qb-table-scroll" style={{ maxHeight }}>
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} className={numeric[c.key] || c.align === 'right' ? 'num' : ''} aria-sort={sort?.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}>
                  <button type="button" onClick={() => toggleSort(c.key)} title="点击排序">
                    {c.label ?? c.key}
                    <i className={`qb-sort${sort?.key === c.key ? (sort.dir === 1 ? ' asc' : ' desc') : ''}`} aria-hidden />
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((r, i) => (
              <tr key={i}>
                {columns.map((c) => {
                  const t = text(r, c)
                  return (
                    <td key={c.key} className={numeric[c.key] || c.align === 'right' ? 'num' : ''} onClick={() => copyCell(t)} title="点击复制">
                      {renderCell ? renderCell(r, c) : t}
                    </td>
                  )
                })}
              </tr>
            ))}
            {!visible.length && <tr><td colSpan={columns.length} className="qb-empty">没有匹配的行</td></tr>}
          </tbody>
        </table>
      </div>
      {(pages > 1 || note || flash) && (
        <div className="qb-table-foot">
          {note && <span className="qb-muted">{note}</span>}
          {flash && <span className="qb-flash anim-fade">已复制「{flash}」</span>}
          <span className="qb-grow" />
          {pages > 1 && (
            <span className="qb-pager">
              <button type="button" className="qb-icon-btn" disabled={cur === 0} onClick={() => setPage(cur - 1)}>上一页</button>
              <span className="mono">{cur + 1} / {pages}</span>
              <button type="button" className="qb-icon-btn" disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>下一页</button>
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/** ql:data_table 的数据：rows 可以是对象数组，也可以是二维数组 + columns 字符串列表。 */
export function tableFromBlock(data: Record<string, unknown>): { columns: Column[]; rows: Record<string, unknown>[] } {
  const rawRows = Array.isArray(data.rows) ? (data.rows as unknown[]) : []
  let columns: Column[] = Array.isArray(data.columns)
    ? (data.columns as unknown[]).map((c) => (typeof c === 'string' ? { key: c, label: c } : (c as Column)))
    : []
  const rows: Record<string, unknown>[] = rawRows.map((r) => {
    if (Array.isArray(r)) {
      const o: Record<string, unknown> = {}
      r.forEach((v, i) => { o[columns[i]?.key ?? `c${i}`] = v })
      return o
    }
    return (r && typeof r === 'object' ? r : { value: r }) as Record<string, unknown>
  })
  if (!columns.length) {
    const keys = new Set<string>()
    rows.slice(0, 100).forEach((r) => Object.keys(r).forEach((k) => keys.add(k)))
    columns = [...keys].map((k) => ({ key: k, label: k }))
  }
  return { columns, rows }
}
