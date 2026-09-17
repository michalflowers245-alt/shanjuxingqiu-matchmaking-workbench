export type Workspace = { id: string; name: string; description: string; color: string }
export type ContentType = {
  key: string
  label: string
  platforms: string[]
  length: [number, number]
  structure: string
  rules: string[]
}

export type SkillStatus = {
  id: string
  name: string
  role: 'researcher' | 'writer' | 'editor'
  version: string
  description: string
  installed: boolean
  enabled: boolean
  status: string
}

export type ModelStatus = {
  usable: boolean
  status: string
  search_status: string
  search_mode: string
  message?: string
  active_provider?: string
  active_model?: string
  active_label?: string
  profiles: Array<Record<string, unknown>>
}

export type Task = {
  id: string
  workspace_id: string
  content_type: string
  platform: string
  topic: string
  goal: string
  offer: string
  constraints: Record<string, unknown>
  web_research: boolean
  status: string
  stage: string
  latest_score?: number
  revision_round: number
  last_error?: string
  created_at: string
  updated_at?: string
  source_case_id?: string | null
}

export type Source = {
  id: string
  source_key: string
  kind: string
  title: string
  url: string
  excerpt: string
  credibility: number
  published_at?: string
  metadata?: {
    search_mode?: string
    searched_at?: string
    platform?: 'xiaohongshu' | 'douyin' | 'web'
    author?: string
    metrics?: Record<string, number | string | null>
    xhs_extraction?: XiaohongshuExtraction
  }
}

export type XiaohongshuExtraction = {
  id: string
  workspace_id: string
  url: string
  post_key: string
  search_keyword: string
  title: string
  author: string
  published_at?: string | null
  body: string
  image_text: string
  video_subtitle: string
  video_speech: string
  merged_copy: string
  tags: string[]
  metrics: Record<string, number | string | null>
  screenshot_url: string
  status: string
  completeness: number
  field_sources: Record<string, string>
  ocr_status: string
  ocr_message: string
  asr_status: string
  asr_message: string
  failure_stage: string
  error_message: string
  diagnostics: Record<string, unknown>
  retry_count: number
  extracted_at: string
  updated_at: string
  body_raw?: string | null
  body_normalized?: string | null
  body_source?: string
  body_evidence?: Record<string, unknown>
  archive_status?: string
  verification_status?: string
  content_sha256?: string | null
  latest_archive_id?: string | null
  expected_asset_count?: number | null
  saved_asset_count?: number
  archive?: XiaohongshuArchive | null
  archive_job?: XiaohongshuArchiveJob | null
}

export type XiaohongshuArchiveAsset = {
  id: string
  role: 'carousel' | 'cover' | string
  ordinal: number
  mime_detected?: string | null
  extension?: string | null
  width?: number | null
  height?: number | null
  byte_size?: number | null
  sha256?: string | null
  status: string
  error_code?: string | null
  download_url?: string
}

export type XiaohongshuArchive = {
  id: string
  extraction_id: string
  post_id: string
  observed_post_id: string
  body_raw?: string | null
  body_normalized?: string | null
  body_source: string
  archive_status: string
  verification_status: string
  content_sha256?: string | null
  manifest_sha256?: string | null
  captured_at: string
  assets: XiaohongshuArchiveAsset[]
}

export type XiaohongshuArchiveJob = {
  id: string
  extraction_id: string
  status: string
  stage: string
  error_code?: string | null
  progress: {
    message?: string
    archive_id?: string
    expected_asset_count?: number | null
    saved_asset_count?: number
    missing_ordinals?: number[]
  }
  updated_at: string
}

export type DraftPackage = {
  title: string
  alternative_titles: string[]
  body: string
  cta: string
  tags?: string[]
  claims?: Array<{ claim: string; evidence_ids: string[]; status: string }>
  platform_variants?: Record<string, string>
  conversion_structure?: Record<string, string>
  deliverables?: ProductDeliverables
}

export type MomentsPost = {
  day: number
  time: string
  role: string
  purpose: string
  content: string
  follow_up: string
}

export type ProductDeliverables = {
  moments_posts: MomentsPost[]
  xiaohongshu_publish: { title: string; body: string; tags: string[] }
  douyin_script: { hook: string; script: string; emotion_beats: string[]; ending: string }
  life_case_variants?: LifeCaseVariant[]
}

export type LifeCaseVariant = {
  angle: 'daily' | 'reflection' | 'soft_business' | string
  label: string
  body: string
  rationale?: string
  recommended: boolean
}

export type LifeCaseMedia = {
  id: string
  display_name: string
  mime_type: string
  byte_size: number
  width?: number | null
  height?: number | null
  sha256?: string
  ordinal: number
  media_url: string
}

export type LifeCase = {
  id: string
  workspace_id: string
  title: string
  note_text: string
  occurred_at?: string | null
  sync_ip: boolean
  archived: boolean
  created_at: string
  updated_at?: string
  media: LifeCaseMedia[]
  latest_task_id?: string | null
  latest_task_status?: string | null
}

export type Draft = {
  id: string
  task_id: string
  version: number
  origin: string
  package: DraftPackage
  body_text: string
  is_final: boolean
  created_at: string
}

export type Review = {
  id: string
  draft_version_id: string
  round: number
  scores: Record<string, number>
  total_score: number
  blocking_issues: string[]
  instructions: string[]
  change_summary: string
  conversion_structure: Record<string, string>
  risks: string[]
  decision: string
}

export type TaskArtifact = {
  id: string
  task_id: string
  artifact_type: string
  round: number
  payload: Record<string, unknown>
  body_text: string
  created_at: string
}

export type TaskEvent = {
  id: number
  stage: string
  event_type: string
  message: string
  created_at: string
}

export type TaskDetail = {
  task: Task
  research: null | { packet: Record<string, unknown> }
  sources: Source[]
  drafts: Draft[]
  reviews: Review[]
  events: TaskEvent[]
  artifacts: TaskArtifact[]
  assets: Array<Record<string, unknown>>
  expression_checks: Array<Record<string, unknown>>
  source_case?: LifeCase | null
}

export type Bootstrap = {
  workspaces: Workspace[]
  content_types: ContentType[]
  model_status: ModelStatus
  skills: SkillStatus[]
  publishing_enabled: boolean
}

export type LocalProxySettings = {
  base_url: string
  model: { status: string; model: string; message: string; latency_ms?: number }
  search: { status: string; mode: string; message: string; source_count?: number }
  checked_at: string | null
}

export type DeepSeekSettings = {
  model: { status: string; model: string; message: string; latency_ms?: number }
  base_url: string
  has_api_key: boolean
  configured: boolean
  active: boolean
  checked_at: string | null
  search?: { status: string; mode: string; message: string; source_count?: number }
}

export type WorkspaceProfile = {
  workspace_id: string
  profile: Record<string, unknown>
  completed_fields: string[]
  updated_at: string
}

/** `web` is the public-trend source used by the profile-aware daily radar. */
export type RadarPlatform = 'xiaohongshu' | 'douyin' | 'web'

export type TopicMonitor = {
  id: string
  workspace_id: string
  query: string
  platforms: RadarPlatform[]
  interval_hours: number
  enabled: boolean
  last_run_at?: string | null
  next_run_at?: string | null
  last_status: string
  last_error?: string | null
  latest_item_count?: number
  /** Older workspaces do not have this field, so keep it optional in the UI. */
  monitor_kind?: 'manual' | 'ip_daily' | string
  created_at: string
  updated_at: string
}

export type TopicRadarPlatformSummary = {
  platform: RadarPlatform
  status: 'success' | 'error' | 'needs_setup' | 'manual_search' | string
  count: number
  source_mode: string
  message?: string
  search_url?: string
}

export type TopicRadarItem = {
  id: string
  run_id: string
  monitor_id: string
  workspace_id: string
  platform: RadarPlatform
  source_mode: string
  source_key: string
  title: string
  url: string
  excerpt: string
  author?: string
  published_at?: string | null
  metrics: Record<string, number | string | null>
  raw: Record<string, unknown>
  is_new: boolean
  discovered_at: string
}

export type TopicRadarRun = {
  id: string
  monitor_id: string
  workspace_id: string
  status: string
  summary: {
    query?: string
    total?: number
    new_count?: number
    platforms?: TopicRadarPlatformSummary[]
    /** A short list of content angles picked from the profile-aware daily scan. */
    recommendations?: TopicRadarRecommendation[]
    recommended_topics?: TopicRadarRecommendation[]
  }
  error_message?: string | null
  started_at: string
  completed_at?: string | null
  items: TopicRadarItem[]
}

export type TopicRadarRecommendation = {
  title?: string
  topic?: string
  angle?: string
  reason?: string
  why?: string
  /** Human-readable account lane, such as a local matchmaking creator or a female dating account. */
  persona?: string
  pillar?: string
  search_query?: string
  /** Only official, platform-native search actions belong on creator-facing radar cards. */
  platform_search_links?: PlatformSearchLink[]
  public_signal_count?: number
  source_url?: string
  source_urls?: string[]
  source_title?: string
  evidence_url?: string
  evidence_platform?: 'xiaohongshu' | 'douyin' | string
  visible_metrics?: Record<string, number | string | null>
  score?: number
}

export type PlatformSearchLink = {
  platform: 'douyin' | 'xiaohongshu'
  label: string
  url: string
  source_mode?: 'official_search' | string
}

/** Response of the one-off "search a topic, then create" flow.
 * The optional aliases let the UI remain compatible while the local service
 * evolves without ever treating an untyped network response as a hard error.
 */
export type TopicSearchResponse = {
  topic?: string
  sources?: Source[]
  items?: Source[]
  angles?: TopicRadarRecommendation[]
  recommendations?: TopicRadarRecommendation[]
  platform_search_links?: PlatformSearchLink[]
  search_mode?: string
  message?: string
  warning?: string | null
  visible_source_count?: number
  platforms?: TopicRadarPlatformSummary[]
}

export type PlatformBrowserStatus = {
  available: boolean
  running: boolean
  mode: string
  message: string
  profile_persistent?: boolean
  session_storage?: string
}
