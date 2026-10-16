/**
 * 平台上的每个功能都能用斜杠命令调用：把平台工具目录（/api/ai/tools）变成「平台功能」一组斜杠命令。
 * 输入 /<工具名> 说明 后照常发送，服务端会改写成「请直接调用这个工具」交给 AI（审批规则不变）。
 */
import { useEffect, useState } from 'react'
import { askAI } from '../../apiAskAI'
import type { SlashCommand } from './Composer'

let cache: SlashCommand[] | null = null
let loading: Promise<SlashCommand[]> | null = null

function load(): Promise<SlashCommand[]> {
  if (cache) return Promise.resolve(cache)
  loading ||= askAI.tools().then((c) => {
    const scene = Object.fromEntries(c.scenes.map((s) => [s.id, s.label]))
    cache = c.tools.map((t) => ({
      cmd: `/${t.name}`,
      group: `平台功能 · ${scene[t.scene] ?? t.scene}`,
      arg: '[说明]',
      help: `${t.title}：${t.description}${t.risk === 'confirm' ? '（需要你确认）' : ''}`,
    }))
    return cache
  }).catch(() => { loading = null; return [] })
  return loading
}

/** 返回平台功能命令（去掉与本页已有命令重名的）。 */
export function usePlatformCommands(existing: SlashCommand[]): SlashCommand[] {
  const [list, setList] = useState<SlashCommand[]>(cache ?? [])
  useEffect(() => { load().then(setList) }, [])
  const taken = new Set(existing.map((c) => c.cmd))
  return list.filter((c) => !taken.has(c.cmd))
}
