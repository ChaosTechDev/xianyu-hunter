import { computed, onMounted, ref } from 'vue'
import * as watchApi from '@/api/watchlist'
import { useWebSocket } from '@/composables/useWebSocket'
import type {
  CreateWatchPayload,
  UpdateWatchPayload,
  WatchAiSummary,
  WatchCategoryTrend,
  WatchEvent,
  WatchItem,
  WatchItemTrend,
  WatchStats,
} from '@/types/watchlist.d.ts'

/**
 * 关注列表状态与操作封装。
 * 复用上游 useTasks 的风格：本地 ref 缓存 + 静默刷新 + WebSocket 增量更新。
 */
export function useWatchlist() {
  const items = ref<WatchItem[]>([])
  const events = ref<WatchEvent[]>([])
  const stats = ref<WatchStats | null>(null)
  const isLoading = ref(false)
  const isEventsLoading = ref(false)
  const error = ref<Error | null>(null)
  const includeDisabled = ref(true)
  const unreadOnly = ref(false)

  // 正在执行中的操作标记，用于按行禁用按钮
  const busyIds = ref<Set<number>>(new Set())
  const refreshingIds = ref<Set<number>>(new Set())

  const { on } = useWebSocket()

  const unreadCount = computed(() => stats.value?.unread_events ?? events.value.filter((event) => !event.is_read).length)

  const activeItems = computed(() => items.value.filter((item) => item.enabled && item.status === 'active'))
  const delistedItems = computed(() => items.value.filter((item) => item.status === 'delisted'))

  function setBusy(id: number, busy: boolean) {
    const next = new Set(busyIds.value)
    if (busy) {
      next.add(id)
    } else {
      next.delete(id)
    }
    busyIds.value = next
  }

  function setRefreshing(id: number, refreshing: boolean) {
    const next = new Set(refreshingIds.value)
    if (refreshing) {
      next.add(id)
    } else {
      next.delete(id)
    }
    refreshingIds.value = next
  }

  function captureError(e: unknown) {
    error.value = e instanceof Error ? e : new Error(String(e))
  }

  async function fetchItems(options?: { silent?: boolean }) {
    if (!options?.silent) {
      isLoading.value = true
    }
    error.value = null
    try {
      items.value = await watchApi.listWatchItems(includeDisabled.value)
    } catch (e) {
      captureError(e)
      throw e
    } finally {
      if (!options?.silent) {
        isLoading.value = false
      }
    }
  }

  async function fetchStats(options?: { silent?: boolean }) {
    try {
      stats.value = await watchApi.getWatchStats()
    } catch (e) {
      captureError(e)
      if (!options?.silent) throw e
    }
  }

  async function fetchEvents(options?: { silent?: boolean }) {
    if (!options?.silent) {
      isEventsLoading.value = true
    }
    try {
      events.value = await watchApi.listWatchEvents({ unreadOnly: unreadOnly.value })
    } catch (e) {
      captureError(e)
      throw e
    } finally {
      if (!options?.silent) {
        isEventsLoading.value = false
      }
    }
  }

  /** 一次性刷新列表、统计与事件（并行请求） */
  async function refreshAll(options?: { silent?: boolean }) {
    await Promise.all([
      fetchItems(options),
      fetchStats(options),
      fetchEvents(options),
    ])
  }

  async function watch(payload: CreateWatchPayload): Promise<WatchItem> {
    error.value = null
    try {
      const created = await watchApi.createWatchItem(payload)
      await refreshAll({ silent: true })
      return created
    } catch (e) {
      captureError(e)
      throw e
    }
  }

  async function update(id: number, payload: UpdateWatchPayload): Promise<WatchItem | null> {
    setBusy(id, true)
    error.value = null
    try {
      const updated = await watchApi.updateWatchItem(id, payload)
      // 用返回值就地替换，避免整表刷新带来的闪烁
      const index = items.value.findIndex((item) => item.id === id)
      if (index >= 0 && updated) {
        items.value[index] = { ...items.value[index], ...updated }
      }
      return updated
    } catch (e) {
      captureError(e)
      throw e
    } finally {
      setBusy(id, false)
    }
  }

  async function remove(id: number) {
    setBusy(id, true)
    error.value = null
    try {
      await watchApi.deleteWatchItem(id)
      items.value = items.value.filter((item) => item.id !== id)
      await fetchStats({ silent: true })
    } catch (e) {
      captureError(e)
      throw e
    } finally {
      setBusy(id, false)
    }
  }

  /** 立即采集：命中 409 表示没有关联任务，402/503 表示启动失败，交由调用方提示 */
  async function refreshNow(id: number) {
    setRefreshing(id, true)
    try {
      return await watchApi.refreshWatchItem(id)
    } finally {
      setRefreshing(id, false)
    }
  }

  async function consult(id: number) {
    setBusy(id, true)
    try {
      return await watchApi.consultWatchItem(id)
    } finally {
      setBusy(id, false)
    }
  }

  async function aiSummary(id: number): Promise<WatchAiSummary> {
    setBusy(id, true)
    try {
      return await watchApi.generateWatchAiSummary(id)
    } finally {
      setBusy(id, false)
    }
  }

  async function itemTrend(id: number): Promise<WatchItemTrend> {
    return await watchApi.getWatchItemTrend(id)
  }

  async function categoryTrend(keyword: string): Promise<WatchCategoryTrend> {
    return await watchApi.getWatchCategoryTrend(keyword)
  }

  async function readAllEvents(): Promise<number> {
    const result = await watchApi.markAllWatchEventsRead()
    await Promise.all([fetchEvents({ silent: true }), fetchStats({ silent: true })])
    return result.updated ?? 0
  }

  async function readEvent(eventId: number) {
    setBusy(eventId, true)
    try {
      await watchApi.markWatchEventRead(eventId)
      const target = events.value.find((event) => event.id === eventId)
      if (target) {
        target.is_read = true
      }
      if (unreadOnly.value) {
        events.value = events.value.filter((event) => event.id !== eventId)
      }
      await fetchStats({ silent: true })
    } finally {
      setBusy(eventId, false)
    }
  }

  // 后端目前只广播 task_status_changed（见 src/app.py 的 _sync_task_runtime_status）。
  // 采集进程结束时，关注项的价格与事件才可能落库，所以在这里做一次静默刷新。
  on('task_status_changed', (data: { id: number; is_running: boolean }) => {
    if (!data?.is_running) {
      refreshAll({ silent: true }).catch(() => undefined)
    }
  })

  onMounted(() => {
    refreshAll().catch(() => undefined)
  })

  return {
    items,
    events,
    stats,
    isLoading,
    isEventsLoading,
    error,
    includeDisabled,
    unreadOnly,
    busyIds,
    refreshingIds,
    unreadCount,
    activeItems,
    delistedItems,
    fetchItems,
    fetchStats,
    fetchEvents,
    refreshAll,
    watch,
    update,
    remove,
    refreshNow,
    consult,
    aiSummary,
    itemTrend,
    categoryTrend,
    readAllEvents,
    readEvent,
  }
}
