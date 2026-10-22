import { createContext, useContext } from 'react'
import type { useLocalProject } from './useLocalProject'

export type LocalProjectState = ReturnType<typeof useLocalProject>

export const LocalContext = createContext<LocalProjectState | null>(null)

/** 在本地项目各面板里读取共享状态与动作。 */
export function useLocal(): LocalProjectState {
  const value = useContext(LocalContext)
  if (!value) throw new Error('本地项目面板必须在 LocalContext 内渲染')
  return value
}
