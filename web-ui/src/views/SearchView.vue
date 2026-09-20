<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { listAccounts, type AccountItem } from '@/api/accounts'
import { searchOfficialItems } from '@/api/search'
import { createWatchItem } from '@/api/watchlist'
import type { OfficialSearchItem } from '@/types/search.d.ts'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { toast } from '@/components/ui/toast'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ExternalLink, Eye, Search as SearchIcon, ShoppingCart } from 'lucide-vue-next'

const { t } = useI18n()
const router = useRouter()

/** 后端 account_path 为必填查询参数，这里用一个必然不存在的名字触发匿名回退 */
const ANONYMOUS_ACCOUNT_PATH = 'anonymous'

const keyword = ref('')
const accountPath = ref<string>(ANONYMOUS_ACCOUNT_PATH)
const pageSize = ref('30')
const page = ref(1)

const accounts = ref<AccountItem[]>([])
const isSearching = ref(false)
const hasSearched = ref(false)
const items = ref<OfficialSearchItem[]>([])
const hasMore = ref(false)
const lastError = ref<string | null>(null)

// 按 item_id 记录各商品的提醒价与提交状态
const alertPrices = ref<Record<string, string>>({})
const submittingIds = ref<Set<string>>(new Set())
const watchedIds = ref<Set<string>>(new Set())

const pageSizeOptions = ['10', '20', '30', '40', '50']

const resultSummary = computed(() =>
  t('search.resultCount', { page: page.value, count: items.value.length })
)

function isSubmitting(itemId: string) {
  return submittingIds.value.has(itemId)
}

function isWatched(itemId: string) {
  return watchedIds.value.has(itemId)
}

function setSubmitting(itemId: string, submitting: boolean) {
  const next = new Set(submittingIds.value)
  if (submitting) {
    next.add(itemId)
  } else {
    next.delete(itemId)
  }
  submittingIds.value = next
}

function markWatched(itemId: string) {
  const next = new Set(watchedIds.value)
  next.add(itemId)
  watchedIds.value = next
}

function openLink(link: string) {
  if (!link) return
  window.open(link, '_blank', 'noopener,noreferrer')
}

function displayPrice(item: OfficialSearchItem): string {
  if (item.price_display) return item.price_display
  if (item.price !== null && item.price !== undefined) return `¥${item.price}`
  return t('common.unknown')
}

async function fetchAccounts() {
  try {
    accounts.value = await listAccounts()
  } catch (e) {
    toast({ title: t('accounts.toasts.loadFailed'), description: (e as Error).message, variant: 'destructive' })
  }
}

async function handleSearch(reset = true) {
  const trimmed = keyword.value.trim()
  if (!trimmed) {
    toast({ title: t('search.validation.keywordRequired'), variant: 'destructive' })
    return
  }
  if (reset) {
    page.value = 1
  }

  isSearching.value = true
  lastError.value = null
  try {
    const result = await searchOfficialItems({
      keyword: trimmed,
      accountPath: accountPath.value || ANONYMOUS_ACCOUNT_PATH,
      page: page.value,
      pageSize: Number(pageSize.value),
    })
    items.value = result.items || []
    hasMore.value = Boolean(result.has_more)
    hasSearched.value = true
    // 新结果需要重新初始化提醒价输入框
    alertPrices.value = {}
  } catch (e) {
    const message = (e as Error).message || ''
    lastError.value = message
    items.value = []
    hasMore.value = false
    hasSearched.value = true
    // 把后端的典型错误翻译成可操作提示
    if (message.includes('登录态')) {
      toast({ title: t('search.error.notFound'), description: message, variant: 'destructive' })
    } else if (message.includes('浏览器未安装')) {
      toast({ title: t('search.error.browserMissing'), description: message, variant: 'destructive' })
    } else {
      toast({ title: t('search.title'), description: message, variant: 'destructive' })
    }
  } finally {
    isSearching.value = false
  }
}

function goPrevPage() {
  if (page.value <= 1) return
  page.value -= 1
  handleSearch(false)
}

function goNextPage() {
  if (!hasMore.value) return
  page.value += 1
  handleSearch(false)
}

/** 把一条官方搜索结果加入关注列表 */
async function handleAddWatch(item: OfficialSearchItem) {
  if (!item.item_id || !item.title || !item.link) {
    toast({ title: t('watchlist.toasts.watchAddFailed'), description: t('search.card.addWatch'), variant: 'destructive' })
    return
  }
  const rawAlert = (alertPrices.value[item.item_id] || '').trim()
  let alertPrice: number | null = null
  if (rawAlert) {
    const parsed = Number(rawAlert)
    if (!Number.isFinite(parsed) || parsed < 0) {
      toast({ title: t('watchlist.toasts.watchAddFailed'), description: t('search.card.alertPricePlaceholder'), variant: 'destructive' })
      return
    }
    alertPrice = parsed
  }

  setSubmitting(item.item_id, true)
  try {
    await createWatchItem({
      item_id: item.item_id,
      title: item.title,
      link: item.link,
      keyword: keyword.value.trim(),
      image_url: item.image_url ?? null,
      alert_price: alertPrice,
      last_price: item.price,
    })
    markWatched(item.item_id)
    toast({ title: t('watchlist.toasts.watchAdded') })
  } catch (e) {
    toast({ title: t('watchlist.toasts.watchAddFailed'), description: (e as Error).message, variant: 'destructive' })
  } finally {
    setSubmitting(item.item_id, false)
  }
}

onMounted(fetchAccounts)
</script>

<template>
  <div>
    <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 class="text-2xl font-bold text-gray-800">{{ t('search.title') }}</h1>
        <p class="text-sm text-gray-500 mt-1">{{ t('search.description') }}</p>
      </div>
      <Button variant="outline" size="sm" @click="router.push('/watchlist')">
        <Eye class="mr-2 h-4 w-4" />
        {{ t('watchlist.title') }}
      </Button>
    </div>

    <Card class="app-surface mb-6 border-none">
      <CardHeader>
        <CardTitle>{{ t('search.title') }}</CardTitle>
        <CardDescription>{{ t('search.initialHint') }}</CardDescription>
      </CardHeader>
      <CardContent>
        <div class="grid gap-4 md:grid-cols-12">
          <div class="grid gap-2 md:col-span-5">
            <Label>{{ t('search.keyword') }}</Label>
            <Input
              v-model="keyword"
              :placeholder="t('search.keywordPlaceholder')"
              @keydown.enter="handleSearch(true)"
            />
          </div>

          <div class="grid gap-2 md:col-span-4">
            <Label>{{ t('search.account') }}</Label>
            <Select v-model="accountPath">
              <SelectTrigger>
                <SelectValue :placeholder="t('search.accountPlaceholder')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem :value="ANONYMOUS_ACCOUNT_PATH">{{ t('search.accountAnonymous') }}</SelectItem>
                <SelectItem v-for="account in accounts" :key="account.name" :value="account.path">
                  {{ account.name }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div class="grid gap-2 md:col-span-3">
            <Label>{{ t('search.pageSize') }}</Label>
            <Select v-model="pageSize">
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="option in pageSizeOptions" :key="option" :value="option">{{ option }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <p class="mt-3 text-xs text-slate-400">{{ t('search.accountHint') }}</p>

        <div class="mt-4 flex flex-wrap items-center gap-2">
          <Button :disabled="isSearching" @click="handleSearch(true)">
            <SearchIcon class="mr-2 h-4 w-4" :class="{ 'animate-spin': isSearching }" />
            {{ isSearching ? t('search.searching') : t('search.submit') }}
          </Button>
          <span v-if="hasSearched && !isSearching" class="text-xs text-slate-500">
            {{ resultSummary }} · {{ hasMore ? t('search.hasMore') : t('search.noMore') }}
          </span>
        </div>
      </CardContent>
    </Card>

    <div v-if="lastError" class="app-alert-error mb-4" role="alert">
      <strong class="font-bold">{{ t('common.error') }}</strong>
      <span class="block sm:inline">{{ lastError }}</span>
    </div>

    <div v-if="isSearching" class="py-16 text-center text-sm text-muted-foreground">
      <SearchIcon class="mx-auto mb-3 h-6 w-6 animate-spin text-slate-400" />
      {{ t('search.searching') }}
    </div>

    <div v-else-if="!hasSearched" class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-16 text-center">
      <p class="text-sm font-semibold text-slate-600">{{ t('search.initial') }}</p>
      <p class="mt-1 text-xs text-slate-400">{{ t('search.initialHint') }}</p>
    </div>

    <div v-else-if="items.length === 0" class="rounded-2xl border border-dashed border-slate-200 bg-white/70 px-4 py-16 text-center">
      <p class="text-sm font-semibold text-slate-600">{{ t('search.empty') }}</p>
      <p class="mt-1 text-xs text-slate-400">{{ t('search.emptyHint') }}</p>
    </div>

    <div v-else class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <Card v-for="item in items" :key="item.item_id" class="app-surface flex flex-col border-none">
        <div class="aspect-video w-full overflow-hidden rounded-t-2xl border-b border-slate-200 bg-slate-50">
          <img v-if="item.image_url" :src="item.image_url" :alt="item.title" class="h-full w-full object-cover" loading="lazy" />
          <div v-else class="flex h-full w-full items-center justify-center text-xs text-slate-400">{{ t('search.card.noImage') }}</div>
        </div>

        <CardContent class="flex flex-1 flex-col gap-3 p-4">
          <h3 class="line-clamp-2 text-sm font-bold text-slate-800">{{ item.title || t('common.unnamed') }}</h3>

          <div class="flex flex-wrap items-baseline gap-2">
            <span class="text-lg font-black text-rose-600">{{ displayPrice(item) }}</span>
            <span v-if="item.original_price && item.original_price !== item.price_display" class="text-xs text-slate-400 line-through">
              {{ t('search.card.originalPrice', { price: item.original_price }) }}
            </span>
          </div>

          <div class="space-y-0.5 text-xs text-slate-500">
            <p v-if="item.seller">{{ t('search.card.seller', { seller: item.seller }) }}</p>
            <p v-if="item.region">{{ t('search.card.region', { region: item.region }) }}</p>
            <p v-if="item.publish_time">{{ t('search.card.publishTime', { time: item.publish_time }) }}</p>
          </div>

          <div v-if="item.tags?.length" class="flex flex-wrap gap-1">
            <Badge v-for="tag in item.tags.slice(0, 4)" :key="String(tag)" variant="outline" class="border-slate-200 text-slate-500">
              {{ tag }}
            </Badge>
          </div>

          <div class="mt-auto space-y-2">
            <div class="grid gap-1">
              <Label class="text-xs text-slate-500">{{ t('search.card.alertPrice') }}</Label>
              <Input
                v-model="alertPrices[item.item_id]"
                type="number"
                min="0"
                step="0.01"
                class="h-9"
                :placeholder="t('search.card.alertPricePlaceholder')"
              />
            </div>
            <div class="flex gap-2">
              <Button
                size="sm"
                class="flex-1"
                :disabled="isSubmitting(item.item_id) || isWatched(item.item_id)"
                @click="handleAddWatch(item)"
              >
                <ShoppingCart class="mr-1.5 h-3.5 w-3.5" />
                <template v-if="isWatched(item.item_id)">{{ t('search.card.watched') }}</template>
                <template v-else-if="isSubmitting(item.item_id)">{{ t('search.card.adding') }}</template>
                <template v-else>{{ t('search.card.addWatch') }}</template>
              </Button>
              <Button size="sm" variant="outline" @click="openLink(item.link)">
                <ExternalLink class="mr-1.5 h-3.5 w-3.5" />
                {{ t('search.card.openLink') }}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>

    <div v-if="hasSearched && items.length > 0" class="mt-6 flex items-center justify-center gap-3">
      <Button variant="outline" :disabled="page <= 1 || isSearching" @click="goPrevPage">{{ t('search.pagination.prev') }}</Button>
      <span class="text-sm text-slate-500">{{ page }}</span>
      <Button variant="outline" :disabled="!hasMore || isSearching" @click="goNextPage">{{ t('search.pagination.next') }}</Button>
    </div>
  </div>
</template>
