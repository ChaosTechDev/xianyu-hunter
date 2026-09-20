<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertTriangle, HardDrive, Loader2, RefreshCw, Trash2 } from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  executeRetention,
  getStorageUsage,
  type OldestRecords,
  type RetentionExecutePayload,
  type RetentionExecuteResponse,
  type StorageUsage,
} from '@/api/storage'

const { t } = useI18n()

const usage = ref<StorageUsage | null>(null)
const oldest = ref<OldestRecords | null>(null)
const generatedAt = ref<string>('')
const isLoading = ref(false)
const isExecuting = ref(false)
const errorMessage = ref('')
const result = ref<RetentionExecuteResponse | null>(null)

// 保留天数：留空表示沿用 .env 里已配置的值，而不是「用 0 天清空」。
// 这个区别很关键——空值被当成 0 会让一次误点清掉全部历史。
const days = ref<Record<string, string>>({
  result_items_days: '',
  price_snapshots_days: '',
  watch_events_days: '',
  logs_days: '',
  ai_usage_days: '',
})

const directoryRows = computed(() => {
  const dirs = usage.value?.directories ?? {}
  return Object.entries(dirs)
    .map(([name, info]) => ({ name, ...info }))
    .sort((a, b) => b.bytes - a.bytes)
})

const oldestRows = computed(() => {
  const records = oldest.value ?? {}
  return Object.entries(records).map(([name, time]) => ({ name, time }))
})

const dayFields = computed(() => [
  { key: 'result_items_days', label: t('storage.days.resultItems') },
  { key: 'price_snapshots_days', label: t('storage.days.priceSnapshots') },
  { key: 'watch_events_days', label: t('storage.days.watchEvents') },
  { key: 'logs_days', label: t('storage.days.logs') },
  { key: 'ai_usage_days', label: t('storage.days.aiUsage') },
])

function buildPayload(confirm: boolean): RetentionExecutePayload {
  const payload: RetentionExecutePayload = { confirm }
  const mutable = payload as unknown as Record<string, unknown>
  for (const [key, value] of Object.entries(days.value)) {
    const trimmed = String(value ?? '').trim()
    // 只提交用户真正填过的字段；留空的分支由后端从 .env 读取。
    if (trimmed !== '' && Number.isFinite(Number(trimmed))) {
      mutable[key] = Number(trimmed)
    }
  }
  return payload
}

async function load() {
  isLoading.value = true
  errorMessage.value = ''
  try {
    const response = await getStorageUsage()
    usage.value = response.usage
    oldest.value = response.oldest_records
    generatedAt.value = response.generated_at
  } catch (error) {
    errorMessage.value = (error as Error).message
  } finally {
    isLoading.value = false
  }
}

async function runCleanup() {
  isExecuting.value = true
  errorMessage.value = ''
  try {
    // confirm= true 才会真正删除；这是接口层的第二道闸门。
    result.value = await executeRetention(buildPayload(true))
    await load()
  } catch (error) {
    errorMessage.value = (error as Error).message
  } finally {
    isExecuting.value = false
  }
}

async function preview() {
  isExecuting.value = true
  errorMessage.value = ''
  try {
    result.value = await executeRetention(buildPayload(false))
  } catch (error) {
    errorMessage.value = (error as Error).message
  } finally {
    isExecuting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <Card class="app-surface overflow-hidden border-none">
      <CardHeader>
        <div class="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <CardTitle class="flex items-center gap-2">
              <HardDrive class="h-4 w-4 text-slate-500" />
              {{ t('storage.title') }}
            </CardTitle>
            <CardDescription>{{ t('storage.description') }}</CardDescription>
          </div>
          <Button variant="outline" size="sm" :disabled="isLoading" @click="load">
            <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': isLoading }" />
            {{ t('storage.refresh') }}
          </Button>
        </div>
      </CardHeader>

      <CardContent class="grid gap-6">
        <p v-if="errorMessage" class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {{ errorMessage }}
        </p>

        <div v-if="usage" class="grid gap-4 sm:grid-cols-3">
          <div class="app-surface-subtle p-4">
            <p class="text-xs text-slate-500">{{ t('storage.totalSize') }}</p>
            <p class="mt-1 text-2xl font-semibold text-slate-900">{{ usage.total_human }}</p>
          </div>
          <div class="app-surface-subtle p-4">
            <p class="text-xs text-slate-500">{{ t('storage.totalFiles') }}</p>
            <p class="mt-1 text-2xl font-semibold text-slate-900">{{ usage.total_files }}</p>
          </div>
          <div class="app-surface-subtle p-4">
            <p class="text-xs text-slate-500">{{ t('storage.largestDirectory') }}</p>
            <p class="mt-1 text-2xl font-semibold text-slate-900">{{ usage.largest_directory || '-' }}</p>
          </div>
        </div>

        <section v-if="directoryRows.length" class="grid gap-3">
          <h3 class="font-semibold text-slate-900">{{ t('storage.byDirectory') }}</h3>
          <div class="divide-y divide-slate-100 rounded-2xl border">
            <div v-for="row in directoryRows" :key="row.name" class="flex items-center justify-between gap-4 px-4 py-3">
              <div class="min-w-0">
                <p class="truncate font-medium text-slate-800">{{ row.name }}</p>
                <p class="text-xs text-slate-500">
                  {{ t('storage.fileCount', { count: row.file_count }) }}
                </p>
              </div>
              <div class="text-right">
                <p class="font-mono text-sm text-slate-900">{{ row.human }}</p>
                <p class="text-xs text-slate-400">{{ row.share_percent }}%</p>
              </div>
            </div>
          </div>
        </section>

        <section class="grid gap-3">
          <h3 class="font-semibold text-slate-900">{{ t('storage.oldestRecords') }}</h3>
          <div class="divide-y divide-slate-100 rounded-2xl border">
            <div v-for="row in oldestRows" :key="row.name" class="flex items-center justify-between gap-4 px-4 py-3">
              <span class="font-mono text-sm text-slate-700">{{ row.name }}</span>
              <span v-if="row.time" class="font-mono text-xs text-slate-600">{{ row.time }}</span>
              <!-- 空表不能显示成「0」或留白：那会被读成「刚写入过」，与事实相反 -->
              <Badge v-else variant="outline">{{ t('storage.empty') }}</Badge>
            </div>
          </div>
        </section>

        <section class="grid gap-4">
          <div>
            <h3 class="font-semibold text-slate-900">{{ t('storage.retentionTitle') }}</h3>
            <p class="text-sm text-slate-500">{{ t('storage.retentionHelp') }}</p>
          </div>
          <div class="grid gap-4 md:grid-cols-3">
            <div v-for="field in dayFields" :key="field.key" class="grid gap-2">
              <Label :for="field.key">{{ field.label }}</Label>
              <Input
                :id="field.key"
                v-model="days[field.key]"
                type="number"
                min="1"
                :placeholder="t('storage.days.placeholder')"
              />
            </div>
          </div>
          <div class="flex items-start gap-2 rounded-2xl border border-dashed border-amber-200 bg-amber-50/70 px-4 py-3 text-sm text-amber-800">
            <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" />
            <p>{{ t('storage.deleteWarning') }}</p>
          </div>
        </section>

        <section v-if="result" class="rounded-2xl border px-4 py-3 text-sm"
                 :class="result.dry_run ? 'border-slate-200 bg-slate-50 text-slate-700' : 'border-emerald-200 bg-emerald-50 text-emerald-800'">
          <p class="font-medium">
            {{ result.dry_run ? t('storage.resultDryRun') : t('storage.resultExecuted') }}
          </p>
          <p class="mt-1">{{ result.note }}</p>
          <p class="mt-2 font-mono text-xs">
            {{ t('storage.resultStats', {
              rows: result.deleted_rows,
              files: result.deleted_files,
              freed: result.freed_human ?? result.freed_bytes,
            }) }}
          </p>
          <ul v-if="result.errors.length" class="mt-2 list-disc pl-5 text-red-700">
            <li v-for="(item, index) in result.errors" :key="index">{{ item }}</li>
          </ul>
        </section>

        <p v-if="generatedAt" class="text-xs text-slate-400">
          {{ t('storage.generatedAt', { time: generatedAt }) }}
        </p>
      </CardContent>

      <CardFooter class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <Badge variant="outline">{{ t('storage.footerHint') }}</Badge>
        <div class="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" :disabled="isExecuting" @click="preview">
            <Loader2 v-if="isExecuting" class="h-4 w-4 animate-spin" />
            {{ t('storage.preview') }}
          </Button>
          <Button variant="destructive" size="sm" :disabled="isExecuting" @click="runCleanup">
            <Trash2 class="h-4 w-4" />
            {{ t('storage.execute') }}
          </Button>
        </div>
      </CardFooter>
    </Card>
  </div>
</template>
