/** 未登录也可读取的公开分享预览。这个模块刻意不发送工作区头。 */
import { get } from './http'

export type PublicShareObjectType = 'group' | 'user' | 'post' | 'strategy'

export interface PublicShareMetric { label: string; value: string | number | null }

export interface PublicSharePayload {
  share: {
    token: string
    type: PublicShareObjectType
    title: string
    route: string
    created_at: number
    object: Record<string, unknown>
  }
  brand: { name: string; tagline: string }
}

export const publicApi = {
  share: (token: string) => get<PublicSharePayload>(`/api/public/shares/${encodeURIComponent(token)}`),
}
