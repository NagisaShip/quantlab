import { useCallback, useEffect, useMemo, useState } from 'react'
import { ago, v3, type FavoriteItem } from '../api/research'
import { Icon } from '../components/icons'
import { EmptyState, Loading, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'

const TYPE_ICON: Record<string, string> = {
  post: 'square', strategy: 'strategies', backtest: 'backtests', factor: 'factors',
  model: 'models', project: 'lab', link: 'docs',
}

export default function FavoritesPage({ onNavigate, notify }: PageProps) {
  const [items, setItems] = useState<FavoriteItem[] | null>(null)
  const [types, setTypes] = useState<Record<string, string>>({})
  const [filter, setFilter] = useState('')
  const load = useCallback(() => {
    v3.favorites().then((r) => { setItems(r.items); setTypes(r.types) }).catch((e) => notify((e as Error).message, true))
  }, [notify])
  useEffect(load, [load])
  const visible = useMemo(() => (items ?? []).filter((x) => !filter || x.object_type === filter), [items, filter])
  const counts = useMemo(() => (items ?? []).reduce<Record<string, number>>((out, x) => {
    out[x.object_type] = (out[x.object_type] || 0) + 1
    return out
  }, {}), [items])
  const remove = async (x: FavoriteItem) => {
    try { await v3.removeFavorite(x.object_type, x.object_id); notify('已取消收藏'); load() } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <div className="favorites-page">
      <div className="favorites-summary">
        <span className="favorites-total"><b>{items?.length ?? 0}</b><small>全部收藏</small></span>
        {Object.entries(types).filter(([k]) => counts[k]).map(([k, label]) => (
          <button key={k} type="button" className={`chip${filter === k ? ' on' : ''}`} onClick={() => setFilter(filter === k ? '' : k)}>{label} <small>{counts[k]}</small></button>
        ))}
        {filter && <button type="button" className="link-btn" onClick={() => setFilter('')}>清除筛选</button>}
      </div>
      <Panel title={filter ? `我的收藏 · ${types[filter] ?? filter}` : '我的收藏'} subtitle="帖子、策略、回测等内容统一保存在这里" flush>
        {!items ? <Loading label="正在读取收藏" /> : !visible.length ? (
          <EmptyState title={filter ? '这个分类还没有收藏' : '还没有收藏内容'}>在帖子、策略或回测页面点击收藏按钮，内容会汇总到这里。</EmptyState>
        ) : (
          <div className="favorites-list stagger">
            {visible.map((x) => (
              <article key={`${x.object_type}:${x.object_id}`} className="favorite-row">
                <span className={`favorite-icon t-${x.object_type}`}><Icon name={TYPE_ICON[x.object_type] || 'square'} size={18} /></span>
                <button type="button" className="favorite-main" onClick={() => x.link && onNavigate(x.link)}>
                  <span className="favorite-title"><b>{x.title || x.object_id}</b><Tag>{types[x.object_type] ?? x.object_type}</Tag></span>
                  <small>{Object.entries(x.meta ?? {}).filter(([, v]) => typeof v === 'string' || typeof v === 'number').slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(' · ') || '已收藏'} · {ago(x.created_at)}</small>
                </button>
                <div className="favorite-actions">
                  <button type="button" className="button small" onClick={() => x.link && onNavigate(x.link)}>打开</button>
                  <button type="button" className="link-btn danger" onClick={() => remove(x)}>取消收藏</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </Panel>
    </div>
  )
}
