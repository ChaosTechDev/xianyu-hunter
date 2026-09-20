<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useWatchlist } from '@/composables/useWatchlist'
import * as watchApi from '@/api/watchlist'
import type {
  ConsultationLog,
  UpdateWatchPayload,
  WatchAiSummary,
  WatchCategoryTrend,
  WatchEvent,
  WatchItem,
  WatchItemTrend,
} from '@/types/watchlist.d.ts'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import PriceTrendChart from '@/components/results/PriceTrendChart.vue'
import CategoryTrendChart from '@/components/watchlist/CategoryTrendChart.vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { toast } from '@/components/ui/toast'
import { formatPrice } from '@/lib/format'
import { formatRelativeTimeFromNow } from '@/i18n'
import { Activity, BellRing, ExternalLink, LineChart, MessageSquare, RefreshCw, Sparkles, Trash2, Pencil } from 'lucide-vue-next'

const { t } = useI18n()
const router = useRouter()

/** 跳转到官方搜索页补充关注项 */
function goSearch() {
  router.push('/search')
}

const {
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
  fetchItems,
  fetchEvents,
  refreshNow,
  consult,
  aiSummary,
  itemTrend,
  categoryTrend,
  readAllEvents,
  readEvent,
  update,
  remove,
} = useWatchlist()

const activeTab = ref('items')
const filterKeyword = ref('')

// 编辑对话框状态
const isEditDialogOpen = ref(false)
const isEditSubmitting = ref(false)
const editingItem = ref<WatchItem | null>(null)
const editForm = ref<{
  alert_price: string
  enabled: boolean
  refresh_interval_minutes: string
  notify_price_drop: boolean
  notify_low_price: boolean
  notify_delisted: boolean
  notify_relisted: boolean
  consult_enabled: boolean
  consult_template: string
}>({
  alert_price: '',
  enabled: true,
  refresh_interval_minutes: '',
  notify_price_drop: true,
  notify_low_price: true,
  notify_delisted: true,
  notify_relisted: true,
  consult_enabled: false,
  consult_template: '',
})

// 删除对话框状态
const isDeleteDialogOpen = ref(false)
const deletingItem = ref<WatchItem | null>(null)

// AI 解读对话框状态
const isAiDialogOpen = ref(false)
const aiLoading = ref(false)
const aiSummaryData = ref<WatchAiSummary | null>(null)
const aiTargetTitle = ref('')

// 趋势对话框状态
const isTrendDialogOpen = ref(false)
const trendLoading = ref(false)
const trendMode = ref<'item' | 'category'>('item')
const itemTrendData = ref<WatchItemTrend | null>(null)
const categoryTrendData = ref<WatchCategoryTrend | null>(null)
const trendKeyword = ref('')

// 咨询记录
const consultations = ref<ConsultationLog[]>([])
const consultationsLoading = ref(false)
const consultationWatchId = ref<number | null>(null)

const filteredItems = computed(() => {
  const needle = filterKeyword.value.trim().toLowerCase()
  if (!needle) return items.value
  return items.value.filter((item) =>
    [item.title, item.keyword, item.task_name, item.item_id]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(needle))
  )
})

const statCards = computed(() => [
  { key: 'total', label: t('watchlist.stats.total'), value: stats.value?.total ?? 0 },
  { key: 'enabled', label: t('watchlist.stats.enabled'), value: stats.value?.enabled ?? 0 },
  { key: 'unread', label: t('watchlist.stats.unreadEvents'), value: unreadCount.value },
  { key: 'lowPrice', label: t('watchlist.stats.lowPriceToday'), value: stats.value?.low_price_today ?? 0 },
  { key: 'delisted', label: t('watchlist.stats.delisted'), value: stats.value?.delisted ?? 0 },
])

/** 分类趋势按天聚合后的折线数据 */
const categoryPoints = computed(() => {
  if (!categoryTrendData.value) return []
  return categoryTrendData.value.daily_trend ?? []
})

/** 单商品趋势的均值/中位数序列，直接复用 PriceTrendChart 需要的结构 */
const itemChartPoints = computed(() => {
  if (!itemTrendData.value) return []
  const points = itemTrendData.value.points ?? []
  if (points.length === 0) return []
  return watchApi.aggregateTrendByDay(points)
})

function isBusy(id: number) {
  return busyIds.value.has(id)
}

function isRefreshing(id: number) {
  return refreshingIds.value.has(id)
}

/** 切换“显示已停用”后重新拉取列表（include_disabled 是后端查询参数） */
function handleToggleIncludeDisabled(value: boolean) {
  includeDisabled.value = value
  fetchItems().catch(() => undefined)
}

/** 切换“仅看未读”后重新拉取事件（unread_only 是后端查询参数） */
function handleToggleUnreadOnly(value: boolean) {
  unreadOnly.value = value
  fetchEvents().catch(() => undefined)
}

function openItemLink(link: string) {
  if (!link) return
  window.open(link, '_blank', 'noopener,noreferrer')
}

function openEditDialog(item: WatchItem) {
  editingItem.value = item
  editForm.value = {
    alert_price: item.alert_price === null || item.alert_price === undefined ? '' : String(item.alert_price),
    enabled: item.enabled,
    refresh_interval_minutes:
      item.refresh_interval_minutes === null || item.refresh_interval_minutes === undefined
        ? ''
        : String(item.refresh_interval_minutes),
    notify_price_drop: item.notify_price_drop,
    notify_low_price: item.notify_low_price,
    notify_delisted: item.notify_delisted,
    notify_relisted: item.notify_relisted,
    consult_enabled: item.consult_enabled,
    consult_template: item.consult_template || '',
  }
  isEditDialogOpen.value = true
}

/** 只把发生变化的字段放进 PATCH body，避免覆盖后端未返回的配置 */
function buildUpdatePayload(item: WatchItem): UpdateWatchPayload {
  const form = editForm.value
  const payload: UpdateWatchPayload = {}

  const originalAlert = item.alert_price === null || item.alert_price === undefined ? '' : String(item.alert_price)
  if (form.alert_price.trim() !== originalAlert) {
    payload.alert_price = form.alert_price.trim() === '' ? null : Number(form.alert_price.trim())
  }

  const originalInterval =
    item.refresh_interval_minutes === null || item.refresh_interval_minutes === undefined
      ? ''
      : String(item.refresh_interval_minutes)
  if (form.refresh_interval_minutes.trim() !== originalInterval) {
    payload.refresh_interval_minutes = form.refresh_interval_minutes.trim() === '' ? null : Number(form.refresh_interval_minutes.trim())
  }

  if (form.enabled !== item.enabled) payload.enabled = form.enabled
  if (form.notify_price_drop !== item.notify_price_drop) payload.notify_price_drop = form.notify_price_drop
  if (form.notify_low_price !== item.notify_low_price) payload.notify_low_price = form.notify_low_price
  if (form.notify_delisted !== item.notify_delisted) payload.notify_delisted = form.notify_delisted
  if (form.notify_relisted !== item.notify_relisted) payload.notify_relisted = form.notify_relisted
  if (form.consult_enabled !== item.consult_enabled) payload.consult_enabled = form.consult_enabled
  if (form.consult_template !== (item.consult_template || '')) {
    payload.consult_template = form.consult_template
  }

  return payload
}

async function handleSubmitEdit() {
  if (!editingItem.value) return
  const payload = buildUpdatePayload(editingItem.value)

  // 本地校验：提醒价必须是合法非负数
  if (payload.alert_price !== undefined && payload.alert_price !== null && !Number.isFinite(payload.alert_price)) {
    toast({ title: t('watchlist.toasts.updateFailed'), description: t('watchlist.editDialog.alertPricePlaceholder'), variant: 'destructive' })
    return
  }
  if (payload.refresh_interval_minutes !== undefined && payload.refresh_interval_minutes !== null && !Number.isFinite(payload.refresh_interval_minutes)) {
    toast({ title: t('watchlist.toasts.updateFailed'), description: t('watchlist.editDialog.refreshIntervalPlaceholder'), variant: 'destructive' })
    return
  }

  if (Object.keys(payload).length === 0) {
    isEditDialogOpen.value = false
    return
  }

  isEditSubmitting.value = true
  try {
    await update(editingItem.value.id, payload)
    toast({ title: t('watchlist.toasts.updated') })
    isEditDialogOpen.value = false
  } catch (e) {
    toast({ title: t('watchlist.toasts.updateFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    isEditSubmitting.value = false
  }
}

function openDeleteDialog(item: WatchItem) {
  deletingItem.value = item
  isDeleteDialogOpen.value = true
}

async function handleConfirmDelete() {
  if (!deletingItem.value) return
  try {
    await remove(deletingItem.value.id)
    toast({ title: t('watchlist.toasts.removed') })
    isDeleteDialogOpen.value = false
  } catch (e) {
    toast({ title: t('watchlist.toasts.removeFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    deletingItem.value = null
  }
}

async function handleToggleEnabled(item: WatchItem, enabled: boolean) {
  const previous = item.enabled
  item.enabled = enabled
  try {
    await update(item.id, { enabled })
    toast({ title: t('watchlist.toasts.updated') })
  } catch (e) {
    item.enabled = previous
    toast({ title: t('watchlist.toasts.updateFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

async function handleRefreshNow(item: WatchItem) {
  try {
    const result = await refreshNow(item.id)
    toast({ title: result.message || t('watchlist.toasts.refreshStarted') })
  } catch (e) {
    // 后端用 409 表示没有关联任务，503 表示启动失败，按状态码给出可操作提示
    const message = (e as Error).message || ''
    const description = message.includes('关联采集任务')
      ? t('watchlist.toasts.refreshNoTask')
      : message
    toast({ title: t('watchlist.toasts.refreshFailed'), description, variant: 'destructive' })
  }
}

async function handleConsult(item: WatchItem) {
  try {
    const result = await consult(item.id)
    if (result.status === 'sent') {
      toast({ title: t('watchlist.toasts.consultSent') })
    } else if (result.status === 'uncertain') {
      toast({ title: t('watchlist.toasts.consultUncertain') })
    } else if (result.status === 'skipped') {
      toast({ title: t('watchlist.toasts.consultSkipped', { reason: result.reason || '' }) })
    } else {
      toast({ title: t('watchlist.toasts.consultFailed'), description: result.error || '', variant: 'destructive' })
    }
    await fetchConsultations()
  } catch (e) {
    toast({ title: t('watchlist.toasts.consultFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

async function handleAiSummary(item: WatchItem) {
  aiTargetTitle.value = item.title
  aiSummaryData.value = null
  isAiDialogOpen.value = true
  aiLoading.value = true
  try {
    aiSummaryData.value = await aiSummary(item.id)
  } catch (e) {
    const message = (e as Error).message || ''
    toast({
      title: t('watchlist.toasts.aiFailed'),
      description: message.includes('AI') ? t('watchlist.aiDialog.notConfigured') : message,
      variant: 'destructive',
    })
    isAiDialogOpen.value = false
  } finally {
    aiLoading.value = false
  }
}

async function handleOpenTrend(item: WatchItem) {
  trendMode.value = 'item'
  trendKeyword.value = item.keyword || ''
  categoryTrendData.value = null
  itemTrendData.value = null
  aiTargetTitle.value = item.title
  isTrendDialogOpen.value = true
  trendLoading.value = true
  try {
    itemTrendData.value = await itemTrend(item.id)
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    trendLoading.value = false
  }
}

async function loadCategoryTrend() {
  const keyword = trendKeyword.value.trim()
  if (!keyword) {
    toast({ title: t('watchlist.trends.emptyKeyword'), variant: 'destructive' })
    return
  }
  trendMode.value = 'category'
  trendLoading.value = true
  try {
    categoryTrendData.value = await categoryTrend(keyword)
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    trendLoading.value = false
  }
}

/**
 * 趋势弹窗内切换 单商品 / 分类市场 两个页签。
 * 切到分类时按该关注项的关键词查询，若没有关键词则回退到已输入的搜索词。
 */
async function loadTrendTab(value: string | number) {
  if (value === 'item') {
    trendMode.value = 'item'
    return
  }
  trendMode.value = 'category'
  if (categoryTrendData.value) return
  const keyword = (trendKeyword.value || '').trim()
  if (!keyword) {
    toast({ title: t('watchlist.trends.emptyKeyword'), variant: 'destructive' })
    return
  }
  trendLoading.value = true
  try {
    categoryTrendData.value = await categoryTrend(keyword)
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    trendLoading.value = false
  }
}

async function fetchConsultations(watchId: number | null = consultationWatchId.value) {
  consultationsLoading.value = true
  try {
    consultations.value = await watchApi.listConsultationLogs({ watchId })
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    consultationsLoading.value = false
  }
}

async function handleReadAll() {
  try {
    const count = await readAllEvents()
    toast({ title: t('watchlist.toasts.eventsReadAll', { count }) })
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

async function handleReadEvent(event: WatchEvent) {
  if (event.is_read) return
  try {
    await readEvent(event.id)
  } catch (e) {
    toast({ title: t('watchlist.toasts.loadedFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

function eventTone(type: WatchEvent['event_type']) {
  switch (type) {
    case 'low_price':
      return 'bg-rose-100 text-rose-700 border-rose-200'
    case 'price_drop':
      return 'bg-emerald-100 text-emerald-700 border-emerald-200'
    case 'delisted':
      return 'bg-slate-200 text-slate-700 border-slate-300'
    case 'relisted':
      return 'bg-sky-100 text-sky-700 border-sky-200'
    default:
      return 'bg-slate-100 text-slate-600 border-slate-200'
  }
}

function consultationTone(status: string) {
  switch (status) {
    case 'sent':
      return 'bg-emerald-100 text-emerald-700 border-emerald-200'
    case 'uncertain':
      return 'bg-amber-100 text-amber-700 border-amber-200'
    default:
      return 'bg-rose-100 text-rose-700 border-rose-200'
  }
}

function consultationStatusLabel(status: string) {
  switch (status) {
    case 'sent':
      return t('watchlist.consultations.statusSent')
    case 'uncertain':
      return t('watchlist.consultations.statusUncertain')
    default:
      return t('watchlist.consultations.statusFailed')
  }
}

function notifyBadges(item: WatchItem) {
  return [
    { key: 'priceDrop', label: t('watchlist.notify.priceDrop'), on: item.notify_price_drop },
    { key: 'lowPrice', label: t('watchlist.notify.lowPrice'), on: item.notify_low_price },
    { key: 'delisted', label: t('watchlist.notify.delisted'), on: item.notify_delisted },
    { key: 'relisted', label: t('watchlist.notify.relisted'), on: item.notify_relisted },
    { key: 'consult', label: t('watchlist.notify.consult'), on: item.consult_enabled },
  ]
}

/** 编辑弹窗里的四个通知开关，统一走这个 setter 避免在模板里做动态索引 */
type NotifyFlagKey = 'notify_price_drop' | 'notify_low_price' | 'notify_delisted' | 'notify_relisted'

function setNotifyFlag(key: NotifyFlagKey, value: boolean) {
  editForm.value[key] = value
}

const notifyOptions: Array<{ key: NotifyFlagKey; labelKey: string }> = [
  { key: 'notify_price_drop', labelKey: 'watchlist.notify.priceDrop' },
  { key: 'notify_low_price', labelKey: 'watchlist.notify.lowPrice' },
  { key: 'notify_delisted', labelKey: 'watchlist.notify.delisted' },
  { key: 'notify_relisted', labelKey: 'watchlist.notify.relisted' },
]

function scheduleText(item: WatchItem): string {
  if (!item.refresh_interval_minutes) return t('watchlist.table.noInterval')
  return t('watchlist.table.interval', { minutes: item.refresh_interval_minutes })
}

onMounted(() => {
  fetchConsultations(null).catch(() => undefined)
})
</script>

<template>
  <div>
    <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 class="text-2xl font-bold text-gray-800">{{ t('watchlist.title') }}</h1>
        <p class="text-sm text-gray-500 mt-1">{{ t('watchlist.description') }}</p>
      </div>
      <Button variant="outline" size="sm" :disabled="isLoading" @click="() => { fetchItems(); fetchEvents() }">
        <RefreshCw class="mr-2 h-4 w-4" :class="{ 'animate-spin': isLoading }" />
        {{ t('common.refresh') }}
      </Button>
    </div>

    <!-- 统计卡片 -->
    <div class="mb-6 grid grid-cols-2 gap-3 md:grid-cols-5">
      <Card v-for="card in statCards" :key="card.key" class="app-surface border-none">
        <CardContent class="p-4">
          <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ card.label }}</p>
          <p class="mt-1 text-2xl font-black text-slate-800">{{ card.value }}</p>
        </CardContent>
      </Card>
    </div>

    <div v-if="error" class="app-alert-error mb-4" role="alert">
      <strong class="font-bold">{{ t('common.error') }}</strong>
      <span class="block sm:inline">{{ error.message }}</span>
    </div>

    <Tabs v-model="activeTab" default-value="items">
      <TabsList class="mb-4 flex-wrap">
        <TabsTrigger value="items">{{ t('watchlist.tabs.items') }}</TabsTrigger>
        <TabsTrigger value="events">
          {{ t('watchlist.tabs.events') }}
          <span v-if="unreadCount > 0" class="ml-1.5 rounded-full bg-rose-500 px-1.5 text-[10px] font-bold text-white">{{ unreadCount }}</span>
        </TabsTrigger>
        <TabsTrigger value="trends">{{ t('watchlist.tabs.trends') }}</TabsTrigger>
        <TabsTrigger value="consultations">{{ t('watchlist.tabs.consultations') }}</TabsTrigger>
      </TabsList>

      <!-- 关注商品 -->
      <TabsContent value="items">
        <Card class="app-surface border-none">
          <CardHeader class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle>{{ t('watchlist.tabs.items') }}</CardTitle>
              <CardDescription>{{ t('watchlist.description') }}</CardDescription>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              <Input v-model="filterKeyword" class="h-9 w-full sm:w-56" :placeholder="t('watchlist.filters.keyword')" />
              <label class="flex items-center gap-2 text-sm text-slate-600">
                <Switch :model-value="includeDisabled" @update:model-value="handleToggleIncludeDisabled" />
                {{ t('watchlist.filters.includeDisabled') }}
              </label>
            </div>
          </CardHeader>
          <CardContent>
            <div v-if="isLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>
            <div v-else-if="filteredItems.length === 0" class="py-12 text-center">
              <p class="text-sm font-semibold text-slate-600">{{ t('watchlist.table.empty') }}</p>
              <p class="mt-1 text-xs text-slate-400">{{ t('watchlist.table.emptyHint') }}</p>
              <Button class="mt-4" variant="outline" @click="goSearch">{{ t('search.title') }}</Button>
            </div>

            <div v-else class="space-y-3">
              <article
                v-for="item in filteredItems"
                :key="item.id"
                class="app-surface-subtle p-4"
              >
                <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                  <div class="flex min-w-0 flex-1 gap-3">
                    <div class="h-16 w-16 flex-shrink-0 overflow-hidden rounded-xl border border-slate-200 bg-slate-50">
                      <img v-if="item.image_url" :src="item.image_url" :alt="item.title" class="h-full w-full object-cover" loading="lazy" />
                      <div v-else class="flex h-full w-full items-center justify-center text-[10px] text-slate-400">{{ t('search.card.noImage') }}</div>
                    </div>
                    <div class="min-w-0 flex-1">
                      <div class="flex items-start gap-2">
                        <h3 class="line-clamp-2 flex-1 text-sm font-bold text-slate-800">{{ item.title }}</h3>
                        <Badge
                          :class="item.status === 'delisted' ? 'border-slate-300 bg-slate-100 text-slate-600' : 'border-emerald-200 bg-emerald-50 text-emerald-700'"
                          variant="outline"
                        >
                          {{ item.status === 'delisted' ? t('watchlist.status.delisted') : t('watchlist.status.active') }}
                        </Badge>
                      </div>
                      <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                        <span v-if="item.keyword">{{ t('common.keyword') }}：{{ item.keyword }}</span>
                        <span v-if="item.task_name">{{ item.task_name }}</span>
                        <span>{{ t('watchlist.table.lastSeen', { time: item.last_seen_at ? formatRelativeTimeFromNow(item.last_seen_at) : t('watchlist.table.neverSeen') }) }}</span>
                        <span v-if="item.missing_runs > 0" class="text-amber-600">{{ t('watchlist.table.missingRuns', { count: item.missing_runs }) }}</span>
                      </div>
                      <div class="mt-2 flex flex-wrap items-center gap-1.5">
                        <Badge
                          v-for="badge in notifyBadges(item)"
                          :key="badge.key"
                          variant="outline"
                          :class="badge.on ? 'border-primary/30 bg-primary/5 text-primary' : 'border-slate-200 bg-slate-50 text-slate-400'"
                        >
                          {{ badge.label }}
                        </Badge>
                      </div>
                    </div>
                  </div>

                  <div class="flex flex-col gap-2 lg:w-64 lg:flex-shrink-0 lg:text-right">
                    <div>
                      <p class="text-lg font-black text-slate-800">{{ formatPrice(item.last_price) }}</p>
                      <p class="text-xs text-slate-500">
                        {{ t('watchlist.table.alertPriceLabel') }}：
                        <span :class="item.alert_price === null ? 'text-slate-400' : 'font-semibold text-rose-600'">
                          {{ item.alert_price === null ? t('watchlist.table.noAlertPrice') : formatPrice(item.alert_price) }}
                        </span>
                      </p>
                    </div>
                    <p class="text-xs text-slate-500">{{ scheduleText(item) }}</p>
                    <p v-if="item.next_refresh_at" class="text-xs text-slate-400">
                      {{ t('watchlist.table.nextRefresh', { time: formatRelativeTimeFromNow(item.next_refresh_at) }) }}
                    </p>
                    <div class="mt-1 flex flex-wrap gap-2 lg:justify-end">
                      <label class="flex items-center gap-2 text-xs text-slate-500">
                        <Switch
                          :model-value="item.enabled"
                          :disabled="isBusy(item.id)"
                          @update:model-value="(value: boolean) => handleToggleEnabled(item, value)"
                        />
                        {{ item.enabled ? t('watchlist.status.enabled') : t('watchlist.status.disabled') }}
                      </label>
                    </div>
                  </div>
                </div>

                <div class="mt-3 flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" :disabled="isRefreshing(item.id)" @click="handleRefreshNow(item)">
                    <RefreshCw class="mr-1.5 h-3.5 w-3.5" :class="{ 'animate-spin': isRefreshing(item.id) }" />
                    {{ isRefreshing(item.id) ? t('watchlist.actions.refreshing') : t('watchlist.actions.refreshNow') }}
                  </Button>
                  <Button size="sm" variant="outline" :disabled="isBusy(item.id)" @click="handleConsult(item)">
                    <MessageSquare class="mr-1.5 h-3.5 w-3.5" />
                    {{ isBusy(item.id) ? t('watchlist.actions.consulting') : t('watchlist.actions.consult') }}
                  </Button>
                  <Button size="sm" variant="outline" :disabled="isBusy(item.id)" @click="handleAiSummary(item)">
                    <Sparkles class="mr-1.5 h-3.5 w-3.5" />
                    {{ t('watchlist.actions.aiSummary') }}
                  </Button>
                  <Button size="sm" variant="outline" @click="handleOpenTrend(item)">
                    <LineChart class="mr-1.5 h-3.5 w-3.5" />
                    {{ t('watchlist.actions.trend') }}
                  </Button>
                  <Button size="sm" variant="outline" @click="openEditDialog(item)">
                    <Pencil class="mr-1.5 h-3.5 w-3.5" />
                    {{ t('watchlist.actions.edit') }}
                  </Button>
                  <Button size="sm" variant="outline" @click="openItemLink(item.link)">
                    <ExternalLink class="mr-1.5 h-3.5 w-3.5" />
                    {{ t('watchlist.actions.openLink') }}
                  </Button>
                  <Button size="sm" variant="destructive" :disabled="isBusy(item.id)" @click="openDeleteDialog(item)">
                    <Trash2 class="mr-1.5 h-3.5 w-3.5" />
                    {{ t('watchlist.actions.delete') }}
                  </Button>
                </div>
              </article>
            </div>
          </CardContent>
        </Card>
      </TabsContent>

      <!-- 事件流 -->
      <TabsContent value="events">
        <Card class="app-surface border-none">
          <CardHeader class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle>{{ t('watchlist.tabs.events') }}</CardTitle>
              <CardDescription>{{ t('watchlist.events.emptyHint') }}</CardDescription>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              <label class="flex items-center gap-2 text-sm text-slate-600">
                <Switch :model-value="unreadOnly" @update:model-value="handleToggleUnreadOnly" />
                {{ t('watchlist.filters.unreadOnly') }}
              </label>
              <Button size="sm" variant="outline" :disabled="unreadCount === 0" @click="handleReadAll">
                <BellRing class="mr-1.5 h-3.5 w-3.5" />
                {{ t('watchlist.events.markAllRead') }}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div v-if="isEventsLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>
            <div v-else-if="events.length === 0" class="py-12 text-center text-sm text-slate-500">
              {{ unreadOnly ? t('watchlist.events.emptyUnread') : t('watchlist.events.empty') }}
            </div>
            <div v-else class="space-y-2">
              <div
                v-for="event in events"
                :key="event.id"
                class="app-surface-subtle flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"
                :class="{ 'opacity-60': event.is_read }"
              >
                <div class="flex min-w-0 flex-1 items-start gap-3">
                  <Badge variant="outline" :class="eventTone(event.event_type)">{{ event.event_label }}</Badge>
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-semibold text-slate-800">{{ event.title }}</p>
                    <p class="mt-0.5 text-xs text-slate-500">{{ event.detail }}</p>
                    <p class="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-slate-400">
                      <span v-if="event.previous_price !== null && event.price !== null">
                        {{ t('watchlist.events.pricePair', { previous: event.previous_price, price: event.price }) }}
                      </span>
                      <span>{{ formatRelativeTimeFromNow(event.created_at) }}</span>
                      <span>{{ event.notified ? t('watchlist.events.notified') : t('watchlist.events.notNotified') }}</span>
                    </p>
                  </div>
                </div>
                <div class="flex flex-shrink-0 items-center gap-2">
                  <Badge variant="outline" :class="event.is_read ? 'border-slate-200 text-slate-400' : 'border-rose-200 bg-rose-50 text-rose-600'">
                    {{ event.is_read ? t('watchlist.events.read') : t('watchlist.events.unread') }}
                  </Badge>
                  <Button size="sm" variant="ghost" :disabled="event.is_read || isBusy(event.id)" @click="handleReadEvent(event)">
                    {{ t('watchlist.events.markRead') }}
                  </Button>
                  <Button size="sm" variant="ghost" @click="openItemLink(event.link)">
                    <ExternalLink class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </TabsContent>

      <!-- 价格趋势 -->
      <TabsContent value="trends">
        <Card class="app-surface border-none">
          <CardHeader>
            <CardTitle>{{ t('watchlist.tabs.trends') }}</CardTitle>
            <CardDescription>{{ t('watchlist.trends.searchHint') }}</CardDescription>
          </CardHeader>
          <CardContent class="space-y-4">
            <div class="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div class="grid flex-1 gap-2">
                <Label>{{ t('watchlist.trends.keywordLabel') }}</Label>
                <Input v-model="trendKeyword" :placeholder="t('watchlist.trends.keywordPlaceholder')" />
              </div>
              <Button :disabled="trendLoading" @click="loadCategoryTrend">
                <Activity class="mr-1.5 h-4 w-4" />
                {{ t('watchlist.trends.query') }}
              </Button>
            </div>

            <div v-if="trendLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>

            <div v-else-if="categoryTrendData" class="space-y-4">
              <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.avgPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-slate-800">{{ formatPrice(categoryTrendData.market_summary?.avg_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.medianPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-slate-800">{{ formatPrice(categoryTrendData.market_summary?.median_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.minPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-emerald-600">{{ formatPrice(categoryTrendData.market_summary?.min_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.maxPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-rose-600">{{ formatPrice(categoryTrendData.market_summary?.max_price) }}</p>
                </div>
              </div>

              <CategoryTrendChart :points="categoryPoints" />

              <p class="text-xs text-slate-400">
                {{ t('watchlist.trendDialog.observationCount', { count: categoryTrendData.history_summary?.unique_items ?? 0 }) }}
                <span v-if="categoryTrendData.latest_snapshot_at">· {{ t('watchlist.trendDialog.latestSnapshot', { time: formatRelativeTimeFromNow(categoryTrendData.latest_snapshot_at) }) }}</span>
              </p>
            </div>

            <div v-else class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-sm text-slate-500">
              {{ t('watchlist.trends.searchHint') }}
            </div>
          </CardContent>
        </Card>
      </TabsContent>

      <!-- 咨询记录 -->
      <TabsContent value="consultations">
        <Card class="app-surface border-none">
          <CardHeader class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle>{{ t('watchlist.tabs.consultations') }}</CardTitle>
              <CardDescription>{{ t('watchlist.consultations.emptyHint') }}</CardDescription>
            </div>
            <Button size="sm" variant="outline" :disabled="consultationsLoading" @click="fetchConsultations(null)">
              <RefreshCw class="mr-1.5 h-3.5 w-3.5" :class="{ 'animate-spin': consultationsLoading }" />
              {{ t('common.refresh') }}
            </Button>
          </CardHeader>
          <CardContent>
            <div v-if="consultationsLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>
            <div v-else-if="consultations.length === 0" class="py-12 text-center text-sm text-slate-500">
              {{ t('watchlist.consultations.empty') }}
            </div>
            <div v-else class="space-y-2">
              <div v-for="log in consultations" :key="log.id" class="app-surface-subtle flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between">
                <div class="min-w-0 flex-1">
                  <p class="line-clamp-2 text-sm text-slate-700">{{ log.message }}</p>
                  <p class="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-slate-400">
                    <span>{{ formatRelativeTimeFromNow(log.created_at) }}</span>
                    <span>{{ log.account_path ? t('watchlist.consultations.account', { account: log.account_path }) : t('watchlist.consultations.noAccount') }}</span>
                    <span v-if="log.error" class="text-rose-500">{{ log.error }}</span>
                  </p>
                </div>
                <Badge variant="outline" :class="consultationTone(log.status)">{{ consultationStatusLabel(log.status) }}</Badge>
              </div>
            </div>
          </CardContent>
        </Card>
      </TabsContent>
    </Tabs>

    <!-- 编辑关注 -->
    <Dialog v-model:open="isEditDialogOpen">
      <DialogContent class="max-h-[85vh] overflow-y-auto sm:max-w-[620px]">
        <DialogHeader>
          <DialogTitle>{{ t('watchlist.editDialog.title', { title: editingItem?.title || '' }) }}</DialogTitle>
          <DialogDescription>{{ t('watchlist.editDialog.description') }}</DialogDescription>
        </DialogHeader>

        <div class="space-y-5">
          <div class="grid gap-2">
            <Label>{{ t('watchlist.editDialog.alertPrice') }}</Label>
            <Input v-model="editForm.alert_price" type="number" min="0" step="0.01" :placeholder="t('watchlist.editDialog.alertPricePlaceholder')" />
          </div>

          <div class="grid gap-2">
            <Label>{{ t('watchlist.editDialog.refreshInterval') }}</Label>
            <Input v-model="editForm.refresh_interval_minutes" type="number" min="1" max="10080" :placeholder="t('watchlist.editDialog.refreshIntervalPlaceholder')" />
          </div>

          <div class="flex items-center justify-between rounded-xl border border-slate-200 p-3">
            <span class="text-sm font-medium text-slate-700">{{ t('watchlist.editDialog.enabled') }}</span>
            <Switch :model-value="editForm.enabled" @update:model-value="(value: boolean) => (editForm.enabled = value)" />
          </div>

          <div class="space-y-3">
            <p class="text-sm font-semibold text-slate-700">{{ t('watchlist.editDialog.notifySection') }}</p>
            <div class="grid gap-2 sm:grid-cols-2">
              <label
                v-for="option in notifyOptions"
                :key="option.key"
                class="flex items-center justify-between rounded-xl border border-slate-200 px-3 py-2"
              >
                <span class="text-sm text-slate-600">{{ t(option.labelKey) }}</span>
                <Switch
                  :model-value="editForm[option.key]"
                  @update:model-value="(value: boolean) => setNotifyFlag(option.key, value)"
                />
              </label>
            </div>
          </div>

          <div class="space-y-3">
            <p class="text-sm font-semibold text-slate-700">{{ t('watchlist.editDialog.consultSection') }}</p>
            <div class="flex items-center justify-between rounded-xl border border-slate-200 p-3">
              <span class="text-sm text-slate-600">{{ t('watchlist.editDialog.consultEnabled') }}</span>
              <Switch :model-value="editForm.consult_enabled" @update:model-value="(value: boolean) => (editForm.consult_enabled = value)" />
            </div>
            <div class="grid gap-2">
              <Label>{{ t('watchlist.editDialog.consultTemplate') }}</Label>
              <Textarea v-model="editForm.consult_template" class="min-h-[90px]" :placeholder="t('watchlist.editDialog.consultTemplatePlaceholder')" />
            </div>
            <div class="grid gap-2">
              <Label>{{ t('watchlist.editDialog.consultStrategy') }}</Label>
              <Input :model-value="t('watchlist.editDialog.consultStrategyPool')" disabled />
              <p class="text-xs text-slate-400">{{ t('watchlist.editDialog.consultStrategyHint') }}</p>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" @click="isEditDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button :disabled="isEditSubmitting" @click="handleSubmitEdit">
            {{ isEditSubmitting ? t('common.saving') : t('common.save') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- AI 解读 -->
    <Dialog v-model:open="isAiDialogOpen">
      <DialogContent class="max-h-[85vh] overflow-y-auto sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>{{ t('watchlist.aiDialog.title') }}：{{ aiTargetTitle }}</DialogTitle>
          <DialogDescription>{{ t('watchlist.aiDialog.description') }}</DialogDescription>
        </DialogHeader>

        <div v-if="aiLoading" class="py-12 text-center text-sm text-muted-foreground">{{ t('watchlist.actions.analyzing') }}</div>

        <div v-else-if="aiSummaryData" class="space-y-4">
          <div class="app-surface-subtle p-3">
            <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.aiDialog.summary') }}</p>
            <p class="mt-1 text-sm text-slate-700">{{ aiSummaryData.summary }}</p>
          </div>
          <div class="app-surface-subtle p-3">
            <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.aiDialog.outlook') }}</p>
            <p class="mt-1 text-sm text-slate-700">{{ aiSummaryData.outlook }}</p>
          </div>
          <div v-if="aiSummaryData.signals?.length" class="app-surface-subtle p-3">
            <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.aiDialog.signals') }}</p>
            <ul class="mt-1 space-y-1 text-sm text-slate-700">
              <li v-for="signal in aiSummaryData.signals" :key="signal">· {{ signal }}</li>
            </ul>
          </div>
          <div v-if="aiSummaryData.risks?.length" class="app-surface-subtle p-3">
            <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.aiDialog.risks') }}</p>
            <ul class="mt-1 space-y-1 text-sm text-slate-700">
              <li v-for="risk in aiSummaryData.risks" :key="risk">· {{ risk }}</li>
            </ul>
          </div>
          <p class="text-xs text-slate-400">{{ t('watchlist.aiDialog.generatedAt', { time: formatRelativeTimeFromNow(aiSummaryData.generated_at) }) }}</p>
        </div>

        <DialogFooter>
          <Button variant="outline" @click="isAiDialogOpen = false">{{ t('common.close') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- 价格趋势 -->
    <Dialog v-model:open="isTrendDialogOpen">
      <DialogContent class="max-h-[85vh] overflow-y-auto sm:max-w-[780px]">
        <DialogHeader>
          <DialogTitle>{{ t('watchlist.trendDialog.title', { title: aiTargetTitle }) }}</DialogTitle>
          <DialogDescription>{{ t('watchlist.trends.searchHint') }}</DialogDescription>
        </DialogHeader>

        <Tabs :model-value="trendMode" @update:model-value="(value: string | number) => loadTrendTab(value)">
          <TabsList class="mb-3">
            <TabsTrigger value="item">{{ t('watchlist.trendDialog.itemScope') }}</TabsTrigger>
            <TabsTrigger value="category">{{ t('watchlist.trendDialog.categoryScope') }}</TabsTrigger>
          </TabsList>

          <TabsContent value="item">
            <div v-if="trendLoading" class="py-10 text-center text-sm text-muted-foreground">{{ t('common.loading') }}</div>
            <div v-else-if="itemTrendData" class="space-y-4">
              <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.currentPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-slate-800">{{ formatPrice(itemTrendData.summary.current_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.minPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-emerald-600">{{ formatPrice(itemTrendData.summary.min_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.maxPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-rose-600">{{ formatPrice(itemTrendData.summary.max_price) }}</p>
                </div>
                <div class="app-surface-subtle p-3">
                  <p class="text-[10px] font-black uppercase tracking-widest text-slate-400">{{ t('watchlist.trendDialog.avgPrice') }}</p>
                  <p class="mt-1 text-xl font-black text-slate-800">{{ formatPrice(itemTrendData.summary.avg_price) }}</p>
                </div>
              </div>

              <PriceTrendChart :points="itemChartPoints" />

              <p class="text-xs text-slate-400">
                {{ t('watchlist.trendDialog.observationCount', { count: itemTrendData.summary.observation_count }) }}
                <span v-if="itemTrendData.summary.latest_snapshot_at">· {{ t('watchlist.trendDialog.latestSnapshot', { time: formatRelativeTimeFromNow(itemTrendData.summary.latest_snapshot_at) }) }}</span>
              </p>
              <p v-if="itemTrendData.summary.is_sparse" class="text-xs text-amber-600">{{ t('watchlist.trendDialog.sparse') }}</p>
            </div>
            <div v-else class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-sm text-slate-500">
              {{ t('watchlist.trendDialog.noData') }}
            </div>
          </TabsContent>

          <TabsContent value="category">
            <div v-if="categoryTrendData" class="space-y-4">
              <CategoryTrendChart :points="categoryPoints" />
            </div>
            <div v-else class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-10 text-center text-sm text-slate-500">
              {{ t('watchlist.trendDialog.noData') }}
            </div>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button variant="outline" @click="isTrendDialogOpen = false">{{ t('common.close') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- 取消关注 -->
    <Dialog v-model:open="isDeleteDialogOpen">
      <DialogContent class="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>{{ t('watchlist.deleteDialog.title') }}</DialogTitle>
          <DialogDescription>
            {{ t('watchlist.deleteDialog.description', { title: deletingItem?.title || '' }) }}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="isDeleteDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button variant="destructive" @click="handleConfirmDelete">{{ t('watchlist.deleteDialog.confirm') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
