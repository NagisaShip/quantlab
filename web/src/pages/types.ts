/** 各页面共用的 props。App 统一注入导航、提示与忙碌遮罩。 */
export interface PageProps {
  onNavigate: (key: string) => void
  notify: (message: string, error?: boolean) => void
  setBusy: (b: { title: string; note?: string } | null) => void
}
