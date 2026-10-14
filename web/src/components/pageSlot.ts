import { createContext } from 'react'

/** 页头右侧的插槽：页面里的 <Heading actions> 会渲染到这里，标题和操作始终在同一行。 */
export const PageSlot = createContext<HTMLElement | null>(null)
