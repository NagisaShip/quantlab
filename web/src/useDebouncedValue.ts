import { useEffect, useState } from 'react'

/** 输入类筛选统一走 350ms 防抖，避免每次按键都触发服务端查询。 */
export function useDebouncedValue<T>(value: T, delay = 350): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}
