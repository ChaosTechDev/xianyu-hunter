import { http } from '@/lib/http'

/** 单个目录的磁盘占用。 */
export interface StorageDirectoryUsage {
  bytes: number
  file_count: number
  human: string
  share_percent: number
}

export interface StorageUsage {
  directories: Record<string, StorageDirectoryUsage>
  total_bytes: number
  total_human: string
  total_files: number
  largest_directory: string
}

/** 各表最早一条记录的时间；`null` 表示该表为空。 */
export interface OldestRecords {
  result_items: string | null
  price_snapshots: string | null
  watch_events: string | null
  ai_usage_stats: string | null
  consultation_logs: string | null
  logs: string | null
  [key: string]: string | null
}

export interface StorageUsageResponse {
  usage: StorageUsage
  oldest_records: OldestRecords
  generated_at: string
}

export interface RetentionPlan {
  generated_at: string
  dry_run?: boolean
  usage?: StorageUsage
  oldest_records?: OldestRecords
  [key: string]: unknown
}

export interface RetentionPlanResponse {
  plan: RetentionPlan
  dry_run: boolean
}

/** 单张表的删除结果。`skipped` 为真表示该表因未启用/无表而被跳过。 */
export interface RetentionTableResult {
  deleted: number
  skipped?: boolean
  reason?: string
}

/** 单个目录的清理结果。 */
export interface RetentionDirectoryResult {
  deleted: string[]
  kept: number
  skipped: number
  freed_bytes: number
}

export interface RetentionExecuteResponse {
  dry_run: boolean
  /** 各表合计删除行数（不是逐表字典）。 */
  deleted_rows: number
  /** 删除的文件总数。 */
  deleted_files: number
  freed_bytes: number
  freed_human: string | null
  /** 逐表明细，键为表名。 */
  database: Record<string, RetentionTableResult>
  /** 逐目录明细，键为目录名。 */
  files: Record<string, RetentionDirectoryResult>
  errors: string[]
  note: string
}

export interface RetentionExecutePayload {
  confirm: boolean
  result_items_days?: number
  price_snapshots_days?: number
  watch_events_days?: number
  logs_days?: number
  ai_usage_days?: number
}

/** 只读：磁盘占用与最早记录时间。 */
export async function getStorageUsage(): Promise<StorageUsageResponse> {
  return await http('/api/storage/usage')
}

/** 只读：预览清理计划（界面据此展示「将要删除什么」）。 */
export async function getRetentionPlan(): Promise<RetentionPlanResponse> {
  return await http('/api/storage/retention/plan')
}

/**
 * 执行清理。**`confirm` 必须显式为 true 才会真正删除**，
 * 否则后端只做 dry-run 并如实返回 `dry_run: true`。
 */
export async function executeRetention(
  payload: RetentionExecutePayload,
): Promise<RetentionExecuteResponse> {
  return await http('/api/storage/retention/execute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}
