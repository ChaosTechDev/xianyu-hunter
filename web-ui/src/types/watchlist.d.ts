// 与后端 src/api/routes/watchlist.py、src/services/watch_service.py 的返回结构对齐

/** 关注商品状态：active 在售 / delisted 已下架 */
export type WatchStatus = 'active' | 'delisted'

/** 关注事件的类型 */
export type WatchEventType = 'price_drop' | 'low_price' | 'delisted' | 'relisted'

/** 咨询账号策略：后端当前仅实现账号池轮换 */
export type ConsultAccountStrategy = 'pool'

export interface WatchItem {
  id: number
  item_id: string
  result_filename: string
  keyword: string
  task_name: string
  title: string
  link: string
  image_url: string | null
  alert_price: number | null
  enabled: boolean
  last_price: number | null
  last_seen_at: string | null
  missing_runs: number
  status: WatchStatus
  created_at: string
  updated_at: string
  refresh_interval_minutes: number | null
  last_refresh_at: string | null
  next_refresh_at: string | null
  notify_price_drop: boolean
  notify_low_price: boolean
  notify_delisted: boolean
  notify_relisted: boolean
  consult_enabled: boolean
  consult_template: string
  consult_account_strategy: ConsultAccountStrategy
  last_consulted_at: string | null
}

export interface WatchEvent {
  id: number
  watch_item_id: number
  item_id: string
  title: string
  link: string
  event_type: WatchEventType
  event_label: string
  price: number | null
  previous_price: number | null
  detail: string
  is_read: boolean
  notified: boolean
  notification_results: Record<string, unknown>
  created_at: string
}

export interface WatchStats {
  total: number
  enabled: number
  delisted: number
  unread_events: number
  low_price_today: number
}

/** 单个关注商品的价格趋势点 */
export interface WatchTrendPoint {
  time: string
  day: string
  price: number
}

export interface WatchTrendSummary {
  current_price: number | null
  min_price: number | null
  max_price: number | null
  avg_price: number | null
  median_price: number | null
  observation_count: number
  latest_snapshot_at: string | null
  /** 采样点少于 2 个时为 true，前端据此提示数据稀疏 */
  is_sparse: boolean
}

export interface WatchItemTrend {
  scope: 'item'
  label: string
  watch_item: WatchItem
  summary: WatchTrendSummary
  points: WatchTrendPoint[]
}

/** 分类趋势里的价格汇总（来自 price_history_service._summarize_prices） */
export interface PriceSummary {
  sample_count: number
  avg_price: number | null
  median_price: number | null
  min_price: number | null
  max_price: number | null
}

export interface CategoryTrendPoint extends PriceSummary {
  day: string
}

export interface WatchCategoryTrend {
  scope: 'category'
  label: string
  market_summary: PriceSummary & { snapshot_time?: string | null }
  history_summary: PriceSummary & { unique_items: number }
  daily_trend: CategoryTrendPoint[]
  latest_snapshot_at: string | null
}

export interface WatchAiSummary {
  summary: string
  outlook: string
  signals: string[]
  risks: string[]
  generated_at: string
}

/** 咨询日志（consultation_logs 表） */
export interface ConsultationLog {
  id: number
  watch_item_id: number
  event_type: string
  account_path: string | null
  message: string
  status: 'sent' | 'uncertain' | 'failed' | string
  error: string | null
  created_at: string
}

/** 咨询发送返回（POST /api/watchlist/{id}/consult） */
export interface ConsultResult {
  status: 'sent' | 'uncertain' | 'failed' | 'skipped' | string
  account_path?: string
  message?: string
  error?: string
  reason?: string
}

/** 立即采集返回（POST /api/watchlist/{id}/refresh） */
export interface RefreshResult {
  message: string
  task_id?: number
}

/** POST /api/watchlist 请求体（对应 CreateWatchRequest） */
export interface CreateWatchPayload {
  item_id: string
  title: string
  link: string
  result_filename?: string
  keyword?: string
  task_name?: string
  image_url?: string | null
  alert_price?: number | null
  last_price?: number | null
  refresh_interval_minutes?: number | null
  notify_price_drop?: boolean
  notify_low_price?: boolean
  notify_delisted?: boolean
  notify_relisted?: boolean
  consult_enabled?: boolean | null
  consult_template?: string | null
  consult_account_strategy?: ConsultAccountStrategy
}

/** PATCH /api/watchlist/{id} 请求体（对应 UpdateWatchRequest，未提交字段保持不变） */
export interface UpdateWatchPayload {
  alert_price?: number | null
  enabled?: boolean
  refresh_interval_minutes?: number | null
  notify_price_drop?: boolean
  notify_low_price?: boolean
  notify_delisted?: boolean
  notify_relisted?: boolean
  consult_enabled?: boolean
  consult_template?: string | null
  consult_account_strategy?: ConsultAccountStrategy
}
