import { http } from '@/lib/http'
import type {
  CategoryTrendPoint,
  ConsultResult,
  ConsultationLog,
  CreateWatchPayload,
  RefreshResult,
  UpdateWatchPayload,
  WatchAiSummary,
  WatchCategoryTrend,
  WatchEvent,
  WatchItem,
  WatchItemTrend,
  WatchStats,
} from '@/types/watchlist.d.ts'

/** 获取关注列表；include_disabled=false 时仅返回已启用的关注项 */
export async function listWatchItems(includeDisabled = true): Promise<WatchItem[]> {
  const data = await http('/api/watchlist', { params: { include_disabled: includeDisabled } })
  return data.items || []
}

/** 新增关注（item_id 已存在时后端会更新并重新启用） */
export async function createWatchItem(payload: CreateWatchPayload): Promise<WatchItem> {
  return await http('/api/watchlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 局部更新关注配置，仅提交发生变化的字段 */
export async function updateWatchItem(watchId: number, payload: UpdateWatchPayload): Promise<WatchItem> {
  return await http(`/api/watchlist/${watchId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 取消关注 */
export async function deleteWatchItem(watchId: number): Promise<{ message: string }> {
  return await http(`/api/watchlist/${watchId}`, { method: 'DELETE' })
}

/** 关注统计汇总 */
export async function getWatchStats(): Promise<WatchStats> {
  return await http('/api/watchlist/summary/stats')
}

/** 关注事件列表 */
export async function listWatchEvents(options: { unreadOnly?: boolean; limit?: number } = {}): Promise<WatchEvent[]> {
  const data = await http('/api/watchlist/events/list', {
    params: {
      unread_only: options.unreadOnly ?? false,
      limit: options.limit ?? 100,
    },
  })
  return data.items || []
}

/** 全部事件标记已读，返回更新的行数 */
export async function markAllWatchEventsRead(): Promise<{ updated: number }> {
  return await http('/api/watchlist/events/read-all', { method: 'POST' })
}

/** 单条事件标记已读 */
export async function markWatchEventRead(eventId: number): Promise<{ message: string }> {
  return await http(`/api/watchlist/events/${eventId}/read`, { method: 'POST' })
}

/** 单个关注商品的价格趋势 */
export async function getWatchItemTrend(watchId: number): Promise<WatchItemTrend> {
  return await http(`/api/watchlist/trends/item/${watchId}`)
}

/** 按关键词查询分类（市场）趋势 */
export async function getWatchCategoryTrend(keyword: string): Promise<WatchCategoryTrend> {
  return await http('/api/watchlist/trends/category', { params: { keyword } })
}

/** 让 AI 基于已落库的价格与事件生成行情解读 */
export async function generateWatchAiSummary(watchId: number): Promise<WatchAiSummary> {
  return await http(`/api/watchlist/ai-summary/${watchId}`, { method: 'POST' })
}

/** 咨询日志；不传 watchId 时返回全部关注的日志 */
export async function listConsultationLogs(options: { watchId?: number | null; limit?: number } = {}): Promise<ConsultationLog[]> {
  const params: Record<string, string | number> = { limit: options.limit ?? 100 }
  if (options.watchId !== undefined && options.watchId !== null) {
    params.watch_id = options.watchId
  }
  const data = await http('/api/watchlist/consultations/logs', { params })
  return data.items || []
}

/** 立即向卖家发送一次咨询（后端 force=true，跳过冷却） */
export async function consultWatchItem(watchId: number): Promise<ConsultResult> {
  return await http(`/api/watchlist/${watchId}/consult`, { method: 'POST' })
}

/** 立即采集：启动该关注项关联的采集任务 */
export async function refreshWatchItem(watchId: number): Promise<RefreshResult> {
  return await http(`/api/watchlist/${watchId}/refresh`, { method: 'POST' })
}

/**
 * 把逐条价格快照按天聚合，供折线图使用。
 * 同一商品在同一天可能多次采样，这里取当天均价与中位数，避免曲线在单日内来回跳动。
 */
export function aggregateTrendByDay(points: Array<{ day: string; price: number }>): CategoryTrendPoint[] {
  const grouped = new Map<string, number[]>()
  for (const point of points) {
    const day = point.day || ''
    const price = Number(point.price)
    if (!day || !Number.isFinite(price)) continue
    const bucket = grouped.get(day)
    if (bucket) {
      bucket.push(price)
    } else {
      grouped.set(day, [price])
    }
  }
  return [...grouped.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, prices]) => ({
      day,
      sample_count: prices.length,
      avg_price: round2(prices.reduce((sum, value) => sum + value, 0) / prices.length),
      median_price: round2(median(prices)),
      min_price: round2(Math.min(...prices)),
      max_price: round2(Math.max(...prices)),
    }))
}

function round2(value: number): number {
  return Math.round(value * 100) / 100
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b)
  const middle = Math.floor(sorted.length / 2)
  if (sorted.length === 0) return 0
  return sorted.length % 2 === 0
    ? ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2
    : (sorted[middle] ?? 0)
}
