import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Archive, ArchiveRestore, ArrowDown, ArrowUpRight, Bot, CalendarDays, Camera, Check, CheckCircle2,
  ChevronDown, Copy, Download, ExternalLink, FileText, Heart, History, ImagePlus,
  LoaderCircle, NotebookPen, PenLine, RefreshCw, RotateCcw, Save, Search, Settings,
  ShieldCheck, Sparkles, Trash2, UserRoundSearch, WandSparkles, Wifi, WifiOff, X,
} from 'lucide-react'
import gatherLogo from './assets/gather-planet-logo.jpg'
import heartPlanet from './assets/heart-planet.png'
import { api, apiUrl, jsonBody } from './api'
import type {
  Bootstrap, ContentType, DeepSeekSettings, Draft, LifeCase, LifeCaseVariant, LocalProxySettings, ProductDeliverables,
  Review, SkillStatus, Source, Task, TaskDetail, WorkspaceProfile,
  TopicMonitor, TopicRadarItem, TopicRadarRecommendation, TopicRadarRun, TopicSearchResponse,
  PlatformSearchLink, PlatformBrowserStatus, XiaohongshuExtraction,
} from './types'

type Section = 'create' | 'radar' | 'works' | 'settings'
type Notice = { text: string; kind: 'ok' | 'error' } | null
type ProductMode = 'moments_ops' | 'xiaohongshu_ip' | 'douyin_emotion' | 'moments_life_case'
type RadarSeed = {
  topic: string
  sourceUrls: string[]
  searchQuery?: string
  platformSearches?: PlatformSearchLink[]
}

const productModes = [
  {
    key: 'moments_ops' as ProductMode,
    title: '朋友圈自动运营',
    short: '朋友圈运营',
    description: '活动预热、日常分享，把信任写进每一天。',
    badge: '日常陪伴',
    placeholder: '输入这轮朋友圈要围绕的主题，例如：让本地客户逐步了解我的相亲服务，但不要每天都在硬卖。',
    contentType: 'moments',
    platform: '微信朋友圈',
    action: '生成朋友圈运营计划',
    examples: [
      '周末脱单局，提前 7 天温柔预热', '聊聊第一次参加交友活动的紧张', '让同城朋友慢慢了解我的相亲服务',
      '活动结束后，一个让我印象很深的小细节', '今天遇到一位认真对待感情的女生', '为什么我们坚持先了解需求再推荐对象',
      '分享一次线下交友活动的准备过程', '圈子小的人，也值得被认真介绍', '相亲前最应该想清楚的一个问题',
      '这周收到的一句真实反馈', '红娘工作里看见的温柔瞬间', '给第一次来参加活动的人一点安心',
      '为什么真实资料比漂亮话更重要', '周末活动名额提醒，但不想写得像广告', '今天想聊聊慢慢认识一个人的意义',
    ],
    icon: CalendarDays,
    tone: 'green',
  },
  {
    key: 'xiaohongshu_ip' as ProductMode,
    title: '小红书 IP 文案',
    short: '小红书文案',
    description: '讲清你的态度，让同频的人先看见你。',
    badge: '同城吸引',
    placeholder: '输入一个主题，例如：第一次参加线下相亲，女生最容易忽略的三个判断细节。',
    contentType: 'xiaohongshu',
    platform: '小红书',
    action: '生成小红书发布稿',
    examples: [
      '第一次线下相亲，女生最容易忽略的 3 个细节', '圈子小的女生，怎么认识更合适的人', '做相亲平台后，我发现的一个真实现象',
      '长沙女生扩大异性圈，可以先做这 4 件事', '相亲资料里，哪些信息真的值得认真看', '为什么条件不错的人也会一直单身',
      '第一次参加线下交友活动是什么体验', '女生主动认识异性，并不等于降低标准', '相亲见面前，先聊清楚这几个问题',
      '红娘眼里更容易脱单的人有什么共同点', '圈子固定以后，怎样自然认识新朋友', '别急着看条件，相处舒服要看这些细节',
      '同城相亲活动怎么选，才不会浪费周末', '认真脱单的人，正在悄悄改变哪些习惯', '相亲后对方没有回复，应该怎么判断',
    ],
    icon: NotebookPen,
    tone: 'red',
  },
  {
    key: 'douyin_emotion' as ProductMode,
    title: '抖音情感文案',
    short: '抖音情感',
    description: '把恋爱里的小情绪，写成听得进去的口播。',
    badge: '情感共鸣',
    placeholder: '输入一个情感主题，例如：成年人的离开，往往不是一次争吵，而是很多次失望都没说出口。',
    contentType: 'short_video',
    platform: '抖音',
    action: '生成抖音情感口播',
    examples: [
      '成年人真正的失望，往往很安静', '真正的离开，是从不再解释开始', '被好好爱过的人，是什么样子',
      '真正想见你的人，不会一直说下次', '关系里最让人心寒的不是争吵', '你舍不得的，也许只是曾经的期待',
      '爱你的人会认真回应你的情绪', '停止内耗，是从不再猜他开始', '有些人错过以后，才懂得珍惜',
      '成年人的喜欢，藏在一次次行动里', '一段关系开始变淡，会有哪些信号', '好的感情，不需要你反复证明自己',
      '为什么越懂事的人越容易受委屈', '真正的告别，往往没有正式说再见', '遇见对的人以后，你会慢慢放松下来',
    ],
    icon: Heart,
    tone: 'purple',
  },
  {
    key: 'moments_life_case' as ProductMode,
    title: '生活案例朋友圈',
    short: '生活案例',
    description: '留住照片和小事，让真实的你更有温度。',
    badge: '真实瞬间',
    placeholder: '写下发生了什么、你当时注意到什么，以及哪些细节一定不能写错。',
    contentType: 'moments',
    platform: '微信朋友圈',
    action: '保存案例并生成三版',
    examples: [],
    icon: Camera,
    tone: 'amber',
  },
] as const

const terminalStatuses = new Set(['FINAL_READY', 'NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED', 'CANCELLED'])
const employeeStages = [
  { key: 'RESEARCHER', title: '研究员', description: '搜索资料与客户问题', icon: UserRoundSearch },
  { key: 'WRITER', title: '文案员工', description: '完成标题与正文初稿', icon: PenLine },
  { key: 'EDITOR', title: '主编', description: '核证据、改表达、定稿', icon: ShieldCheck },
] as const

const statusNames: Record<string, string> = {
  QUEUED: '等待处理',
  RUNNING: '正在创作',
  FINAL_READY: '已定稿',
  NEEDS_MODEL: '需要连接模型',
  NEEDS_ATTENTION: '需要处理',
  FAILED: '未完成',
  CANCELLED: '已取消',
}

function normalizeStage(stage: string) {
  if (['RESEARCHER', 'RESEARCHING'].includes(stage)) return 'RESEARCHER'
  if (['WRITER', 'WRITING', 'WRITING_V1', 'STRATEGIZING', 'REVISING'].includes(stage)) return 'WRITER'
  if (['EDITOR', 'EDITING', 'EXPRESSION_CHECK', 'HUMANIZING'].includes(stage)) return 'EDITOR'
  if (['FINAL', 'FINAL_READY'].includes(stage)) return 'FINAL'
  return 'RESEARCHER'
}

function formatDate(value?: string) {
  if (!value) return ''
  const raw = String(value).trim()
  const numeric = /^\d{10,13}$/.test(raw) ? Number(raw) : Number.NaN
  const date = Number.isFinite(numeric)
    ? new Date(raw.length === 10 ? numeric * 1000 : numeric)
    : new Date(raw)
  if (Number.isNaN(date.getTime())) return raw.slice(0, 24)
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
  } catch {
    return raw.slice(0, 24)
  }
}

function taskProductLabel(task: Task) {
  const mode = String(task.constraints?.product_mode || '')
  if (mode === 'moments_ops') return '朋友圈自动运营'
  if (mode === 'xiaohongshu_ip') return '小红书 IP 文案'
  if (mode === 'douyin_emotion') return '抖音情感文案'
  if (mode === 'moments_life_case') return '生活案例朋友圈'
  return task.platform
}

function localToday() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function momentsPostsFromBody(body: string, fallback: ProductDeliverables['moments_posts']) {
  const sections = body.split(/\n\s*---\s*\n/).map(item => item.trim()).filter(Boolean)
  if (sections.length !== fallback.length) return []
  return sections.map((section, index) => {
    const lines = section.split(/\r?\n/)
    const heading = lines.shift() || ''
    const metadata = (lines.shift() || '').split('｜')
    const rest = lines.join('\n').trim()
    const parts = rest.split(/\n\s*互动承接[：:]\s*/)
    const dayMatch = heading.match(/第\s*(\d+)\s*天/)
    const timeMatch = heading.match(/[·｜]\s*([^\n]+)$/)
    return {
      day: Number(dayMatch?.[1] || fallback[index].day || index + 1),
      time: timeMatch?.[1]?.trim() || fallback[index].time,
      role: metadata[0]?.trim() || fallback[index].role,
      purpose: metadata[1]?.trim() || fallback[index].purpose,
      content: parts[0]?.trim() || fallback[index].content,
      follow_up: parts.slice(1).join('\n').trim() || fallback[index].follow_up,
    }
  })
}

function App() {
  const [section, setSection] = useState<Section>('create')
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [workspaceId, setWorkspaceId] = useState('')
  const [tasks, setTasks] = useState<Task[]>([])
  const [lifeCases, setLifeCases] = useState<LifeCase[]>([])
  const [lifeCaseSeed, setLifeCaseSeed] = useState<LifeCase | null>(null)
  const [radarSeed, setRadarSeed] = useState<RadarSeed | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [notice, setNotice] = useState<Notice>(null)

  const notify = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    setNotice({ text, kind })
    window.setTimeout(() => setNotice(null), 3600)
  }, [])

  const loadBootstrap = useCallback(async () => {
    const data = await api<Bootstrap>('/api/bootstrap')
    setBootstrap(data)
    setWorkspaceId(current => current || data.workspaces[0]?.id || '')
    return data
  }, [])

  const loadTasks = useCallback(async (id: string) => {
    if (!id) return
    setTasks(await api<Task[]>(`/api/copy-tasks?workspace_id=${encodeURIComponent(id)}`))
  }, [])

  const loadLifeCases = useCallback(async (id: string) => {
    if (!id) return
    setLifeCases(await api<LifeCase[]>(`/api/life-cases?workspace_id=${encodeURIComponent(id)}&include_archived=true`))
  }, [])

  const loadDetail = useCallback(async (id: string) => {
    const value = await api<TaskDetail>(`/api/copy-tasks/${id}`)
    setDetail(value)
    return value
  }, [])

  useEffect(() => {
    loadBootstrap().catch(error => notify((error as Error).message, 'error'))
  }, [loadBootstrap, notify])

  useEffect(() => {
    if (!workspaceId) return
    setDetail(null)
    Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]).catch(error => notify((error as Error).message, 'error'))
  }, [workspaceId, loadLifeCases, loadTasks, notify])

  useEffect(() => {
    if (!detail || terminalStatuses.has(detail.task.status)) return
    const timer = window.setInterval(async () => {
      try {
        const next = await loadDetail(detail.task.id)
        if (terminalStatuses.has(next.task.status)) {
          await Promise.all([loadTasks(next.task.workspace_id), loadLifeCases(next.task.workspace_id)])
          await loadBootstrap()
        }
      } catch (error) {
        notify((error as Error).message, 'error')
      }
    }, 1400)
    return () => window.clearInterval(timer)
  }, [detail?.task.id, detail?.task.status, loadBootstrap, loadDetail, loadLifeCases, loadTasks, notify])

  const workspace = bootstrap?.workspaces.find(item => item.id === workspaceId)
  const activeProviderLabel = bootstrap?.model_status.active_label || '模型服务'
  const openTask = async (taskId: string, target: Section = 'works') => {
    setSection(target)
    await loadDetail(taskId)
  }

  if (!bootstrap) {
    return <div className="app-loading"><LoaderCircle className="spin" /><span>正在打开文案工作台</span></div>
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => setSection('create')} aria-label="回到开始创作">
          <span className="brand-logo"><img src={gatherLogo} alt="闪聚星球 GATHER PLANET" width="488" height="180" /></span>
          <span className="brand-descriptor">内容工作室</span>
        </button>
        <nav className="main-nav" aria-label="主导航">
          <NavButton active={section === 'create'} icon={WandSparkles} label="开始创作" onClick={() => setSection('create')} />
          <NavButton active={section === 'radar'} icon={Search} label="内容雷达" onClick={() => setSection('radar')} />
          <NavButton active={section === 'works'} icon={History} label="作品记录" onClick={() => setSection('works')} />
          <NavButton active={section === 'settings'} icon={Settings} label="设置" onClick={() => setSection('settings')} />
        </nav>
        <div className="top-actions">
          {bootstrap.workspaces.length > 1 ? (
            <select className="workspace-select" value={workspaceId} onChange={event => setWorkspaceId(event.target.value)}>
              {bootstrap.workspaces.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          ) : <span className="workspace-name">{workspace?.name === '我的品牌工作区' ? '闪聚创作空间' : workspace?.name || '闪聚创作空间'}</span>}
          <button
            className={`connection-pill ${bootstrap.model_status.usable ? 'online' : 'offline'}`}
            onClick={() => setSection('settings')}
          >
            {bootstrap.model_status.usable ? <Wifi size={15} /> : <WifiOff size={15} />}
            {bootstrap.model_status.usable ? `${activeProviderLabel} 已连接` : `连接${activeProviderLabel}`}
          </button>
        </div>
      </header>

      <main className="page-shell">
        <div hidden={section !== 'create'}>
          <CreatePage
            key={workspaceId}
            workspaceId={workspaceId}
            modelUsable={bootstrap.model_status.usable}
            lifeCaseSeed={lifeCaseSeed}
            radarSeed={radarSeed}
            detail={section === 'create' ? detail : null}
            onCreated={async value => {
              setDetail(value)
              await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)])
            }}
            onSeedConsumed={() => setLifeCaseSeed(null)}
            onRadarSeedConsumed={() => setRadarSeed(null)}
            onCasesChanged={() => loadLifeCases(workspaceId)}
            onOpenSettings={() => setSection('settings')}
            onRefresh={async () => { if (detail) await loadDetail(detail.task.id) }}
            notify={notify}
          />
        </div>
        {section === 'radar' && (
          <RadarPage
            workspaceId={workspaceId}
            onCreateFromTopic={seed => {
              setLifeCaseSeed(null)
              setRadarSeed(seed)
              setSection('create')
              notify(`已带入创作台：${seed.topic}`)
            }}
            onTaskCreated={async value => {
              setLifeCaseSeed(null)
              setRadarSeed(null)
              setDetail(value)
              await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)])
              setSection('create')
            }}
            notify={notify}
          />
        )}
        {section === 'works' && (
          <WorksPage
            tasks={tasks}
            lifeCases={lifeCases}
            detail={detail}
            contentTypes={bootstrap.content_types}
            onOpen={id => openTask(id)}
            onRefresh={async () => { if (detail) await loadDetail(detail.task.id); await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]) }}
            onCreated={async value => { setDetail(value); await Promise.all([loadTasks(workspaceId), loadLifeCases(workspaceId)]) }}
            onCasesChanged={() => loadLifeCases(workspaceId)}
            onDuplicateCase={value => { setDetail(null); setLifeCaseSeed(value); setSection('create') }}
            onSettings={() => setSection('settings')}
            notify={notify}
          />
        )}
        {section === 'settings' && (
          <SettingsPage
            workspaceId={workspaceId}
            modelStatus={bootstrap.model_status}
            skills={bootstrap.skills}
            onUpdated={loadBootstrap}
            notify={notify}
          />
        )}
      </main>

      <nav className="mobile-nav" aria-label="手机导航">
        <NavButton active={section === 'create'} icon={WandSparkles} label="创作" onClick={() => setSection('create')} />
        <NavButton active={section === 'radar'} icon={Search} label="雷达" onClick={() => setSection('radar')} />
        <NavButton active={section === 'works'} icon={History} label="作品" onClick={() => setSection('works')} />
        <NavButton active={section === 'settings'} icon={Settings} label="设置" onClick={() => setSection('settings')} />
      </nav>
      {notice && <div className={`toast ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.kind === 'ok' ? <Check size={16} /> : <AlertTriangle size={16} />}{notice.text}</div>}
    </div>
  )
}

function NavButton({ active, icon: Icon, label, onClick }: { active: boolean; icon: typeof Settings; label: string; onClick: () => void }) {
  return <button className={active ? 'active' : ''} aria-current={active ? 'page' : undefined} onClick={onClick}><Icon size={17} /><span>{label}</span></button>
}

function CreatePage({
  workspaceId, modelUsable, lifeCaseSeed, radarSeed, detail, onCreated, onSeedConsumed, onRadarSeedConsumed, onCasesChanged,
  onOpenSettings, onRefresh, notify,
}: {
  workspaceId: string
  modelUsable: boolean
  lifeCaseSeed: LifeCase | null
  radarSeed: RadarSeed | null
  detail: TaskDetail | null
  onCreated: (value: TaskDetail) => Promise<void>
  onSeedConsumed: () => void
  onRadarSeedConsumed: () => void
  onCasesChanged: () => Promise<void>
  onOpenSettings: () => void
  onRefresh: () => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [productMode, setProductMode] = useState<ProductMode>('moments_ops')
  const [inspirationOffsets, setInspirationOffsets] = useState<Partial<Record<ProductMode, number>>>({})
  const [briefs, setBriefs] = useState<Partial<Record<ProductMode, string>>>({})
  const brief = briefs[productMode] || ''
  const setBrief = (value: string) => setBriefs(current => ({ ...current, [productMode]: value }))
  const selectedMode = productModes.find(item => item.key === productMode) || productModes[0]
  const inspirationExamples = useMemo(() => {
    const examples = [...selectedMode.examples]
    if (!examples.length) return []
    const offset = (inspirationOffsets[productMode] || 0) % examples.length
    return Array.from({ length: Math.min(3, examples.length) }, (_, index) => examples[(offset + index) % examples.length])
  }, [inspirationOffsets, productMode, selectedMode.examples])
  const [webResearch, setWebResearch] = useState(true)
  const [goal, setGoal] = useState('')
  const [offer, setOffer] = useState('')
  const [sourceUrls, setSourceUrls] = useState('')
  const [radarContext, setRadarContext] = useState<Pick<RadarSeed, 'searchQuery' | 'platformSearches'> | null>(null)
  const [campaignDays, setCampaignDays] = useState('7')
  const [campaignGoal, setCampaignGoal] = useState('建立信任')
  const [xhsGoal, setXhsGoal] = useState('同步 IP 并获客')
  const [syncIp, setSyncIp] = useState(true)
  const [emotion, setEmotion] = useState('共鸣')
  const [duration, setDuration] = useState('60')
  const [lifeNote, setLifeNote] = useState('')
  const [lifeDate, setLifeDate] = useState(localToday())
  const [lifeSyncIp, setLifeSyncIp] = useState(true)
  const [lifeFiles, setLifeFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const composerRef = useRef<HTMLElement>(null)

  const selectMode = (mode: ProductMode) => {
    setProductMode(mode)
    if (window.matchMedia('(max-width: 640px)').matches) {
      window.requestAnimationFrame(() => composerRef.current?.scrollIntoView({
        block: 'start',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
      }))
    }
  }


  const refreshInspirations = () => {
    const total = selectedMode.examples.length
    if (!total) return
    setInspirationOffsets(current => ({
      ...current,
      [productMode]: ((current[productMode] || 0) + 3) % total,
    }))
  }

  const lifePreviews = useMemo(() => lifeFiles.map(file => ({ file, url: URL.createObjectURL(file) })), [lifeFiles])

  useEffect(() => () => lifePreviews.forEach(item => URL.revokeObjectURL(item.url)), [lifePreviews])

  useEffect(() => {
    if (!lifeCaseSeed) return
    let cancelled = false
    const seed = async () => {
      try {
        const files = await Promise.all(lifeCaseSeed.media
          .slice()
          .sort((a, b) => a.ordinal - b.ordinal)
          .map(async media => {
            const response = await fetch(apiUrl(media.media_url))
            if (!response.ok) throw new Error(`读取图片失败（${response.status}）`)
            const blob = await response.blob()
            return new File([blob], media.display_name, { type: media.mime_type || blob.type })
          }))
        if (cancelled) return
        setProductMode('moments_life_case')
        setLifeNote(lifeCaseSeed.note_text)
        setLifeDate(lifeCaseSeed.occurred_at?.slice(0, 10) || localToday())
        setLifeSyncIp(lifeCaseSeed.sync_ip)
        setLifeFiles(files)
        notify('案例已复制，请修改后保存为新案例')
      } catch (error) {
        notify((error as Error).message, 'error')
      } finally {
        if (!cancelled) onSeedConsumed()
      }
    }
    seed()
    return () => { cancelled = true }
  }, [lifeCaseSeed, notify, onSeedConsumed])

  useEffect(() => {
    if (!radarSeed) return
    setProductMode('xiaohongshu_ip')
    setBriefs(current => ({ ...current, xiaohongshu_ip: radarSeed.topic }))
    setSourceUrls(radarSeed.sourceUrls.join('\n'))
    setRadarContext({ searchQuery: radarSeed.searchQuery, platformSearches: radarSeed.platformSearches })
    onRadarSeedConsumed()
    window.requestAnimationFrame(() => composerRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }, [onRadarSeedConsumed, radarSeed])

  const addLifeFiles = (incoming: File[]) => {
    const acceptedExtensions = /\.(jpe?g|png|webp|gif|heic|heif)$/i
    const valid = incoming.filter(file => {
      if (!acceptedExtensions.test(file.name)) {
        notify(`${file.name} 不是支持的图片格式`, 'error')
        return false
      }
      if (file.size > 12 * 1024 * 1024) {
        notify(`${file.name} 超过 12MB`, 'error')
        return false
      }
      return true
    })
    const next = [...lifeFiles, ...valid]
    if (next.length > 9) {
      notify('一次最多上传 9 张图片', 'error')
      return
    }
    if (next.reduce((sum, file) => sum + file.size, 0) > 30 * 1024 * 1024) {
      notify('图片总大小不能超过 30MB', 'error')
      return
    }
    setLifeFiles(next)
  }

  const submitLifeCase = async () => {
    if (!lifeNote.trim()) {
      notify('请写下这件小事的经过或你当时的感受', 'error')
      return
    }
    if (lifeFiles.length === 0) {
      notify('请至少上传 1 张生活图片', 'error')
      return
    }
    setSubmitting(true)
    let saved = false
    try {
      const form = new FormData()
      form.append('workspace_id', workspaceId)
      form.append('note_text', lifeNote.trim())
      form.append('occurred_at', lifeDate)
      form.append('sync_ip', String(lifeSyncIp))
      lifeFiles.forEach(file => form.append('images', file, file.name))
      const lifeCase = await api<LifeCase>('/api/life-cases', { method: 'POST', body: form })
      saved = true
      setLifeNote('')
      setLifeFiles([])
      setLifeDate(localToday())
      await onCasesChanged()
      const value = await api<TaskDetail>(`/api/life-cases/${lifeCase.id}/generate?workspace_id=${encodeURIComponent(lifeCase.workspace_id)}`, { method: 'POST' })
      await onCreated(value)
      notify(modelUsable ? '案例已保存，三名数字员工已经开始工作' : '案例已保存，连接模型后可继续生成')
    } catch (error) {
      notify(saved ? `案例已保存；生成暂未开始：${(error as Error).message}` : (error as Error).message, 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const submit = async () => {
    if (productMode === 'moments_life_case') {
      await submitLifeCase()
      return
    }
    if (!modelUsable) {
      onOpenSettings()
      notify('请先在设置中测试并启用一个模型', 'error')
      return
    }
    if (brief.trim().length < 2) {
      notify('先告诉数字员工你想写什么', 'error')
      return
    }
    setSubmitting(true)
    try {
      const constraints: Record<string, unknown> = { product_mode: productMode }
      if (radarContext?.searchQuery || radarContext?.platformSearches?.length) {
        constraints.radar_origin = 'ip_daily'
        if (radarContext.searchQuery) constraints.radar_search_query = radarContext.searchQuery
        if (radarContext.platformSearches?.length) constraints.radar_platform_searches = radarContext.platformSearches
      }
      let defaultGoal = '获取精准咨询'
      if (productMode === 'moments_ops') {
        const days = Number(campaignDays)
        constraints.campaign_days = days
        constraints.campaign_goal = campaignGoal
        constraints.target_length = [Math.max(500, days * 120), Math.min(20000, days * 360)]
        defaultGoal = campaignGoal
      } else if (productMode === 'xiaohongshu_ip') {
        constraints.content_goal = xhsGoal
        constraints.sync_ip = syncIp
        constraints.target_length = [500, 1200]
        defaultGoal = xhsGoal
      } else {
        const seconds = Number(duration)
        constraints.emotion = emotion
        constraints.duration_seconds = seconds
        constraints.target_length = [Math.max(120, seconds * 4), Math.max(220, seconds * 7)]
        defaultGoal = '引发真实情绪共鸣和互动'
      }
      const value = await api<TaskDetail>('/api/copy-tasks', jsonBody('POST', {
        workspace_id: workspaceId,
        content_type: selectedMode.contentType,
        platform: selectedMode.platform,
        topic: brief.trim(),
        goal: goal.trim() || defaultGoal,
        offer: offer.trim(),
        constraints,
        source_urls: sourceUrls.split(/\r?\n/).map(item => item.trim()).filter(Boolean),
        web_research: webResearch,
      }))
      await onCreated(value)
      notify('三名数字员工已经开始工作')
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="create-page">
      <section className="hero">
        <div className="hero-copy">
          <span className="studio-label"><span />GATHER PLANET · 闪聚星球</span>
          <h1>写点心动，<br /><em>让缘分发生。</em><Sparkles aria-hidden="true" /></h1>
          <p>把相遇的故事、恋爱的感悟、活动的小美好，<br className="desktop-break" />写成让人想靠近的内容。你的灵感，交给闪聚。</p>
          <div className="hero-topics"><span><Heart />情感共鸣</span><i /><span>同城相遇</span><i /><span>真实故事</span></div>
        </div>
        <div className="hero-scene" aria-hidden="true">
          <div className="scene-art"><img src={heartPlanet} alt="" width="1254" height="1254" fetchPriority="high" /></div>
          <span className="scene-note note-top"><Sparkles />今天，灵感也心动了</span>
          <span className="scene-note note-bottom"><Heart />让同频的人，看见你<span>♡</span></span>
          <span className="scene-spark spark-one">✳</span><span className="scene-spark spark-two">✧</span>
        </div>
      </section>

      <div className="creation-heading"><div><span className="section-number">01</span><h2>选一个心动入口</h2><p>今天，想怎样被看见？</p></div><span className="section-aside">让好内容，成为相遇的开始 <ArrowDown /></span></div>
      <section className="product-switcher" aria-label="内容工作台">
        {productModes.map(mode => {
          const Icon = mode.icon
          return (
            <button
              key={mode.key}
              className={`${productMode === mode.key ? 'active' : ''} ${mode.tone}`}
              onClick={() => selectMode(mode.key)}
              aria-pressed={productMode === mode.key}
            >
              <span className="mode-icon"><Icon /></span>
              <span className="mode-badge">{mode.badge}</span>
              <div><strong>{mode.title}</strong><small>{mode.description}</small></div>
              <span className="mode-state">{productMode === mode.key ? <><CheckCircle2 />已选择</> : <>开始创作<ArrowUpRight /></>}</span>
            </button>
          )
        })}
      </section>

      <div className="studio-workspace">
      <section className={`composer-card mode-${selectedMode.tone}`} ref={composerRef}>
        <div className="composer-mode-title"><span className="section-number">02</span><div><strong>{productMode === 'moments_life_case' ? '把生活里的小美好，留在这里' : '说说你今天想分享什么'}</strong><span>{selectedMode.title} · 写下想法，剩下的我们来</span></div><span className="composer-step"><PenLine />灵感起笔</span></div>
        {productMode === 'moments_life_case' ? (
          <div className="life-case-form">
            <label
              className="life-upload"
              onDragOver={event => event.preventDefault()}
              onDrop={event => {
                event.preventDefault()
                addLifeFiles(Array.from(event.dataTransfer.files))
              }}
            >
              <input
                type="file"
                multiple
                accept=".jpg,.jpeg,.png,.webp,.gif,.heic,.heif,image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif"
                onChange={event => {
                  addLifeFiles(Array.from(event.target.files || []))
                  event.target.value = ''
                }}
              />
              <span><ImagePlus /></span>
              <div><strong>上传生活图片</strong><small>点击选择或拖进来，1–9 张</small></div>
              <em>JPG、PNG、WebP、GIF、HEIC</em>
            </label>
            {lifePreviews.length > 0 && (
              <div className="life-preview-grid" aria-label="已选择的生活图片">
                {lifePreviews.map((item, index) => {
                  const heic = /\.hei[cf]$/i.test(item.file.name)
                  return (
                    <figure key={`${item.file.name}-${item.file.lastModified}-${index}`}>
                      {heic ? <div className="heic-placeholder"><Camera /><span>HEIC</span><small>{item.file.name}</small></div> : <img src={item.url} alt={`生活图片 ${index + 1}`} />}
                      <figcaption>{index + 1}</figcaption>
                      <button type="button" aria-label={`移除第 ${index + 1} 张图片`} onClick={() => setLifeFiles(files => files.filter((_, fileIndex) => fileIndex !== index))}><X /></button>
                    </figure>
                  )
                })}
              </div>
            )}
            <label className="life-note-field">
              <span>这件小事里发生了什么</span>
              <textarea value={lifeNote} onChange={event => setLifeNote(event.target.value)} placeholder={selectedMode.placeholder} maxLength={5000} />
              <small>{lifeNote.length}/5000 · 写清真实细节，数字员工不会替你编故事</small>
            </label>
            <div className="life-case-controls">
              <label><span>发生日期</span><input type="date" value={lifeDate} onChange={event => setLifeDate(event.target.value)} /></label>
              <label className="compact-toggle"><input type="checkbox" checked={lifeSyncIp} onChange={event => setLifeSyncIp(event.target.checked)} /><span className="switch" /><span>同步我的 IP 语气</span></label>
            </div>
          </div>
        ) : <>
          <div className="brief-field">
            <textarea
              className="brief-input"
              aria-label="创作主题"
              value={brief}
              onChange={event => setBrief(event.target.value)}
              placeholder={selectedMode.placeholder}
              maxLength={5000}
            />
            <div className="brief-helper">
              <div className="inspiration-chips" aria-label="灵感快捷填入">
                <span><Sparkles />没灵感？试试</span>
                <button className="inspiration-refresh" type="button" onClick={refreshInspirations} aria-label="换一批创作灵感"><RefreshCw />换一批</button>
                {inspirationExamples.map(example => (
                  <button type="button" key={example} onClick={() => setBrief(example)}>{example}</button>
                ))}
              </div>
              <div className="brief-count">
                {brief && <button type="button" onClick={() => setBrief('')} aria-label="清空主题"><X />清空</button>}
                <span>{brief.length}/5000</span>
              </div>
            </div>
          </div>
          <div className="mode-controls">
            {productMode === 'moments_ops' && <>
              <label><span>运营周期</span><select value={campaignDays} onChange={event => setCampaignDays(event.target.value)}><option value="3">连续 3 天</option><option value="7">连续 7 天</option><option value="14">连续 14 天</option></select></label>
              <label><span>这轮目标</span><select value={campaignGoal} onChange={event => setCampaignGoal(event.target.value)}><option>建立信任</option><option>日常活跃</option><option>活动预热</option><option>自然成交</option></select></label>
            </>}
            {productMode === 'xiaohongshu_ip' && <>
              <label><span>内容目标</span><select value={xhsGoal} onChange={event => setXhsGoal(event.target.value)}><option>同步 IP 并获客</option><option>经验分享</option><option>产品种草</option><option>建立专业感</option></select></label>
              <label className="compact-toggle"><input type="checkbox" checked={syncIp} onChange={event => setSyncIp(event.target.checked)} /><span className="switch" /><span>同步我的 IP 信息</span></label>
            </>}
            {productMode === 'douyin_emotion' && <>
              <label><span>情绪方向</span><select value={emotion} onChange={event => setEmotion(event.target.value)}><option>共鸣</option><option>治愈</option><option>遗憾</option><option>成长</option><option>关系清醒</option></select></label>
              <label><span>口播时长</span><select value={duration} onChange={event => setDuration(event.target.value)}><option value="30">约 30 秒</option><option value="60">约 60 秒</option><option value="90">约 90 秒</option></select></label>
            </>}
            <label className="search-toggle"><input type="checkbox" checked={webResearch} onChange={event => setWebResearch(event.target.checked)} /><span className="switch" /><span><Search size={15} />联网搜索</span></label>
          </div>
          <details className="more-options">
            <summary><ChevronDown size={16} />更多要求（选填）</summary>
            <div className="option-grid">
              <label><span>希望读者最后做什么</span><input value={goal} onChange={event => setGoal(event.target.value)} placeholder="不填则按当前模式自动决定" /></label>
              <label><span>承接的产品或服务</span><input value={offer} onChange={event => setOffer(event.target.value)} placeholder="没有可以留空" /></label>
              <label className="wide"><span>参考网址（每行一个）</span><textarea value={sourceUrls} onChange={event => setSourceUrls(event.target.value)} placeholder="https://..." /></label>
            </div>
          </details>
        </>}
        <div className="composer-footer">
          <span><ShieldCheck />{productMode === 'moments_life_case' ? '保留真实细节，生成三种表达供你挑选' : productMode === 'xiaohongshu_ip' && syncIp ? '融合你的 IP 信息，保留你的表达习惯' : webResearch ? '参考真实来源，写出有依据的内容' : '围绕你的主题与资料，认真写好每一句'}</span>
          <button className="primary-button" disabled={submitting} onClick={submit}>
            {submitting ? <LoaderCircle className="spin" /> : <Sparkles />}
            {submitting ? '正在交给数字员工' : selectedMode.action}
          </button>
        </div>
      </section>
      <aside className="studio-companion">
        <div className="companion-heading"><span><Sparkles /></span><small>你的创作搭子</small><h3>有想法就好，<br />我们帮你写出来。</h3></div>
        <div className="companion-steps">
          <div><span><Search /></span><div><strong>找一点灵感</strong><small>整理主题、参考与真实来源</small></div><i>01</i></div>
          <div><span><PenLine /></span><div><strong>写一点心动</strong><small>把想说的话，写得自然动人</small></div><i>02</i></div>
          <div><span><CheckCircle2 /></span><div><strong>再认真读一遍</strong><small>核对细节，让表达更像你</small></div><i>03</i></div>
        </div>
        <div className="companion-tip"><Heart /><p>好的相遇，从真实开始。<br />多写一点具体的小事，会更打动人。</p></div>
        <span className="companion-signature">MADE WITH LOVE, BY GATHER PLANET</span>
      </aside>
      </div>

      <div className="studio-signoff"><span />用真实表达，连接同频的人。<Heart /><span /></div>

      {detail && (
        <TaskWorkspace
          detail={detail}
          onRefresh={onRefresh}
          onSettings={onOpenSettings}
          notify={notify}
        />
      )}
    </div>
  )
}

function TaskWorkspace({ detail, onRefresh, onSettings, notify, showSourceCase = true }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
  showSourceCase?: boolean
}) {
  const current = normalizeStage(detail.task.stage)
  const currentIndex = current === 'FINAL' ? 3 : employeeStages.findIndex(item => item.key === current)
  const isProblem = ['NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED'].includes(detail.task.status)
  const lifeMode = detail.task.constraints?.product_mode === 'moments_life_case'
  const lifeStageDescriptions: Record<string, string> = {
    RESEARCHER: '识别图片与真实细节',
    WRITER: '写出三种朋友圈角度',
    EDITOR: '核事实、去 AI 味、定稿',
  }
  const lastMessages = new Map<string, string>()
  for (const event of detail.events) lastMessages.set(normalizeStage(event.stage), event.message)

  return (
    <section className="task-workspace">
      <div className="task-heading">
        <div><span className="eyebrow">当前任务</span><h2>{detail.task.topic}</h2></div>
        <span className={`status-badge ${detail.task.status.toLowerCase()}`}>{statusNames[detail.task.status] || detail.task.status}</span>
      </div>
      {showSourceCase && detail.source_case && <LifeCaseMaterial caseItem={detail.source_case} compact />}
      <div className="employee-flow">
        {employeeStages.map((employee, index) => {
          const done = detail.task.status === 'FINAL_READY' || currentIndex > index
          const active = currentIndex === index && detail.task.status !== 'FINAL_READY'
          const problem = active && isProblem
          return (
            <div className={`employee-card ${done ? 'done' : ''} ${active ? 'active' : ''} ${problem ? 'problem' : ''}`} key={employee.key}>
              <span className="employee-icon">{done ? <CheckCircle2 /> : problem ? <AlertTriangle /> : active ? <LoaderCircle className="spin" /> : <employee.icon />}</span>
              <div><strong>{employee.title}</strong><small>{lastMessages.get(employee.key) || (lifeMode ? lifeStageDescriptions[employee.key] : employee.description)}</small></div>
              {index < 2 && <i />}
            </div>
          )
        })}
      </div>
      {detail.task.revision_round > 0 && <div className="revision-note"><RotateCcw size={15} />主编已退回修改 {detail.task.revision_round} 次，本轮不会推翻整篇。</div>}
      {isProblem && <ProblemPanel detail={detail} onRefresh={onRefresh} onSettings={onSettings} notify={notify} />}
      <ResultEditor detail={detail} onRefresh={onRefresh} notify={notify} />
    </section>
  )
}

function ProblemPanel({ detail, onRefresh, onSettings, notify }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const act = async (path: string) => {
    try {
      await api(path, { method: 'POST' })
      await onRefresh()
      notify('任务已继续')
    } catch (error) { notify((error as Error).message, 'error') }
  }
  return (
    <div className="problem-panel">
      <AlertTriangle />
      <div><strong>{detail.task.status === 'NEEDS_MODEL' ? '模型连接需要处理' : '这一步需要你的选择'}</strong><p>{detail.task.last_error || '任务暂时没有完成。'}</p></div>
      <div className="problem-actions">
        {detail.task.status === 'NEEDS_MODEL' && <button onClick={onSettings}>去连接</button>}
        <button onClick={() => act(`/api/copy-tasks/${detail.task.id}/retry`)}><RefreshCw />重试</button>
        {normalizeStage(detail.task.stage) === 'RESEARCHER' && detail.task.constraints?.product_mode !== 'moments_life_case' && <button onClick={() => act(`/api/copy-tasks/${detail.task.id}/continue-without-search`)}>不联网继续</button>}
      </div>
    </div>
  )
}

function ResultEditor({ detail, onRefresh, notify }: {
  detail: TaskDetail
  onRefresh: () => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const finalDraft = detail.drafts.find(item => item.is_final)
  const draft = finalDraft || detail.drafts[0]
  const review = detail.reviews[0]
  const productLabel = taskProductLabel(detail.task)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [saving, setSaving] = useState(false)
  const [adoptingAngle, setAdoptingAngle] = useState('')
  const activeEmployee = employeeStages.find(item => item.key === normalizeStage(detail.task.stage))

  useEffect(() => {
    setTitle(draft?.package.title || '')
    setBody(draft?.package.body || draft?.body_text || '')
  }, [draft?.id])

  if (!draft) {
    if (!terminalStatuses.has(detail.task.status)) return (
      <div className="waiting-result">
        <span className="waiting-orb"><LoaderCircle className="spin" /></span>
        <div>
          <strong>{detail.task.status === 'QUEUED' ? '正在准备创作' : `${activeEmployee?.title || '数字员工'}正在工作`}</strong>
          <span>你可以先去查看其他作品，完成后定稿会自动显示在这里。</span>
        </div>
      </div>
    )
    return null
  }

  const copy = async () => {
    try {
      if (detail.task.constraints?.product_mode === 'moments_life_case') {
        await navigator.clipboard.writeText(body)
        notify('朋友圈文案已复制')
        return
      }
      const tags = (draft.package.tags || []).map(item => item.startsWith('#') ? item : `#${item}`).join(' ')
      await navigator.clipboard.writeText(`${title}\n\n${body}${tags ? `\n\n${tags}` : ''}`)
      notify(detail.task.content_type === 'xiaohongshu' ? '小红书发布版已复制' : '文案已复制')
    } catch { notify('复制失败，请手动选择正文', 'error') }
  }
  const save = async () => {
    setSaving(true)
    try {
      const mode = String(detail.task.constraints?.product_mode || '')
      const deliverables = draft.package.deliverables
        ? JSON.parse(JSON.stringify(draft.package.deliverables)) as ProductDeliverables
        : undefined
      if (deliverables && mode === 'xiaohongshu_ip') {
        deliverables.xiaohongshu_publish = { ...deliverables.xiaohongshu_publish, title, body, tags: draft.package.tags || [] }
      }
      if (deliverables && mode === 'douyin_emotion') {
        deliverables.douyin_script = { ...deliverables.douyin_script, script: body }
      }
      if (deliverables && mode === 'moments_ops') {
        deliverables.moments_posts = momentsPostsFromBody(body, deliverables.moments_posts)
      }
      await api(`/api/drafts/${draft.id}/edit`, jsonBody('POST', {
        title, body, cta: draft.package.cta || '', alternative_titles: draft.package.alternative_titles || [],
        platform_variants: draft.package.platform_variants || {}, tags: draft.package.tags || [],
        deliverables: deliverables || {}, claims: draft.package.claims || [], image_briefs: [],
        conversion_structure: draft.package.conversion_structure || {},
      }))
      await onRefresh()
      notify('修改已保存为新版本')
    } catch (error) { notify((error as Error).message, 'error') } finally { setSaving(false) }
  }

  const adoptVariant = async (variant: LifeCaseVariant) => {
    if (!draft.package.deliverables) return
    setAdoptingAngle(variant.angle)
    try {
      const deliverables = JSON.parse(JSON.stringify(draft.package.deliverables)) as ProductDeliverables
      deliverables.life_case_variants = (deliverables.life_case_variants || []).map(item => ({
        ...item,
        recommended: item.angle === variant.angle,
      }))
      await api(`/api/drafts/${draft.id}/edit`, jsonBody('POST', {
        title: draft.package.title || '生活案例朋友圈', body: variant.body, cta: '',
        alternative_titles: draft.package.alternative_titles || [], platform_variants: {},
        tags: [], deliverables, claims: draft.package.claims || [], image_briefs: [],
        conversion_structure: draft.package.conversion_structure || {},
      }))
      await onRefresh()
      notify(`已采用“${variant.label}”，并保存为新的最终稿`)
    } catch (error) {
      notify((error as Error).message, 'error')
    } finally {
      setAdoptingAngle('')
    }
  }

  return (
    <div className="result-card">
      <div className="result-toolbar">
        <div><span className="eyebrow">{draft.is_final ? '主编定稿' : `当前版本 V${draft.version}`}</span><strong>{productLabel} · {detail.task.web_research ? '已联网研究' : '未联网'}</strong></div>
        <div>
          <button onClick={copy}><Copy />{detail.task.content_type === 'xiaohongshu' ? '复制发布版' : '复制'}</button>
          <button onClick={() => window.open(`/api/drafts/${draft.id}/export?format=md`, '_blank')}><Download />导出</button>
          <button onClick={save} disabled={saving}><Save />{saving ? '保存中' : '保存新版'}</button>
        </div>
      </div>
      <ProductDeliverablesPanel task={detail.task} deliverables={draft.package.deliverables} onAdoptVariant={adoptVariant} adoptingAngle={adoptingAngle} notify={notify} />
      {draft.package.deliverables && <div className="editor-label"><span>{detail.task.constraints?.product_mode === 'moments_life_case' ? '编辑当前采用稿' : '编辑总稿'}</span><small>修改后可保存为新版本，不覆盖原稿</small></div>}
      {detail.task.constraints?.product_mode !== 'moments_life_case' && <input className="title-editor" value={title} onChange={event => setTitle(event.target.value)} />}
      <textarea className={`body-editor ${draft.package.deliverables ? 'structured' : ''}`} value={body} onChange={event => setBody(event.target.value)} />
      <div className="delivery-summary">
        <span>{productLabel}</span>
        <p>{detail.task.constraints?.product_mode === 'moments_life_case' ? '不强加标题、标签或营销话术' : draft.package.cta ? `行动承接：${draft.package.cta}` : '主编已完成平台适配'}</p>
        {(draft.package.tags || []).length > 0 && <div>{draft.package.tags?.map(item => <i key={item}>#{item.replace(/^#/, '')}</i>)}</div>}
      </div>
      {draft.package.alternative_titles?.length > 0 && (
        <div className="title-options"><span>备选标题</span>{draft.package.alternative_titles.map(item => <button key={item} onClick={() => setTitle(item)}>{item}</button>)}</div>
      )}
      <div className="result-details">
        <SourceDetails sources={detail.sources} research={detail.research?.packet} />
        <ReviewDetails review={review} />
      </div>
    </div>
  )
}

function ProductDeliverablesPanel({ task, deliverables, onAdoptVariant, adoptingAngle, notify }: {
  task: Task
  deliverables?: ProductDeliverables
  onAdoptVariant: (variant: LifeCaseVariant) => Promise<void>
  adoptingAngle: string
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const mode = String(task.constraints?.product_mode || '')
  if (!deliverables) return null

  const copyText = async (text: string, success: string) => {
    try {
      await navigator.clipboard.writeText(text)
      notify(success)
    } catch { notify('复制失败，请手动选择内容', 'error') }
  }

  if (mode === 'moments_life_case' && deliverables.life_case_variants?.length) {
    const labels: Record<string, string> = { daily: '真实日常', reflection: '有感而发', soft_business: '轻度业务启发' }
    const ordered = [...deliverables.life_case_variants].sort((a, b) => Number(b.recommended) - Number(a.recommended))
    return (
      <section className="product-deliverables life-variants-deliverable">
        <div className="deliverable-heading">
          <div><span className="eyebrow">主编已去 AI 味</span><h3>三种角度，都可以直接发</h3></div>
          <small>推荐版排在最前</small>
        </div>
        <div className="life-variant-grid">
          {ordered.map(variant => (
            <article className={`life-variant-card ${variant.recommended ? 'recommended' : ''}`} key={variant.angle}>
              <header>
                <div><strong>{variant.label || labels[variant.angle] || '朋友圈版本'}</strong>{variant.recommended && <span><Sparkles />主编推荐</span>}</div>
                {variant.rationale && <small>{variant.rationale}</small>}
              </header>
              <p>{variant.body}</p>
              <footer>
                <button onClick={() => copyText(variant.body, `“${variant.label || labels[variant.angle]}”已复制`)}><Copy />复制这版</button>
                <button className="adopt-button" disabled={variant.recommended || Boolean(adoptingAngle)} onClick={() => onAdoptVariant(variant)}>
                  {adoptingAngle === variant.angle ? <LoaderCircle className="spin" /> : <CheckCircle2 />}
                  {variant.recommended ? '当前采用' : '采用这版'}
                </button>
              </footer>
            </article>
          ))}
        </div>
      </section>
    )
  }

  if (mode === 'moments_ops' && deliverables.moments_posts?.length) {
    return (
      <section className="product-deliverables moments-deliverables">
        <div className="deliverable-heading"><div><span className="eyebrow">可直接发布</span><h3>朋友圈每日内容</h3></div><small>{deliverables.moments_posts.length} 天</small></div>
        <div className="moments-post-grid">
          {deliverables.moments_posts.map(post => (
            <article className="moments-post-card" key={`${post.day}-${post.time}`}>
              <header><strong>第 {post.day} 天</strong><span>{post.time || '时间自定'}</span></header>
              <div className="post-purpose"><i>{post.role || '日常内容'}</i><span>{post.purpose}</span></div>
              <p>{post.content}</p>
              {post.follow_up && <footer><strong>互动承接</strong><span>{post.follow_up}</span></footer>}
              <button onClick={() => copyText(post.content + (post.follow_up ? `\n\n互动承接：${post.follow_up}` : ''), `第 ${post.day} 天内容已复制`)}><Copy />复制这条</button>
            </article>
          ))}
        </div>
      </section>
    )
  }

  if (mode === 'xiaohongshu_ip' && deliverables.xiaohongshu_publish?.body) {
    const publish = deliverables.xiaohongshu_publish
    const tagLine = (publish.tags || []).map(item => `#${item.replace(/^#/, '')}`).join(' ')
    const fullText = `${publish.title}\n\n${publish.body}${tagLine ? `\n\n${tagLine}` : ''}`
    return (
      <section className="product-deliverables publish-deliverable">
        <div className="deliverable-heading"><div><span className="eyebrow">小红书发布版</span><h3>{publish.title}</h3></div><button onClick={() => copyText(fullText, '小红书发布版已复制')}><Copy />一键复制</button></div>
        <div className="publish-preview"><p>{publish.body}</p><div>{publish.tags.map(tag => <i key={tag}>#{tag.replace(/^#/, '')}</i>)}</div></div>
      </section>
    )
  }

  if (mode === 'douyin_emotion' && deliverables.douyin_script?.script) {
    const script = deliverables.douyin_script
    return (
      <section className="product-deliverables douyin-deliverable">
        <div className="deliverable-heading"><div><span className="eyebrow">抖音口播结构</span><h3>情绪推进已经拆好</h3></div><button onClick={() => copyText(script.script, '抖音口播稿已复制')}><Copy />复制口播稿</button></div>
        <div className="douyin-hook"><span>前 3 秒</span><strong>{script.hook}</strong></div>
        <div className="emotion-beats">{script.emotion_beats.map((beat, index) => <span key={`${index}-${beat}`}><i>{index + 1}</i>{beat}</span>)}</div>
        {script.ending && <div className="douyin-ending"><span>自然收束</span><p>{script.ending}</p></div>}
      </section>
    )
  }

  return null
}

function LifeCaseMaterial({ caseItem, compact = false }: { caseItem: LifeCase; compact?: boolean }) {
  const sortedMedia = [...caseItem.media].sort((a, b) => a.ordinal - b.ordinal)
  return (
    <section className={`life-case-material ${compact ? 'compact' : ''}`}>
      <header>
        <div><span className="eyebrow">本次生活素材</span><strong>{caseItem.title}</strong></div>
        <small>{caseItem.occurred_at ? caseItem.occurred_at.slice(0, 10) : '未填写日期'} · {sortedMedia.length} 张图片</small>
      </header>
      <div className="life-material-body">
        <div className="life-material-images">
          {sortedMedia.map(media => {
            const heic = /hei[cf]/i.test(media.mime_type) || /\.hei[cf]$/i.test(media.display_name)
            return (
              <figure key={media.id} title={media.display_name}>
                <div className="heic-placeholder"><Camera /><span>{heic ? 'HEIC' : '图片'}</span><small>{media.display_name}</small></div>
                {!heic && <img src={apiUrl(media.media_url)} alt={media.display_name} loading="lazy" onError={event => { event.currentTarget.style.display = 'none' }} />}
              </figure>
            )
          })}
        </div>
        <p>{caseItem.note_text}</p>
      </div>
      {!compact && <footer><ShieldCheck />原图保存在本机；生成时不会把图片地址写进文案或日志。</footer>}
    </section>
  )
}

function SourceDetails({ sources, research }: { sources: Source[]; research?: Record<string, unknown> }) {
  const observation = research?.life_case_observation as Record<string, unknown> | undefined
  const observationGroups = observation ? [
    ['你提供的事实', observation.user_facts],
    ['图片中可确认', observation.visible_facts],
    ['主编不会擅自写入', observation.uncertain_items],
    ['隐私提醒', observation.privacy_notes],
  ].map(([label, value]) => ({ label: String(label), values: Array.isArray(value) ? value.map(String) : [] })).filter(group => group.values.length) : []
  return (
    <details>
      <summary>{observation ? <Camera /> : <Search />}{observation ? '素材识别' : '研究来源'} <span>{observation ? observationGroups.reduce((sum, group) => sum + group.values.length, 0) : sources.length}</span><ChevronDown /></summary>
      <div className="details-body">
        {observationGroups.map(group => <div className="observation-group" key={group.label}><strong>{group.label}</strong>{group.values.map(item => <p key={item}>· {item}</p>)}</div>)}
        {research && <p className="research-brief">{String(research.brief || '')}</p>}
        {sources.length === 0 ? !observation && <p className="muted">本次没有使用外部来源。</p> : sources.map(source => (
          <article className="source-row" key={source.id}>
            <span>{source.source_key}</span>
            <div>
              {source.url ? <a href={source.url} target="_blank" rel="noreferrer">{source.title}<ExternalLink /></a> : <strong>{source.title}</strong>}
              <p>{source.excerpt || '来源已记录'}</p>
              <small>{source.metadata?.search_mode === 'openai_web_search' ? '模型联网来源' : ['rss_fallback', 'bing_rss', 'sogou_web', 'public_web'].includes(source.metadata?.search_mode || '') ? '公开联网来源' : '资料库'} · {formatDate(source.metadata?.searched_at || source.published_at)}</small>
            </div>
          </article>
        ))}
      </div>
    </details>
  )
}

function ReviewDetails({ review }: { review?: Review }) {
  return (
    <details>
      <summary><ShieldCheck />主编意见 {review && <span>{review.total_score} 分</span>}<ChevronDown /></summary>
      <div className="details-body">
        {!review ? <p className="muted">主编尚未完成审核。</p> : <>
          <p className="review-summary">{review.change_summary}</p>
          {review.risks?.length > 0 && <div className="risk-list"><strong>需要留意</strong>{review.risks.map(item => <p key={item}>· {item}</p>)}</div>}
          {review.blocking_issues?.length > 0 && <div className="risk-list danger"><strong>仍需处理</strong>{review.blocking_issues.map(item => <p key={item}>· {item}</p>)}</div>}
        </>}
      </div>
    </details>
  )
}

function recommendationTitle(value: TopicRadarRecommendation | string | null | undefined) {
  if (typeof value === 'string') return value.trim()
  return String(value?.topic || value?.title || value?.angle || '').trim()
}

function recommendationReason(value: TopicRadarRecommendation | string | null | undefined) {
  if (!value || typeof value === 'string') return ''
  return String(value.reason || value.why || '').trim()
}

function platformSearchLinksForQuery(query: string): PlatformSearchLink[] {
  const clean = String(query || '').trim()
  if (!clean) return []
  return [
    {
      platform: 'douyin',
      label: '抖音搜索',
      url: `https://www.douyin.com/search/${encodeURIComponent(clean)}?type=video`,
      source_mode: 'official_search',
    },
    {
      platform: 'xiaohongshu',
      label: '小红书搜索',
      url: `https://www.xiaohongshu.com/search_result/?keyword=${encodeURIComponent(clean)}`,
      source_mode: 'official_search',
    },
  ]
}

function isOfficialPlatformSearchLink(value: unknown): value is PlatformSearchLink {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<PlatformSearchLink>
  if ((item.platform !== 'douyin' && item.platform !== 'xiaohongshu') || typeof item.url !== 'string') return false
  try {
    const parsed = new URL(item.url)
    if (parsed.protocol !== 'https:') return false
    const host = parsed.hostname.toLowerCase()
    return item.platform === 'douyin'
      ? host === 'douyin.com' || host.endsWith('.douyin.com')
      : host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')
  } catch { return false }
}

function resolvePlatformSearchLinks(rawLinks: unknown, query: string): PlatformSearchLink[] {
  const fallback = platformSearchLinksForQuery(query)
  const fromResponse = Array.isArray(rawLinks) ? rawLinks.filter(isOfficialPlatformSearchLink) : []
  const byPlatform = new Map(fromResponse.map(item => [item.platform, item]))
  return fallback.map(item => byPlatform.get(item.platform) || item)
}

function recommendationPlatformSearchLinks(value: TopicRadarRecommendation | string | null | undefined) {
  const query = typeof value === 'string'
    ? value
    : String(value?.search_query || recommendationTitle(value) || '').trim()
  return resolvePlatformSearchLinks(typeof value === 'string' ? undefined : value?.platform_search_links, query)
}

function recommendationSourceUrls(value: TopicRadarRecommendation | string | null | undefined, platformLinks: PlatformSearchLink[]) {
  const evidence = typeof value === 'string' || !value ? [] : Array.isArray(value.source_urls) ? value.source_urls : []
  return Array.from(new Set([...evidence.filter(url => /^https:\/\//i.test(url)), ...platformLinks.map(link => link.url)])).slice(0, 10)
}

function recommendationOriginalPost(value: TopicRadarRecommendation | string | null | undefined) {
  if (!value || typeof value === 'string') return null
  const candidates = [value.evidence_url, value.source_url, ...(value.source_urls || [])].filter((url): url is string => typeof url === 'string')
  for (const url of candidates) {
    try {
      const parsed = new URL(url)
      const host = parsed.hostname.toLowerCase()
      if ((host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com')) && /\/(explore|discovery\/item)\//.test(parsed.pathname)) {
        return { url, label: '小红书原帖', platform: 'xiaohongshu' as const }
      }
      if ((host === 'douyin.com' || host.endsWith('.douyin.com')) && /\/(video|note)\//.test(parsed.pathname)) {
        return { url, label: '抖音原帖', platform: 'douyin' as const }
      }
    } catch { /* ignore malformed historic links */ }
  }
  return null
}

function runRecommendations(run?: TopicRadarRun | null) {
  const values = (run?.summary?.recommendations || run?.summary?.recommended_topics || []) as unknown[]
  return values.filter((item): item is TopicRadarRecommendation | string => typeof item === 'string' || (Boolean(item) && typeof item === 'object'))
}

function XhsExtractionPanel({ extraction }: { extraction?: XiaohongshuExtraction }) {
  if (!extraction) return null
  const successful = extraction.status === 'success' || extraction.status === 'partial'
  const pending = extraction.status === 'queued' || extraction.status === 'extracting'
  const statusLabel = extraction.status === 'success' ? '完整文案已提取'
    : extraction.status === 'partial' ? '已提取部分内容'
      : extraction.status === 'queued' ? '已排队，系统会自动提取'
        : extraction.status === 'extracting' ? '正在自动提取完整文案'
      : extraction.status === 'needs_verification' ? '需要完成小红书安全验证'
        : extraction.status === 'needs_login' ? '需要登录小红书'
          : extraction.status === 'platform_limited' ? '平台暂时限制访问'
            : extraction.status === 'unavailable' ? '原帖已失效或不可访问' : '暂时无法提取'
  if (successful) return <details className="xhs-copy-disclosure">
    <summary><span><FileText />原文已提取</span><b>查看原文 <ChevronDown /></b></summary>
    <section className="xhs-extraction-panel ready">
      <header><div><span className="platform-dot xhs">小</span><strong>{statusLabel}</strong></div><b>{extraction.completeness || 0}% 完整度</b></header>
      <div className="xhs-extraction-meta"><span>{extraction.author || '作者未读取'}</span>{extraction.published_at && <span>{extraction.published_at}</span>}{extraction.tags?.length > 0 && <span>{extraction.tags.slice(0, 6).join(' ')}</span>}</div>
      <pre className="xhs-reading-copy">{extraction.merged_copy || extraction.body || extraction.image_text || [extraction.video_subtitle, extraction.video_speech].filter(Boolean).join('\n\n')}</pre>
      {(extraction.screenshot_url || extraction.body || extraction.image_text || extraction.video_subtitle || extraction.video_speech) && <details className="xhs-extraction-details"><summary>查看提取明细 <ChevronDown /></summary>
        {extraction.screenshot_url && <details><summary>采集截图 <ChevronDown /></summary><img src={apiUrl(extraction.screenshot_url)} alt="小红书原帖采集截图" /></details>}
        {extraction.body && <details><summary>页面正文 <ChevronDown /></summary><pre>{extraction.body}</pre></details>}
        {extraction.image_text && <details><summary>图片文字（OCR） <ChevronDown /></summary><pre>{extraction.image_text}</pre></details>}
        {(extraction.video_subtitle || extraction.video_speech) && <details><summary>视频字幕 / 口播 <ChevronDown /></summary><pre>{[extraction.video_subtitle, extraction.video_speech].filter(Boolean).join('\n\n')}</pre></details>}
      </details>}
      <footer><span>OCR：{extraction.ocr_message || extraction.ocr_status}</span><span>视频：{extraction.asr_message || extraction.asr_status}</span></footer>
    </section>
  </details>
  return <section className={`xhs-extraction-panel xhs-extraction-status ${pending ? 'pending' : 'failed'}`}>
    <header><div><span className="platform-dot xhs">小</span><strong>{pending && <LoaderCircle className="spin" />}{statusLabel}</strong></div>{!pending && <b>{extraction.completeness || 0}% 完整度</b>}</header>
    {extraction.error_message && <div className="xhs-extraction-error"><AlertTriangle /><span>{extraction.error_message}</span></div>}
  </section>
}

function xhsNeedsRetry(extraction?: XiaohongshuExtraction) {
  return Boolean(extraction && !['success', 'partial', 'queued', 'extracting'].includes(extraction.status))
}

function xhsRetryLabel(extraction?: XiaohongshuExtraction) {
  if (extraction?.status === 'needs_verification') return '完成安全验证后重试'
  if (extraction?.status === 'needs_login') return '登录小红书后重试'
  if (extraction?.status === 'platform_limited') return '稍后重新提取'
  if (extraction?.status === 'unavailable') return '重新搜索可访问原帖'
  return '自动提取失败，重试'
}

function RadarPage({ workspaceId, onCreateFromTopic, onTaskCreated, notify }: {
  workspaceId: string
  onCreateFromTopic: (seed: RadarSeed) => void
  onTaskCreated: (value: TaskDetail) => Promise<void>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [monitors, setMonitors] = useState<TopicMonitor[]>([])
  const [dailyRuns, setDailyRuns] = useState<TopicRadarRun[]>([])
  const [browserStatus, setBrowserStatus] = useState<PlatformBrowserStatus | null>(null)
  const [openingBrowser, setOpeningBrowser] = useState(false)
  const [dailyAction, setDailyAction] = useState<'enable' | 'run' | ''>('')
  const [topicQuery, setTopicQuery] = useState('')
  const [topicResult, setTopicResult] = useState<TopicSearchResponse | null>(null)
  const [selectedAngle, setSelectedAngle] = useState('')
  const [searchMode, setSearchMode] = useState<ProductMode>('xiaohongshu_ip')
  const [searchingTopic, setSearchingTopic] = useState(false)
  const [creatingFromTopic, setCreatingFromTopic] = useState(false)
  const [xhsExtractions, setXhsExtractions] = useState<Record<string, XiaohongshuExtraction>>({})
  const [extractingUrl, setExtractingUrl] = useState('')
  const dailyMonitor = useMemo(() => monitors.find(item => item.monitor_kind === 'ip_daily') || null, [monitors])

  const loadXhsExtractions = useCallback(async () => {
    const saved = await api<XiaohongshuExtraction[]>(`/api/topic-radar/xhs/extractions?workspace_id=${encodeURIComponent(workspaceId)}`)
    setXhsExtractions(Object.fromEntries(saved.map(item => [item.url, item])))
    return saved
  }, [workspaceId])

  const loadDailyRuns = useCallback(async (monitorId: string) => {
    if (!monitorId) {
      setDailyRuns([])
      return [] as TopicRadarRun[]
    }
    const value = await api<TopicRadarRun[]>(`/api/topic-radar/monitors/${monitorId}/runs?workspace_id=${encodeURIComponent(workspaceId)}&limit=5`)
    setDailyRuns(value)
    return value
  }, [workspaceId])

  const loadMonitors = useCallback(async () => {
    const value = await api<TopicMonitor[]>(`/api/topic-radar/monitors?workspace_id=${encodeURIComponent(workspaceId)}`)
    setMonitors(value)
    return value
  }, [workspaceId])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [items, collectorStatus, savedExtractions] = await Promise.all([
          api<TopicMonitor[]>(`/api/topic-radar/monitors?workspace_id=${encodeURIComponent(workspaceId)}`),
          api<PlatformBrowserStatus>('/api/topic-radar/browser/status'),
          api<XiaohongshuExtraction[]>(`/api/topic-radar/xhs/extractions?workspace_id=${encodeURIComponent(workspaceId)}`),
        ])
        const daily = items.find(item => item.monitor_kind === 'ip_daily')
        const dailyHistory = daily ? await api<TopicRadarRun[]>(`/api/topic-radar/monitors/${daily.id}/runs?workspace_id=${encodeURIComponent(workspaceId)}&limit=5`) : []
        if (cancelled) return
        setMonitors(items)
        setBrowserStatus(collectorStatus)
        setDailyRuns(dailyHistory)
        setXhsExtractions(Object.fromEntries(savedExtractions.map(item => [item.url, item])))
      } catch (error) {
        if (!cancelled) notify((error as Error).message, 'error')
      }
    }
    load()
    return () => { cancelled = true }
  }, [notify, workspaceId])

  const hasAutomaticExtraction = Object.values(xhsExtractions).some(item => ['queued', 'extracting'].includes(item.status))
  useEffect(() => {
    if (!hasAutomaticExtraction) return
    const timer = window.setInterval(() => { loadXhsExtractions().catch(() => undefined) }, 2000)
    return () => window.clearInterval(timer)
  }, [hasAutomaticExtraction, loadXhsExtractions])

  const serverDailyRefreshing = dailyRuns.some(item => item.status === 'RUNNING')
  useEffect(() => {
    if (!dailyMonitor || !serverDailyRefreshing) return
    const timer = window.setInterval(async () => {
      try {
        const rows = await loadDailyRuns(dailyMonitor.id)
        if (!rows.some(item => item.status === 'RUNNING')) {
          await Promise.all([loadMonitors(), loadXhsExtractions()])
        }
      } catch { /* the next poll can recover a temporary local request failure */ }
    }, 1500)
    return () => window.clearInterval(timer)
  }, [dailyMonitor, serverDailyRefreshing, loadDailyRuns, loadMonitors, loadXhsExtractions])

  const openPlatformBrowser = async () => {
    setOpeningBrowser(true)
    try {
      const value = await api<PlatformBrowserStatus>('/api/topic-radar/browser/open', jsonBody('POST', {
        platform: 'both', query: topicQuery.trim() || '相亲 脱单',
      }))
      setBrowserStatus(value)
      notify('双平台采集浏览器已打开；首次使用请分别扫码登录')
    } catch (error) { notify((error as Error).message, 'error') } finally { setOpeningBrowser(false) }
  }

  const enableDaily = async () => {
    setDailyAction('enable')
    try {
      const result = await api<{ monitor?: TopicMonitor; latest_run?: TopicRadarRun; latestRun?: TopicRadarRun; run?: TopicRadarRun }>('/api/topic-radar/ip-daily/enable', jsonBody('POST', { workspace_id: workspaceId }))
      const nextMonitors = await loadMonitors()
      const monitor = result.monitor || nextMonitors.find(item => item.monitor_kind === 'ip_daily')
      const latestRun = result.latest_run || result.latestRun || result.run
      if (latestRun) setDailyRuns(current => [latestRun, ...current.filter(item => item.id !== latestRun.id)])
      else if (monitor) await loadDailyRuns(monitor.id)
      await loadXhsExtractions()
      notify('每日 IP 热点已开启，会按你保存的人设自动更新')
    } catch (error) { notify((error as Error).message, 'error') } finally { setDailyAction('') }
  }

  const runDaily = async () => {
    setDailyAction('run')
    try {
      const result = await api<{ monitor?: TopicMonitor; latest_run?: TopicRadarRun; latestRun?: TopicRadarRun; run?: TopicRadarRun }>('/api/topic-radar/ip-daily/run', jsonBody('POST', { workspace_id: workspaceId }))
      const latestRun = result.latest_run || result.latestRun || result.run
      const monitor = result.monitor || dailyMonitor
      if (latestRun) setDailyRuns(current => [latestRun, ...current.filter(item => item.id !== latestRun.id)])
      else if (monitor) await loadDailyRuns(monitor.id)
      if (latestRun?.status === 'RUNNING') {
        notify('热点已开始更新，完成后会自动显示')
      } else {
        await Promise.all([loadMonitors(), loadXhsExtractions()])
        const directions = runRecommendations(latestRun).length
        notify(directions ? `今天已整理 ${directions} 个抖音和小红书方向` : '今日 IP 方向已更新')
      }
    } catch (error) { notify((error as Error).message, 'error') } finally { setDailyAction('') }
  }

  const toggleMonitor = async (monitor: TopicMonitor) => {
    try {
      const next = await api<TopicMonitor>(`/api/topic-radar/monitors/${monitor.id}?workspace_id=${encodeURIComponent(workspaceId)}`, jsonBody('PATCH', { enabled: !monitor.enabled }))
      setMonitors(current => current.map(item => item.id === next.id ? next : item))
      notify(next.enabled ? '雷达已恢复定时刷新' : '雷达已暂停')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  const searchTopic = async () => {
    const topic = topicQuery.trim()
    if (!topic) {
      notify('先输入这次想搜的主题', 'error')
      return null
    }
    setSearchingTopic(true)
    try {
      const result = await api<TopicSearchResponse>('/api/topic-radar/topic-search', jsonBody('POST', { workspace_id: workspaceId, topic, limit: 8 }))
      const normalized = { ...result, topic: result.topic || topic }
      const first = (normalized.angles || normalized.recommendations || [])[0]
      setTopicResult(normalized)
      setSelectedAngle(recommendationTitle(first) || normalized.topic || topic)
      setBrowserStatus(await api<PlatformBrowserStatus>('/api/topic-radar/browser/status'))
      await loadXhsExtractions()
      notify(result.visible_source_count ? `已看到 ${result.visible_source_count} 条平台内容，并生成更多主题` : '搜索已完成；登录平台采集浏览器后可看到更多真实内容')
      return normalized
    } catch (error) {
      notify((error as Error).message, 'error')
      return null
    } finally { setSearchingTopic(false) }
  }

  const extractXhsPost = async (url: string, title: string, searchKeyword: string, quiet = false, force = false) => {
    setExtractingUrl(url)
    try {
      const result = await api<{ extraction: XiaohongshuExtraction }>('/api/topic-radar/xhs/extract', jsonBody('POST', {
        workspace_id: workspaceId,
        url,
        title_hint: title,
        search_keyword: searchKeyword,
        force,
      }))
      setXhsExtractions(current => ({ ...current, [result.extraction.url]: result.extraction, [url]: result.extraction }))
      if (result.extraction.status === 'success' || result.extraction.status === 'partial') {
        if (!quiet) notify(`已提取小红书文案，完整度 ${result.extraction.completeness}%`)
      } else if (!quiet) {
        notify(result.extraction.error_message || '暂时无法提取这篇小红书文案', 'error')
      }
      return result.extraction
    } catch (error) {
      if (!quiet) notify((error as Error).message, 'error')
      return null
    } finally {
      setExtractingUrl('')
    }
  }

  const createFromTopicSearch = async () => {
    let result = topicResult
    if (!result || result.topic !== topicQuery.trim()) result = await searchTopic()
    if (!result) return
    const angles = result.angles || result.recommendations || []
    const selected = angles.find(item => recommendationTitle(item) === selectedAngle)
    const topic = selectedAngle || recommendationTitle(selected) || result.topic || topicQuery.trim()
    const platformLinks = selected
      ? recommendationPlatformSearchLinks(selected)
      : resolvePlatformSearchLinks(result.platform_search_links, result.topic || topic)
    const urls = recommendationSourceUrls(selected, platformLinks)
    setCreatingFromTopic(true)
    try {
      const response = await api<TaskDetail | { detail?: TaskDetail }>('/api/topic-radar/topic-search/create', jsonBody('POST', {
        workspace_id: workspaceId,
        topic,
        source_urls: urls,
        product_mode: searchMode,
        constraints: {
          radar_origin: 'topic_search',
          radar_search_query: result.topic || topic,
          radar_platform_searches: platformLinks,
        },
      }))
      const detail = 'task' in response ? response : response.detail
      if (!detail?.task) throw new Error('创作任务已提交，但没有收到任务详情')
      await onTaskCreated(detail)
      notify('已把搜索资料带入创作流程')
    } catch (error) { notify((error as Error).message, 'error') } finally { setCreatingFromTopic(false) }
  }

  const activeDailyRun = dailyRuns.find(item => item.status === 'RUNNING')
  const dailyRun = dailyRuns.find(item => item.status !== 'RUNNING' && (runRecommendations(item).length > 0 || item.items.length > 0))
    || dailyRuns.find(item => item.status !== 'RUNNING')
    || activeDailyRun
  const dailyRecommendations = runRecommendations(dailyRun)
  const dailyPlatforms = dailyRun?.summary?.platforms || []
  const topicAngles = topicResult?.angles || topicResult?.recommendations || []
  const topicSources = topicResult?.sources || topicResult?.items || []
  const savedXhsExtractions = Object.values(xhsExtractions).sort((a, b) => String(b.extracted_at).localeCompare(String(a.extracted_at)))

  return (
    <div className="radar-page">
      <section className="radar-hero">
        <div>
          <span className="eyebrow"><span className="radar-pulse" />GATHER PLANET · 内容雷达</span>
          <h1>热点，不用等你<em>先想到关键词。</em></h1>
          <p>每天按你已保存的人设生成抖音和小红书的搜索方向；有具体主题时，也会先准备两个平台的真实内容入口。</p>
        </div>
        <div className="radar-hero-orbit" aria-hidden="true"><Search /><i /><b /><span>HOT</span></div>
      </section>

      <section className={`radar-browser-card ${browserStatus?.running ? 'connected' : ''}`}>
        <div className="radar-browser-copy">
          <span className="radar-browser-icon"><ShieldCheck /></span>
          <div><strong>平台采集浏览器</strong><p>{browserStatus?.message || '正在检查本机浏览器状态…'}</p><small>首次扫码后，Cookie 和登录会话会一直保存在这台电脑的专用 Chrome 资料中；平台主动让会话过期或触发风控时，仍需本人重新验证。</small></div>
        </div>
        <div className="radar-browser-actions">
          <span className={`radar-status-chip ${browserStatus?.running ? 'on' : ''}`}><span />{browserStatus?.running ? '已连接' : browserStatus?.available === false ? '未安装浏览器' : '等待打开'}</span>
          <button className="primary-button" onClick={openPlatformBrowser} disabled={openingBrowser || browserStatus?.available === false}>{openingBrowser ? <LoaderCircle className="spin" /> : <ExternalLink />}{openingBrowser ? '正在打开' : browserStatus?.running ? '打开登录页' : '打开双平台浏览器'}</button>
        </div>
      </section>

      <div className="radar-path-grid">
        <section className="radar-path-card radar-ip-card">
          <div className="radar-path-heading">
            <span className="radar-path-icon"><Bot /></span>
            <div><span className="section-number">01</span><h2>每日 IP 自动热点</h2><p>按你的人设筛选值得看的方向，每张卡清楚展示标题、内容方向、热度依据和创作判断。</p></div>
          </div>
          <div className="radar-path-actions">
            {!dailyMonitor ? <button className="primary-button" onClick={enableDaily} disabled={dailyAction !== ''}>{dailyAction === 'enable' ? <LoaderCircle className="spin" /> : <Sparkles />}{dailyAction === 'enable' ? '正在开启' : '开启每天自动找热点'}</button> : <>
              <span className={`radar-status-chip ${dailyMonitor.enabled ? 'on' : ''}`}><span />{dailyMonitor.enabled ? '已开启 · 每天自动更新' : '已暂停'}</span>
              <button className="secondary-button" onClick={() => toggleMonitor(dailyMonitor)}>{dailyMonitor.enabled ? '暂停' : '恢复'}</button>
              <button className="primary-button" onClick={runDaily} disabled={dailyAction !== '' || serverDailyRefreshing}>{dailyAction === 'run' || serverDailyRefreshing ? <LoaderCircle className="spin" /> : <RefreshCw />}{dailyAction === 'run' || serverDailyRefreshing ? '正在更新' : '现在帮我找热点'}</button>
            </>}
          </div>
          {dailyMonitor?.last_error && <div className="radar-notice"><AlertTriangle /><span>{dailyMonitor.last_error}</span></div>}
          {dailyRun ? <div className="radar-daily-result">
            <div className="radar-daily-result-heading"><div><strong>今天的 IP 方向</strong><small>{activeDailyRun && dailyRun.id !== activeDailyRun.id ? '正在更新 · 先展示上一次可用结果' : dailyRun.completed_at ? `更新于 ${formatDate(dailyRun.completed_at)} · 基于平台可见内容` : '正在整理双平台方向'}</small></div><span>{dailyRecommendations.length || 0} 个方向</span></div>
            <div className="radar-daily-platforms">{(['xiaohongshu', 'douyin'] as const).map(platform => {
              const summary = dailyPlatforms.find(item => item.platform === platform)
              const count = Number(summary?.count || 0)
              return <div className={count > 0 ? 'ready' : ''} key={platform}><span className={`platform-dot ${platform === 'douyin' ? 'dy' : 'xhs'}`}>{platform === 'douyin' ? '抖' : '小'}</span><div><strong>{platform === 'douyin' ? '抖音' : '小红书'} {count} 条</strong><small>{summary?.source_mode === 'cached_visible_browser' ? '实时验证未完成，先显示上次保存的热门内容' : count > 0 ? '已按相关性和可见互动数排序' : summary?.message || '等待读取平台内容'}</small></div></div>
            })}</div>
            <div className="radar-batch-extract"><span>{hasAutomaticExtraction ? <LoaderCircle className="spin" /> : <CheckCircle2 />}{hasAutomaticExtraction ? '正在自动提取小红书完整文案' : '小红书原帖会自动提取'}</span><small>无需点击；每轮搜索后会自动读取前 6 篇原文、图片文字和可见字幕。</small></div>
            {dailyRecommendations.length > 0 ? <div className="radar-recommendations">{dailyRecommendations.slice(0, 12).map((item, index) => {
              const title = recommendationTitle(item)
              const platformLinks = recommendationPlatformSearchLinks(item)
              const persona = typeof item === 'string' ? '' : item.persona
              const pillar = typeof item === 'string' ? '' : item.pillar
              const searchQuery = typeof item === 'string' ? title : item.search_query || title
              const originalPost = recommendationOriginalPost(item)
              const extraction = originalPost?.platform === 'xiaohongshu' ? xhsExtractions[originalPost.url] : undefined
              return <article className="radar-recommendation" key={`${title}-${index}`}>
                <div className="radar-recommendation-kicker"><b>{String(index + 1).padStart(2, '0')}</b><span>{persona || '今日推荐'}</span></div>
                <strong>{title || '今日可看方向'}</strong>
                {pillar && <small className="radar-recommendation-pillar"><b>IP 方向</b>{pillar}</small>}
                <p><b>创作判断</b>{recommendationReason(item) || '结合真实内容与评论，把这个话题改写成贴近受众的内容。'}</p>
                {originalPost && <a className="radar-original-post" href={originalPost.url} target="_blank" rel="noreferrer"><span className={`platform-dot ${originalPost.platform === 'douyin' ? 'dy' : 'xhs'}`}>{originalPost.platform === 'douyin' ? '抖' : '小'}</span><span><b>{originalPost.label}</b><small>{[radarMetricText(typeof item === 'string' ? undefined : item.visible_metrics), '查看真实原帖'].filter(Boolean).join(' · ')}</small></span><ExternalLink /></a>}
                {originalPost?.platform === 'xiaohongshu' && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(originalPost.url, title, searchQuery, false, true)} disabled={extractingUrl === originalPost.url}>{extractingUrl === originalPost.url ? <LoaderCircle className="spin" /> : <RefreshCw />}{extractingUrl === originalPost.url ? '正在重试' : xhsRetryLabel(extraction)}</button>}
                <XhsExtractionPanel extraction={extraction} />
                <footer><button onClick={() => onCreateFromTopic({ topic: title || dailyMonitor?.query || '今天的 IP 热点', sourceUrls: recommendationSourceUrls(item, platformLinks), searchQuery, platformSearches: platformLinks })}><WandSparkles />用这个方向创作</button></footer>
              </article>
            })}</div> : <div className="radar-path-empty"><Search /><div><strong>还没有足够的平台内容</strong><p>先打开双平台浏览器并扫码登录，再点击“现在帮我找热点”。</p></div></div>}
            {dailyRun.items.length > 0 && <details className="radar-visible-sources"><summary>查看本轮实际看到的 {dailyRun.items.length} 条内容 <ChevronDown /></summary><div className="radar-item-grid">{dailyRun.items.slice(0, 12).map(item => <RadarItemCard key={item.id} item={item} extraction={xhsExtractions[item.url]} extracting={extractingUrl === item.url} onExtract={(url, title) => extractXhsPost(url, title, dailyMonitor?.query || title, false, Boolean(xhsExtractions[item.url]))} />)}</div></details>}
          </div> : <div className="radar-path-empty"><Sparkles /><div><strong>先让它认识你的内容方向</strong><p>开启后会每天自动观察；不需要你每天想一个新的搜索词。</p></div></div>}
        </section>

        <section className="radar-path-card radar-topic-card">
          <div className="radar-path-heading">
            <span className="radar-path-icon peach"><Search /></span>
            <div><span className="section-number">02</span><h2>主题搜索后创作</h2><p>输入一个主题，系统会整理双平台内容并给出清晰的可讲方向，再直接进入创作。</p></div>
          </div>
          <div className="radar-topic-form">
            <input value={topicQuery} onChange={event => setTopicQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') searchTopic() }} placeholder="例如：为什么越来越多女生不想相亲" aria-label="输入想搜索的主题" />
            <div className="radar-format-switch" role="group" aria-label="选择创作格式">
              <button className={searchMode === 'xiaohongshu_ip' ? 'selected' : ''} onClick={() => setSearchMode('xiaohongshu_ip')}><span className="platform-dot xhs">小</span>小红书文案</button>
              <button className={searchMode === 'douyin_emotion' ? 'selected' : ''} onClick={() => setSearchMode('douyin_emotion')}><span className="platform-dot dy">抖</span>抖音口播</button>
            </div>
            <div className="radar-topic-actions"><button className="secondary-button" onClick={searchTopic} disabled={searchingTopic || creatingFromTopic}>{searchingTopic ? <LoaderCircle className="spin" /> : <Search />}{searchingTopic ? '正在搜索' : '先搜主题'}</button><button className="primary-button" onClick={createFromTopicSearch} disabled={searchingTopic || creatingFromTopic}>{creatingFromTopic ? <LoaderCircle className="spin" /> : <WandSparkles />}{creatingFromTopic ? '正在开始创作' : '搜索后开始创作'}</button></div>
          </div>
          <div className="radar-full-copy-guide"><FileText /><div><strong>小红书完整文案会自动出现</strong><p>搜索完成后，系统会直接提取正文、图片文字和可见字幕，不需要再点“提取文案”。</p></div></div>
          {topicResult && <div className="radar-topic-result">
            <div className="radar-daily-result-heading"><div><strong>“{topicResult.topic || topicQuery}” 的可讲方向</strong><small>{topicResult.visible_source_count ? `实际读取 ${topicResult.visible_source_count} 条平台内容` : topicSources.length ? `已用 ${topicSources.length} 条平台索引辅助判断` : '等待平台内容'}</small></div><span>{topicAngles.length} 个主题</span></div>
            {topicAngles.length > 0 ? <div className="topic-angle-list">{topicAngles.slice(0, 12).map((angle, index) => {
              const title = recommendationTitle(angle)
              return <button key={`${title}-${index}`} className={selectedAngle === title ? 'selected' : ''} onClick={() => setSelectedAngle(title)}><span>{selectedAngle === title ? <CheckCircle2 /> : index + 1}</span><div><strong>{title || '可讲内容方向'}</strong><p>{recommendationReason(angle) || '选中后会把这个角度和搜索资料一起带进创作。'}</p></div></button>
            })}</div> : <p className="radar-result-copy">搜索资料已准备好。点击“搜索后开始创作”，让工作台把主题写成可发布内容。</p>}
            {topicResult.warning && <div className="radar-notice"><AlertTriangle /><span>{topicResult.warning}</span></div>}
            {topicSources.length > 0 && <details className="radar-visible-sources" open><summary>查看实际找到的 {topicSources.length} 条平台内容 <ChevronDown /></summary><div className="radar-topic-source-grid">{topicSources.slice(0, 16).map((source, index) => {
              const isXhs = source.metadata?.platform === 'xiaohongshu'
              const extraction = isXhs ? xhsExtractions[source.url] || source.metadata?.xhs_extraction : undefined
              return <article key={`${source.url}-${index}`} className="radar-topic-source">
                <span>{isXhs ? '小红书' : source.metadata?.platform === 'douyin' ? '抖音' : '平台原文'}</span>
                <strong>{source.title}</strong>
                <small className="radar-summary-label">搜索页摘要</small>
                <p>{source.excerpt}</p>
                <footer><small>{[source.metadata?.author, radarMetricText(source.metadata?.metrics)].filter(Boolean).join(' · ')}</small><a href={source.url} target="_blank" rel="noreferrer">打开原文 <ExternalLink /></a></footer>
                {isXhs && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(source.url, source.title, topicResult.topic || topicQuery, false, true)} disabled={extractingUrl === source.url}>{extractingUrl === source.url ? <LoaderCircle className="spin" /> : <RefreshCw />}{extractingUrl === source.url ? '正在重试' : xhsRetryLabel(extraction)}</button>}
                <XhsExtractionPanel extraction={extraction} />
              </article>
            })}</div></details>}
          </div>}
        </section>
      </div>

      {savedXhsExtractions.length > 0 && <details className="xhs-extraction-library xhs-extraction-library-wide">
        <summary><span>已提取原文库（{savedXhsExtractions.length}）</span><small>需要时再展开查看</small><ChevronDown /></summary>
        <div>{savedXhsExtractions.slice(0, 12).map(extraction => <article key={extraction.id}>
          <header><div><span className="platform-dot xhs">小</span><strong>{extraction.title || '小红书原帖'}</strong></div><a href={extraction.url} target="_blank" rel="noreferrer">平台原帖 <ExternalLink /></a></header>
          {xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => extractXhsPost(extraction.url, extraction.title, extraction.search_keyword, false, true)} disabled={extractingUrl === extraction.url}>{extractingUrl === extraction.url ? <LoaderCircle className="spin" /> : <RefreshCw />}重新尝试</button>}
          <XhsExtractionPanel extraction={extraction} />
        </article>)}</div>
      </details>}

    </div>
  )
}

function RadarItemCard({ item, extraction, extracting, onExtract }: {
  item: TopicRadarItem
  extraction?: XiaohongshuExtraction
  extracting: boolean
  onExtract: (url: string, title: string) => void
}) {
  const metricEntries = Object.entries(item.metrics || {}).filter(([, value]) => value !== null && value !== undefined && value !== '')
  const publicWeb = item.platform === 'web'
  const dotClass = item.platform === 'xiaohongshu' ? 'xhs' : publicWeb ? 'web' : 'dy'
  const platformLabel = item.platform === 'xiaohongshu' ? '小红书可见内容' : publicWeb ? '公开趋势线索' : item.source_mode === 'visible_browser' ? '抖音可见内容' : '抖音公开索引'
  const dotText = item.platform === 'xiaohongshu' ? '小' : publicWeb ? '热' : '抖'
  return <article className="radar-item-card">
    <div className="radar-item-meta"><span className={`platform-dot ${dotClass}`}>{dotText}</span><span>{platformLabel}</span>{item.is_new && <b>NEW</b>}</div>
    <h3>{item.title}</h3>
    {item.excerpt && <><small className="radar-summary-label">搜索页摘要</small><p>{item.excerpt}</p></>}
    <footer><span>{item.author || '平台内容'}{item.published_at ? ` · ${formatDate(item.published_at)}` : ''}</span>{metricEntries.length > 0 && <small>{metricEntries.slice(0, 3).map(([key, value]) => `${key === 'likes' ? '赞' : key === 'comments' ? '评' : key === 'shares' ? '转' : key === 'plays' ? '播' : key === 'visible_engagement' ? '可见热度' : key}${value}`).join(' · ')}</small>}{item.url && <a href={item.url} target="_blank" rel="noreferrer" aria-label="打开内容"><ExternalLink /></a>}</footer>
    {item.platform === 'xiaohongshu' && item.url && xhsNeedsRetry(extraction) && <button className="xhs-extract-button" onClick={() => onExtract(item.url, item.title)} disabled={extracting}>{extracting ? <LoaderCircle className="spin" /> : <RefreshCw />}{extracting ? '正在重试' : xhsRetryLabel(extraction)}</button>}
    <XhsExtractionPanel extraction={extraction} />
  </article>
}

function radarMetricText(metrics?: Record<string, number | string | null>) {
  return Object.entries(metrics || {}).filter(([, value]) => value !== null && value !== undefined && value !== '').slice(0, 2).map(([key, value]) => `${key === 'likes' ? '赞' : key === 'comments' ? '评' : key === 'shares' ? '转' : key === 'plays' ? '播' : key === 'visible_engagement' ? '可见热度' : key}${value}`).join(' · ')
}

function WorksPage({ tasks, lifeCases, detail, contentTypes, onOpen, onRefresh, onCreated, onCasesChanged, onDuplicateCase, onSettings, notify }: {
  tasks: Task[]
  lifeCases: LifeCase[]
  detail: TaskDetail | null
  contentTypes: ContentType[]
  onOpen: (id: string) => Promise<void>
  onRefresh: () => Promise<void>
  onCreated: (value: TaskDetail) => Promise<void>
  onCasesChanged: () => Promise<void>
  onDuplicateCase: (value: LifeCase) => void
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null)
  const filtered = useMemo(() => tasks.filter(task => {
    const matchQuery = !query || task.topic.toLowerCase().includes(query.toLowerCase())
    const matchFilter = filter === 'all' || (filter === 'final' ? task.status === 'FINAL_READY' : ['NEEDS_MODEL', 'NEEDS_ATTENTION', 'FAILED'].includes(task.status))
    return matchQuery && matchFilter
  }), [tasks, query, filter])
  const filteredCases = useMemo(() => lifeCases.filter(item => !query || `${item.title} ${item.note_text}`.toLowerCase().includes(query.toLowerCase())), [lifeCases, query])
  const selectedCase = lifeCases.find(item => item.id === selectedCaseId) || null

  const regenerate = async () => {
    if (!detail) return
    try {
      const value = await api<TaskDetail>(`/api/copy-tasks/${detail.task.id}/regenerate`, { method: 'POST' })
      await onCreated(value)
      notify('已创建一条新的生成任务')
    } catch (error) { notify((error as Error).message, 'error') }
  }

  return (
    <div className="works-page">
      <section className="page-heading"><span className="eyebrow">所有创作都在这里</span><h1>作品记录</h1><p>打开、修改或重新生成，不会覆盖之前的版本。</p></section>
      <div className="works-layout">
        <aside className="work-list-panel">
          <div className="list-tools">
            <label><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索作品" /></label>
            <div>
              <button className={filter === 'all' ? 'active' : ''} onClick={() => { setFilter('all'); setSelectedCaseId(null) }}>全部</button>
              <button className={filter === 'final' ? 'active' : ''} onClick={() => { setFilter('final'); setSelectedCaseId(null) }}>已定稿</button>
              <button className={filter === 'problem' ? 'active' : ''} onClick={() => { setFilter('problem'); setSelectedCaseId(null) }}>需处理</button>
              <button className={filter === 'life' ? 'active' : ''} onClick={() => { setFilter('life'); setSelectedCaseId(null) }}>生活案例</button>
            </div>
          </div>
          <div className="work-list">
            {filter === 'life' ? <>
              {filteredCases.length === 0 && <div className="empty-list"><Camera /><p>还没有保存生活案例</p></div>}
              {filteredCases.map(item => (
                <button className={selectedCaseId === item.id ? 'active' : ''} key={item.id} onClick={async () => {
                  setSelectedCaseId(item.id)
                  if (item.latest_task_id) await onOpen(item.latest_task_id)
                }}>
                  <span className={`task-dot ${item.archived ? '' : (item.latest_task_status || '').toLowerCase()}`} />
                  <div><strong>{item.title}</strong><small>{item.archived ? '已归档' : item.latest_task_status ? statusNames[item.latest_task_status] || item.latest_task_status : '仅保存，尚未生成'}</small></div>
                  <time>{item.occurred_at?.slice(5, 10) || formatDate(item.created_at)}</time>
                </button>
              ))}
            </> : <>
              {filtered.length === 0 && <div className="empty-list"><FileText /><p>还没有符合条件的作品</p></div>}
              {filtered.map(task => (
                <button className={detail?.task.id === task.id ? 'active' : ''} key={task.id} onClick={() => { setSelectedCaseId(null); onOpen(task.id) }}>
                  <span className={`task-dot ${task.status.toLowerCase()}`} />
                  <div><strong>{task.topic}</strong><small>{taskProductLabel(task)} · {statusNames[task.status] || task.status}</small></div>
                  <time>{formatDate(task.created_at)}</time>
                </button>
              ))}
            </>}
          </div>
        </aside>
        <section className="work-detail">
          {filter === 'life' ? selectedCase ? (
            <LifeCaseRecord
              caseItem={selectedCase}
              taskDetail={detail?.task.id === selectedCase.latest_task_id ? detail : null}
              onRefreshTask={onRefresh}
              onTaskCreated={onCreated}
              onCasesChanged={onCasesChanged}
              onDuplicate={() => onDuplicateCase(selectedCase)}
              onDeleted={() => setSelectedCaseId(null)}
              onSettings={onSettings}
              notify={notify}
            />
          ) : <div className="empty-detail"><Camera /><h2>选择一条生活案例</h2><p>图片、原始记录和三版朋友圈会显示在这里。</p></div> : !detail ? <div className="empty-detail"><FileText /><h2>选择一条作品</h2><p>定稿、来源和主编意见会显示在这里。</p></div> : <>
            <TaskWorkspace detail={detail} onRefresh={onRefresh} onSettings={onSettings} notify={notify} />
            <button className="regenerate-button" onClick={regenerate}><RotateCcw />按相同要求重新生成</button>
          </>}
        </section>
      </div>
    </div>
  )
}

function LifeCaseRecord({ caseItem, taskDetail, onRefreshTask, onTaskCreated, onCasesChanged, onDuplicate, onDeleted, onSettings, notify }: {
  caseItem: LifeCase
  taskDetail: TaskDetail | null
  onRefreshTask: () => Promise<void>
  onTaskCreated: (value: TaskDetail) => Promise<void>
  onCasesChanged: () => Promise<void>
  onDuplicate: () => void
  onDeleted: () => void
  onSettings: () => void
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [working, setWorking] = useState('')

  const generate = async () => {
    setWorking('generate')
    try {
      const value = await api<TaskDetail>(`/api/life-cases/${caseItem.id}/generate?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, { method: 'POST' })
      await onTaskCreated(value)
      notify(value.task.status === 'NEEDS_MODEL' ? '任务已保留，连接模型后继续' : '三名数字员工已经开始工作')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  const setArchived = async (archived: boolean) => {
    setWorking('archive')
    try {
      await api<LifeCase>(`/api/life-cases/${caseItem.id}?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, jsonBody('PATCH', { archived }))
      await onCasesChanged()
      notify(archived ? '生活案例已归档，原图仍保留' : '生活案例已恢复')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  const permanentlyDelete = async () => {
    if (!window.confirm('永久删除后，案例文字和本地原图无法恢复；已生成的作品会保留。确定删除吗？')) return
    setWorking('delete')
    try {
      await api(`/api/life-cases/${caseItem.id}?workspace_id=${encodeURIComponent(caseItem.workspace_id)}`, { method: 'DELETE' })
      onDeleted()
      await onCasesChanged()
      notify('生活案例和本地原图已永久删除')
    } catch (error) { notify((error as Error).message, 'error') } finally { setWorking('') }
  }

  return (
    <div className="life-case-record">
      <div className="case-record-actions">
        <div><span className="eyebrow">生活案例记录</span><h2>{caseItem.title}</h2></div>
        <div>
          <button onClick={onDuplicate}><Copy />复制为新案例</button>
          <button disabled={Boolean(working)} onClick={() => setArchived(!caseItem.archived)}>{caseItem.archived ? <ArchiveRestore /> : <Archive />}{caseItem.archived ? '恢复' : '归档'}</button>
          <button className="danger-button" disabled={Boolean(working)} onClick={permanentlyDelete}><Trash2 />永久删除</button>
        </div>
      </div>
      <LifeCaseMaterial caseItem={caseItem} />
      {!caseItem.archived && !taskDetail && <div className="case-generate-callout"><div><strong>素材已经安全保存</strong><p>现在可以交给研究员、文案员工和主编，生成三种朋友圈角度。</p></div><button className="primary-button" disabled={Boolean(working)} onClick={generate}>{working === 'generate' ? <LoaderCircle className="spin" /> : <Sparkles />}生成三版文案</button></div>}
      {taskDetail && <TaskWorkspace detail={taskDetail} onRefresh={onRefreshTask} onSettings={onSettings} notify={notify} showSourceCase={false} />}
    </div>
  )
}

function SettingsPage({ workspaceId, modelStatus, skills, onUpdated, notify }: {
  workspaceId: string
  modelStatus: Bootstrap['model_status']
  skills: SkillStatus[]
  onUpdated: () => Promise<Bootstrap>
  notify: (text: string, kind?: 'ok' | 'error') => void
}) {
  const [settings, setSettings] = useState<LocalProxySettings | null>(null)
  const [deepseekSettings, setDeepseekSettings] = useState<DeepSeekSettings | null>(null)
  const [profile, setProfile] = useState<WorkspaceProfile | null>(null)
  const [proxyBaseUrl, setProxyBaseUrl] = useState('http://127.0.0.1:8787/v1')
  const [model, setModel] = useState('gpt-5.6-luna')
  const [deepseekModel, setDeepseekModel] = useState('deepseek-v4-flash')
  const [deepseekApiKey, setDeepseekApiKey] = useState('')
  const [commonInfo, setCommonInfo] = useState('')
  const [testing, setTesting] = useState(false)
  const [savingDeepseek, setSavingDeepseek] = useState(false)
  const [savingInfo, setSavingInfo] = useState(false)

  const load = useCallback(async () => {
    const [localProxy, currentDeepseek, currentProfile] = await Promise.all([
      api<LocalProxySettings>('/api/settings/local-proxy'),
      api<DeepSeekSettings>('/api/settings/deepseek'),
      api<WorkspaceProfile>(`/api/workspaces/${workspaceId}/profile`),
    ])
    setSettings(localProxy)
    setProxyBaseUrl(localProxy.base_url || 'http://127.0.0.1:8787/v1')
    setModel(localProxy.model.model || 'gpt-5.6-luna')
    setDeepseekSettings(currentDeepseek)
    setDeepseekModel(currentDeepseek.model.model || 'deepseek-v4-flash')
    setProfile(currentProfile)
    setCommonInfo(String(currentProfile.profile?.common_info || ''))
  }, [workspaceId])

  useEffect(() => { load().catch(error => notify((error as Error).message, 'error')) }, [load, notify])

  const saveConnection = async () => {
    if (!proxyBaseUrl.trim() || !model.trim()) {
      notify('请填写本机代理地址和模型名称', 'error')
      return
    }
    setTesting(true)
    try {
      const result = await api<LocalProxySettings>('/api/settings/local-proxy', jsonBody('PUT', { base_url: proxyBaseUrl, model }))
      setSettings(result)
      await onUpdated()
      notify(result.search.status === 'fallback' ? '本机代理已连接，热点将使用多个公开搜索源与资料库' : '本机代理连接检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setTesting(false) }
  }

  const recheck = async () => {
    setTesting(true)
    try {
      const result = await api<LocalProxySettings>('/api/settings/local-proxy/recheck', { method: 'POST' })
      setSettings(result)
      await onUpdated()
      notify('重新检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setTesting(false) }
  }

  const saveDeepseek = async () => {
    setSavingDeepseek(true)
    try {
      const result = await api<DeepSeekSettings>('/api/settings/deepseek', jsonBody('PUT', {
        model: deepseekModel,
        ...(deepseekApiKey.trim() ? { api_key: deepseekApiKey.trim() } : {}),
      }))
      setDeepseekSettings(result)
      setDeepseekApiKey('')
      await onUpdated()
      notify('DeepSeek 已验证并启用，三名员工将使用它生成文案')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setSavingDeepseek(false) }
  }

  const recheckDeepseek = async () => {
    setSavingDeepseek(true)
    try {
      const result = await api<DeepSeekSettings>('/api/settings/deepseek/recheck', { method: 'POST' })
      setDeepseekSettings(result)
      await onUpdated()
      notify('DeepSeek 重新检测完成')
    } catch (error) {
      notify((error as Error).message, 'error')
      await load()
      await onUpdated()
    } finally { setSavingDeepseek(false) }
  }

  const saveInfo = async () => {
    setSavingInfo(true)
    try {
      const value = await api<WorkspaceProfile>(`/api/workspaces/${workspaceId}/profile`, jsonBody('PATCH', { fields: { common_info: commonInfo } }))
      setProfile(value)
      notify('常用信息已保存')
    } catch (error) { notify((error as Error).message, 'error') } finally { setSavingInfo(false) }
  }

  return (
    <div className="settings-page">
      <section className="page-heading"><span className="eyebrow">一次设置，三名员工共用</span><h1>设置</h1><p>这里只保留真正影响文案生成的内容。</p></section>
      <div className="settings-stack">
        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon purple"><Bot /></span><div><h2>本机 OpenAI OAuth 代理</h2><p>极速模式：本机完成研究与审核，只调用一次 Luna 生成正文，并关闭自动整轮返工。</p></div><StatusDot status={settings?.model.status || modelStatus.status} /></div>
          <div className="connection-summary">
            <div><span>模型生成</span><strong className={settings?.model.status === 'verified' ? 'success-text' : 'warning-text'}>{settings?.model.message || '正在读取'}</strong></div>
            <div><span>联网搜索</span><strong className={settings?.search.status === 'verified' || settings?.search.status === 'fallback' ? 'success-text' : 'warning-text'}>{settings?.search.message || '正在读取'}</strong></div>
            <div><span>最后检测</span><strong>{settings?.checked_at ? formatDate(settings.checked_at) : '尚未检测'}</strong></div>
          </div>
          <div className="connection-form">
            <label className="key-field"><span>代理根地址</span><input value={proxyBaseUrl} onChange={event => setProxyBaseUrl(event.target.value)} placeholder="http://127.0.0.1:8787/v1" /></label>
            <label><span>模型名称</span><input value={model} onChange={event => setModel(event.target.value)} placeholder="由本机代理提供，例如 gpt-5.6-luna" /></label>
            <button className="primary-button" onClick={saveConnection} disabled={testing}>{testing ? <LoaderCircle className="spin" /> : <Wifi />}{testing ? '正在测试代理' : '测试并保存'}</button>
            {settings && <button className="secondary-button" onClick={recheck} disabled={testing}><RefreshCw />重新检测</button>}
          </div>
          <p className="security-note"><ShieldCheck />这个连接不读取或保存 OAuth Token；登录与授权只在本机代理中完成。点击“测试并保存”会重新启用本机代理。</p>
        </section>

        <section className="settings-card provider-card deepseek-card">
          <div className="settings-title"><span className="settings-icon rose"><Sparkles /></span><div><h2>DeepSeek API</h2><p>可选直连。测试成功后会成为默认模型；本机 OAuth 代理会保留，随时可以切回。</p></div><StatusDot status={deepseekSettings?.model.status || 'unverified'} /></div>
          <div className="connection-summary">
            <div><span>模型生成</span><strong className={deepseekSettings?.model.status === 'verified' ? 'success-text' : 'warning-text'}>{deepseekSettings?.model.message || '正在读取'}</strong></div>
            <div><span>API Key</span><strong className={deepseekSettings?.has_api_key ? 'success-text' : 'warning-text'}>{deepseekSettings?.has_api_key ? '已保存到本机钥匙串' : '尚未填写'}</strong></div>
            <div><span>当前使用</span><strong className={deepseekSettings?.active ? 'success-text' : ''}>{deepseekSettings?.active ? '已启用' : '未启用'}</strong></div>
            <div><span>最后检测</span><strong>{deepseekSettings?.checked_at ? formatDate(deepseekSettings.checked_at) : '尚未检测'}</strong></div>
          </div>
          <div className="connection-form deepseek-form">
            <label><span>DeepSeek 模型</span><select value={deepseekModel} onChange={event => setDeepseekModel(event.target.value)}>
              <option value="deepseek-v4-flash">DeepSeek V4 Flash · 更快</option>
              <option value="deepseek-v4-pro">DeepSeek V4 Pro · 更强</option>
              <option value="deepseek-v4-flash-vision-exp">DeepSeek V4 Flash Vision · 图片案例（实验）</option>
            </select></label>
            <label className="key-field"><span>API Key</span><input type="password" value={deepseekApiKey} onChange={event => setDeepseekApiKey(event.target.value)} autoComplete="new-password" placeholder={deepseekSettings?.has_api_key ? '已保存到本机钥匙串；留空会保留' : '粘贴 DeepSeek API Key'} /></label>
            <button className="primary-button" onClick={saveDeepseek} disabled={savingDeepseek}>{savingDeepseek ? <LoaderCircle className="spin" /> : <Sparkles />}{savingDeepseek ? '正在测试 DeepSeek' : '测试并启用 DeepSeek'}</button>
            {deepseekSettings?.configured && <button className="secondary-button" onClick={recheckDeepseek} disabled={savingDeepseek}><RefreshCw />重新检测</button>}
          </div>
          <p className="security-note"><ShieldCheck />Key 只存在这台 Mac 的系统钥匙串，不会写入工作台数据库、导出文件或日志。<a href="https://api-docs.deepseek.com/quick_start/pricing/" target="_blank" rel="noreferrer">查看 DeepSeek 模型说明 <ExternalLink /></a></p>
        </section>

        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon green"><Sparkles /></span><div><h2>三个数字员工</h2><p>Skill 已内置进工作流，不需要逐个配置。</p></div></div>
          <div className="skill-grid">
            {skills.map(skill => <SkillCard skill={skill} key={skill.id} />)}
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-title"><span className="settings-icon amber"><FileText /></span><div><h2>我的 IP 信息</h2><p>选填一次，小红书会自动同步，朋友圈和抖音也会保持同一个人设与语气。</p></div></div>
          <textarea
            className="common-info"
            value={commonInfo}
            onChange={event => setCommonInfo(event.target.value)}
            placeholder={"可以写：\n我的身份、经历和账号定位\n我提供什么产品或服务\n主要客户是谁、他们常说什么\n我习惯怎样说话、哪些词不要用\n不能承诺或不能公开的内容"}
          />
          <div className="settings-footer"><span>{profile?.updated_at ? `上次保存：${formatDate(profile.updated_at)}` : '没有也可以直接创作'}</span><button className="secondary-button" onClick={saveInfo} disabled={savingInfo}><Save />{savingInfo ? '保存中' : '保存 IP 信息'}</button></div>
        </section>
      </div>
    </div>
  )
}

function StatusDot({ status }: { status: string }) {
  const online = status === 'verified'
  return <span className={`large-status ${online ? 'online' : ''}`}>{online ? <><CheckCircle2 />已连接</> : <><AlertTriangle />未验证</>}</span>
}

function SkillCard({ skill }: { skill: SkillStatus }) {
  const icons = { researcher: UserRoundSearch, writer: PenLine, editor: ShieldCheck }
  const labels = { researcher: '研究员', writer: '文案员工', editor: '主编' }
  const Icon = icons[skill.role]
  return (
    <article>
      <span><Icon /></span>
      <div><strong>{labels[skill.role]}</strong><p>{skill.description}</p><small><CheckCircle2 />Skill 已安装 · v{skill.version}</small></div>
    </article>
  )
}

export default App
