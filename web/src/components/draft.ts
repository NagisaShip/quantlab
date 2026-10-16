/**
 * 输入框草稿：输入了但还没发送的内容存在本机，切换页面、换对话、刷新后都还在；发送（清空）后删掉。
 * key 区分不同的输入框，例如 lab:<对话 id>、lab:new、home、ask。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

const PREFIX = 'qa.draft.'

function read(key: string): string {
  try { return localStorage.getItem(PREFIX + key) ?? '' } catch { return '' }
}

function write(key: string, value: string) {
  try {
    if (value.trim()) localStorage.setItem(PREFIX + key, value)
    else localStorage.removeItem(PREFIX + key)
  } catch { /* 私密模式或存储已满：只是不保存草稿 */ }
}

/** initial 优先（例如从别的页面带过来的想法），没有时读草稿。 */
export function useDraft(key: string, initial?: string): [string, (v: string) => void] {
  const [value, setValue] = useState(() => initial || read(key))
  const keyRef = useRef(key)
  const timer = useRef<number | undefined>(undefined)
  const latest = useRef(value)
  latest.current = value

  // 换了输入框（例如切到另一个对话）：先把上一个存好，再读新的
  useEffect(() => {
    if (keyRef.current === key) return
    window.clearTimeout(timer.current)
    write(keyRef.current, latest.current)
    keyRef.current = key
    setValue(read(key))
  }, [key])

  const set = useCallback((v: string) => {
    setValue(v)
    window.clearTimeout(timer.current)
    const k = keyRef.current
    // 清空（通常是刚发送）立即删除；输入中节流写入
    if (!v.trim()) write(k, '')
    else timer.current = window.setTimeout(() => write(k, v), 250)
  }, [])

  // 离开页面（组件卸载）或关掉标签页前，把最后一次输入写进去
  useEffect(() => {
    const flush = () => { window.clearTimeout(timer.current); write(keyRef.current, latest.current) }
    window.addEventListener('pagehide', flush)
    return () => { window.removeEventListener('pagehide', flush); flush() }
  }, [])

  return [value, set]
}
