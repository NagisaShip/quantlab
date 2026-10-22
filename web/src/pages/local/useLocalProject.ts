import { useCallback, useEffect, useState, type DragEvent } from 'react'
import { explainError, get, post, put, request, withWs, type ErrorExplanation } from '../../http'
import { chooseFolder, desktopKind, onDroppedFiles, openProjectWindow } from '../../native'
import type { PageProps } from '../types'
import { baseName, size } from './format'
import type {
  CacheInfo, CatalogCard, Entry, Env, EnvCandidate, Info, Job, LibraryModule, PreviewResult, Resource, Tasks,
} from './types'

export type ProjectLocation = 'project' | 'file'
export type ImportMode = 'reference' | 'copy'

/**
 * 本地项目面板的数据与动作。所有 /api/local/* 请求只在本地模式发起；
 * 账号模式仅在「研究执行环境」面板读取 /api/workflows/environment。
 */
export function useLocalProject({ me, notify, onNavigate, isLocal, tab, features }: PageProps & { isLocal: boolean; tab: string }) {
  const [info, setInfo] = useState<Info | null>(null)
  const [env, setEnv] = useState<Env | null>(null)
  const [envError, setEnvError] = useState('')
  const [envLoading, setEnvLoading] = useState(false)
  const [loading, setLoading] = useState(isLocal)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<ErrorExplanation | null>(null)
  // 真实资金能力：默认关闭，仅本地管理员可见开关；服务端仍会强制校验。
  const [realSpot, setRealSpot] = useState<boolean | null>(null)
  const [realSpotBusy, setRealSpotBusy] = useState(false)
  const realSpotAllowed = isLocal && !!me?.user.is_admin

  const [projectPath, setProjectPath] = useState('')
  const [filePath, setFilePath] = useState('')
  const [folder, setFolder] = useState('')
  const [entries, setEntries] = useState<Entry[]>([])

  const [python, setPython] = useState('')
  const [wheels, setWheels] = useState('')
  const [neural, setNeural] = useState(false)
  const [environments, setEnvironments] = useState<EnvCandidate[]>([])
  const [encoding, setEncoding] = useState('utf-8-sig')
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [reference, setReference] = useState('')
  const [job, setJob] = useState<Job | null>(null)

  const [tasks, setTasks] = useState<Tasks>({ runs: [], conversations: [] })
  const [resource, setResource] = useState<Resource | null>(null)
  const [cache, setCache] = useState<CacheInfo | null>(null)
  const [library, setLibrary] = useState<LibraryModule[]>([])
  const [cards, setCards] = useState<CatalogCard[]>([])
  const [module, setModule] = useState('')
  const [exportLink, setExportLink] = useState('')
  const [projectLink, setProjectLink] = useState('')

  const canEdit = isLocal || !!me?.user.is_admin
  const clearError = useCallback(() => setError(null), [])
  const clearPreview = useCallback(() => setPreview(null), [])

  /** 统一动作包装：忙碌提示 + 人话错误块 + toast。 */
  const run = useCallback(async (label: string, fn: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
    } catch (e) {
      const info = explainError(e)
      setError(info)
      notify(info.title, true)
    } finally {
      setBusy('')
    }
  }, [notify])

  const browse = useCallback(async (path: string) => {
    const v = await get<{ path: string; entries: Entry[]; total: number; truncated: boolean }>(
      `/api/local/files?path=${encodeURIComponent(path)}`,
    )
    setFolder(v.path)
    setEntries(v.entries ?? [])
    if (v.truncated) notify(`目录共 ${v.total} 项，这里显示前 ${v.entries.length} 项；请缩小目录范围`)
  }, [notify])

  const loadEnv = useCallback(async () => {
    setEnvLoading(true)
    setEnvError('')
    try {
      setEnv(await get<Env>('/api/workflows/environment'))
    } catch (e) {
      // 环境错误直接展示人话说明；技术细节不在这里堆 JSON。
      const info = explainError(e)
      setEnvError(info.message || info.title)
    } finally {
      setEnvLoading(false)
    }
  }, [])

  const loadLocal = useCallback(async () => {
    setLoading(true)
    try {
      const current = await get<Info>('/api/local/info')
      setInfo(current)
      setProjectPath((previous) => previous || current.root)
      const results = await Promise.allSettled([
        browse(current.root),
        get<CacheInfo>('/api/local/cache'),
        get<{ modules: LibraryModule[] }>('/api/local/library'),
        get<Resource>('/api/local/resources'),
        get<Tasks>('/api/local/tasks'),
        get<{ cards: CatalogCard[] }>('/api/workflows/catalog'),
      ])
      const [browseResult, cacheResult, libraryResult, resourceResult, tasksResult, cardsResult] = results
      if (cacheResult.status === 'fulfilled') setCache(cacheResult.value)
      if (libraryResult.status === 'fulfilled') setLibrary(libraryResult.value.modules ?? [])
      if (resourceResult.status === 'fulfilled') setResource(resourceResult.value)
      if (tasksResult.status === 'fulfilled') setTasks(tasksResult.value)
      if (cardsResult.status === 'fulfilled') {
        setCards((cardsResult.value.cards ?? []).filter((card) => !card.kind.startsWith('module.native_')))
      }
      const failure = [browseResult, cacheResult, libraryResult, resourceResult, tasksResult, cardsResult]
        .find((result) => result.status === 'rejected')
      if (failure && failure.status === 'rejected') {
        setError(explainError(failure.reason))
      }
    } catch (e) {
      setError(explainError(e))
    } finally {
      setLoading(false)
    }
  }, [browse])

  const wantsEnv = isLocal || tab === 'env'
  useEffect(() => { if (isLocal) void loadLocal() }, [isLocal, loadLocal])
  useEffect(() => { if (wantsEnv) void loadEnv() }, [wantsEnv, loadEnv])

  // 真实资金开关：以服务端工作区设置为准；读取失败时退回 App 已加载的开关值。
  const featureRealSpot = features?.['execution.real_spot']
  useEffect(() => {
    if (!realSpotAllowed) return
    let live = true
    get<{ enabled: Record<string, boolean> }>('/api/workspace/features')
      .then((value) => { if (live) setRealSpot(value.enabled['execution.real_spot'] === true) })
      .catch(() => { if (live) setRealSpot(featureRealSpot === true) })
    return () => { live = false }
  }, [realSpotAllowed, featureRealSpot])

  // 后台管理任务（导出项目 / 创建研究环境）轮询。
  const jobId = job?.id
  const jobState = job?.state
  useEffect(() => {
    if (!jobId || !jobState || !['running', 'queued'].includes(jobState)) return
    let live = true
    const timer = window.setInterval(() => {
      get<Job>(`/api/workflows/environment/jobs/${encodeURIComponent(jobId)}`).then((value) => {
        if (!live) return
        setJob(value)
        if (value.state === 'completed') {
          if (isLocal) void loadLocal()
          void loadEnv()
          if (typeof value.result?.name === 'string') {
            setExportLink(withWs(`/api/local/project/download/${encodeURIComponent(value.result.name)}`))
          }
        }
      }).catch((e) => { if (live) setError(explainError(e)) })
    }, 1500)
    return () => { live = false; window.clearInterval(timer) }
  }, [jobId, jobState, isLocal, loadLocal, loadEnv])

  // 资源与任务自动刷新（仅本地模式）。
  useEffect(() => {
    if (!isLocal) return
    let live = true
    const timer = window.setInterval(() => {
      Promise.all([get<Resource>('/api/local/resources'), get<Tasks>('/api/local/tasks')])
        .then(([nextResource, nextTasks]) => { if (live) { setResource(nextResource); setTasks(nextTasks) } })
        .catch(() => undefined)
    }, 5000)
    return () => { live = false; window.clearInterval(timer) }
  }, [isLocal])

  const refreshAll = () => run('刷新', async () => {
    await Promise.all([isLocal ? loadLocal() : Promise.resolve(), loadEnv()])
  })

  const refreshEnv = () => run('检查环境', loadEnv)

  const refreshRuntime = () => run('刷新资源', async () => {
    const [nextResource, nextTasks] = await Promise.all([
      get<Resource>('/api/local/resources'),
      get<Tasks>('/api/local/tasks'),
    ])
    setResource(nextResource)
    setTasks(nextTasks)
  })

  const pickFolder = (target: ProjectLocation) => run('选择文件夹', async () => {
    if (desktopKind() !== 'web') {
      const picked = await chooseFolder()
      if (picked[0]) {
        if (target === 'project') setProjectPath(picked[0])
        else setFilePath(picked[0])
      }
      return
    }
    notify('浏览器模式请粘贴本机文件夹路径；桌面版提供系统选择器')
  })

  const openProject = () => run('打开项目', async () => {
    const path = projectPath.trim()
    if (!path) return
    if (await openProjectWindow(path)) {
      notify('已在新窗口打开项目，当前任务继续运行')
      return
    }
    const value = await post<{ url: string }>('/api/local/project/open', { path })
    setProjectLink(value.url)
    notify('已在新窗口打开项目')
  })

  const exportProject = () => run('导出项目快照', async () => {
    setJob(await post<Job>('/api/local/project/export', {}))
    notify('已开始导出项目快照，完成后可下载 ZIP')
  })

  const saveCloseAction = (value: string) => run('保存关窗偏好', async () => {
    const saved = await post<{ close_action: string; last_workflow_id?: string }>('/api/local/preferences', { close_action: value })
    setInfo((current) => current ? { ...current, preferences: { ...current.preferences, ...saved } } : current)
    notify('已保存关窗行为')
  })

  /** 真实资金能力开关：仅在本地模式由管理员显式操作，服务端仍会按功能开关强制校验。 */
  const updateRealTrading = (next: boolean) => run(next ? '开启真实资金' : '关闭真实资金', async () => {
    setRealSpotBusy(true)
    try {
      const value = await put<{ enabled: Record<string, boolean> }>('/api/workspace/features', {
        features: { 'execution.real_spot': next },
      })
      setRealSpot(value.enabled['execution.real_spot'] === true)
      notify(next ? '真实资金能力已开启；真实下单仍需逐次确认' : '真实资金能力已关闭')
    } finally {
      setRealSpotBusy(false)
    }
  })

  const browsePath = () => run('浏览目录', async () => {
    const path = filePath.trim()
    if (path) await browse(path)
  })

  const browseInto = (path: string) => run('浏览目录', async () => { await browse(path) })

  const previewFile = () => run('预览数据', async () => {
    const path = filePath.trim()
    if (!path) return
    const value = await get<unknown>(
      `/api/local/files/preview?path=${encodeURIComponent(path)}&encoding=${encodeURIComponent(encoding)}`,
    )
    setPreview({ title: `数据预览 · ${baseName(path)}`, value })
  })

  const grantPermission = (mode: 'read' | 'write') => run(mode === 'read' ? '授权只读' : '授权可写', async () => {
    const path = filePath.trim()
    if (!path) return
    setInfo(await post<Info>('/api/local/permissions', { path, mode }))
    notify(mode === 'read' ? '已授权所选文件或目录只读' : '已授权目录可写')
  })

  const revokePermission = (path: string) => run('撤销授权', async () => {
    setInfo(await post<Info>('/api/local/permissions/revoke', { path }))
    notify('已撤销授权')
  })

  const importFromPath = (mode: ImportMode) => run(mode === 'reference' ? '引用文件' : '复制文件', async () => {
    const path = filePath.trim()
    if (!path) return
    const value = await post<{ name: string }>('/api/local/files/import', { path, mode })
    setReference(value.name)
    notify(`已${mode === 'reference' ? '引用' : '复制导入'}：${value.name}`)
  })

  const uploadFile = async (file: File) => {
    const value = await request<{ name: string }>(
      `/api/workflows/files?name=${encodeURIComponent(file.name)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file },
    )
    setReference(value.name)
    notify(`已流式导入 ${value.name}（${size(file.size)}）`)
  }

  const uploadFiles = (files: File[]) => run('复制文件', async () => {
    for (const file of files) await uploadFile(file)
  })

  const dropFiles = (event: DragEvent) => {
    event.preventDefault()
    const files = Array.from(event.dataTransfer.files)
    if (!files.length) return
    void run('接入文件', async () => {
      for (const file of files) {
        const localPath = (file as File & { pywebviewFullPath?: string }).pywebviewFullPath
        if (localPath) {
          setFilePath(localPath)
          notify('已识别本机文件路径；可先预览，再选择引用或复制')
        } else {
          await uploadFile(file)
        }
      }
    })
  }

  // Tauri 桌面壳拦截系统拖放后只给绝对路径（页面拿不到 File），按「引用本机文件」接住
  useEffect(() => onDroppedFiles((paths) => {
    if (!paths.length) return
    setFilePath(paths[0])
    notify(paths.length > 1
      ? `已识别 ${paths.length} 个本机文件路径，使用第一个：${baseName(paths[0])}`
      : '已识别本机文件路径；可先预览，再选择引用或复制')
  }), [notify])

  const relocateReference = () => run('重新定位文件', async () => {
    const path = filePath.trim()
    if (!reference || !path) return
    const value = await post<{ name: string; changed: boolean }>('/api/local/files/relocate', { name: reference, path })
    setPreview({ title: '重新定位结果', value })
    notify(value.changed ? '文件内容已变化，已记录新的指纹' : '文件未变化，指纹一致')
  })

  const discoverEnvironments = () => run('检测本机 Python', async () => {
    const value = await get<{ environments: EnvCandidate[] }>('/api/workflows/environment/discover')
    setEnvironments(value.environments ?? [])
    notify(value.environments?.length ? `发现 ${value.environments.length} 个 Python 解释器` : '没有发现可用的 Python 解释器')
  })

  const selectEnvironment = () => run('检查并切换环境', async () => {
    const value = python.trim()
    if (!value) return
    setEnv(await post<Env>('/api/workflows/environment/select', { python: value }))
    notify('已切换研究执行环境')
  })

  const repairEnvironment = () => run('创建独立研究环境', async () => {
    setJob(await post<Job>('/api/workflows/environment/repair', {
      python: python.trim(),
      wheelhouse: wheels.trim(),
      neural,
    }))
    notify('已开始创建独立研究环境；依赖检查通过后自动切换')
  })

  const cancelRun = (id: string) => run('取消任务', async () => {
    await post(`/api/runs/${encodeURIComponent(id)}/commands`, { command: 'cancel' })
    notify('已请求取消任务')
    setTasks(await get<Tasks>('/api/local/tasks'))
  })

  const clearCache = () => run('清理缓存', async () => {
    setCache(await post<CacheInfo>('/api/local/cache/clear', {}))
    notify('已清理可重建缓存，运行证据与历史保留')
  })

  const publishModule = () => run('发布固定版本', async () => {
    const split = module.lastIndexOf('@')
    if (split < 1) return
    await post('/api/local/library/publish', { kind: module.slice(0, split), version: module.slice(split + 1) })
    const value = await get<{ modules: LibraryModule[] }>('/api/local/library')
    setLibrary(value.modules ?? [])
    setModule('')
    notify('已发布固定版本到本机模块库')
  })

  const importModule = (id: string) => run('导入模块', async () => {
    await post('/api/local/library/import', { id })
    notify('固定版本已导入当前项目')
  })

  return {
    isLocal, tab, canEdit,
    info, env, envError, envLoading, loading, busy, error,
    realSpot, realSpotBusy, realSpotAllowed, updateRealTrading,
    projectPath, setProjectPath, filePath, setFilePath, folder, entries,
    python, setPython, wheels, setWheels, neural, setNeural, environments,
    encoding, setEncoding, preview, clearPreview, reference, setReference, job,
    tasks, resource, cache, library, cards, module, setModule, exportLink, projectLink,
    clearError, onNavigate, notify,
    refreshAll, refreshEnv, refreshRuntime,
    pickFolder, openProject, exportProject, saveCloseAction,
    browse, browsePath, browseInto, previewFile, grantPermission, revokePermission,
    importFromPath, uploadFiles, dropFiles, relocateReference,
    discoverEnvironments, selectEnvironment, repairEnvironment,
    cancelRun, clearCache, publishModule, importModule,
  }
}
