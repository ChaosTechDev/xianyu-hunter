<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultItem } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
}>()
const { t } = useI18n()

const info = props.item.商品信息
const seller = props.item.卖家信息
const ai = props.item.ai_analysis
const priceInsight = props.item.price_insight

const isRecommended = ai?.is_recommended === true
const recommendationStatus = computed(() => {
  if (ai?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})

const imageUrl = info.商品图片列表?.[0] || info.商品主图链接 || ''
const crawlTime = props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')

// 后端融合评分优先：ai.value_score 是可选的模型自由字段，经常缺失，
// 缺失时旧逻辑会静默退化成 0%（把「没给分」显示成「零分」）。
// 评分是后端确定性算出来的，只要拿到 AI 结论/关键词/价格任一项就存在。
const fusedScore = computed(() => props.item.评分)
const scorePercent = computed(() => {
  const fused = fusedScore.value?.score
  if (typeof fused === 'number' && Number.isFinite(fused)) {
    return Math.round(Math.max(0, Math.min(100, fused)))
  }
  const legacy = ai?.value_score
  if (typeof legacy === 'number' && Number.isFinite(legacy)) {
    return Math.round(Math.max(0, Math.min(100, legacy)))
  }
  return null
})
const scoreLabel = computed(() => (scorePercent.value === null ? '—' : `${scorePercent.value}%`))
// 一票否决（硬性条件不满足）时，分数被后端强制封顶。必须显式提示，
// 否则用户看到「已通过」标签配 30 分会困惑。
const isVetoed = computed(() => props.item.评分?.vetoed === true)
const vetoReason = computed(() => {
  const failures = props.item.评分?.ai_failures
  return failures?.length ? failures.join('、') : ''
})
const isDegraded = computed(() => props.item.评分?.degraded === true)

// 卖家信用评分：后端把散落的卖家指标确定性合成一个分数与风险信号。
// 关键：score 可能是 null（卖家信息不足），此时**必须显示「未评分」而不是 0 分**。
// 「没数据」与「信用很差」是两件完全不同的事，把前者渲染成后者会误伤正常卖家。
const sellerCredit = computed(() => props.item.卖家信用评分)
const sellerCreditScore = computed(() => {
  const raw = sellerCredit.value?.score
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return Math.round(Math.max(0, Math.min(100, raw)))
  }
  return null
})
const sellerCreditLabel = computed(() => {
  if (sellerCreditScore.value === null) return t('results.card.sellerCreditUnknown')
  return sellerCredit.value?.level_label || `${sellerCreditScore.value}%`
})
const sellerCreditRiskFlags = computed<string[]>(() => {
  const flags = sellerCredit.value?.risk_flags
  return Array.isArray(flags) ? flags.filter((f: unknown) => typeof f === 'string') : []
})
const sellerCreditVetoed = computed(() => sellerCredit.value?.vetoed === true)
const sellerCreditTone = computed(() => {
  if (sellerCreditScore.value === null) return 'text-slate-400'
  if (sellerCreditVetoed.value || sellerCreditScore.value < 40) return 'text-rose-600'
  if (sellerCreditScore.value < 70) return 'text-amber-600'
  return 'text-emerald-600'
})

const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

const expanded = ref(false)
</script>

<template>
  <Card class="group flex flex-col h-full border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[4/3] overflow-hidden">
      <div class="absolute inset-0 bg-slate-200 animate-pulse" v-if="!imageUrl"></div>
      <img
        v-else
        :src="imageUrl"
        :alt="info.商品标题"
        class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
        loading="lazy"
      />
      <!-- Hidden overlay -->
      <div v-if="isHidden" class="absolute inset-0 bg-black/30 flex items-center justify-center">
        <span class="text-white/80 text-xs font-semibold uppercase tracking-wider">{{ hiddenLabel }}</span>
      </div>
      <!-- Overlays -->
      <div class="absolute top-3 left-3 flex gap-2">
        <Badge v-if="isRecommended && !isHidden" variant="default" class="bg-emerald-500/90 backdrop-blur-md border-none shadow-sm">
          {{ t('results.card.curated') }}
        </Badge>
        <Badge v-if="isRuleHidden" variant="secondary" class="bg-slate-900/75 text-white border-none backdrop-blur-md shadow-sm">
          {{ t('results.card.blacklisted') }}
        </Badge>
      </div>
      <div class="absolute top-3 right-3 flex gap-1.5">
        <button
          v-if="canToggleBlock"
          type="button"
          @click="emit('toggle-block', props.item)"
          :aria-label="isHidden ? t('results.card.unblock') : t('results.card.block')"
          class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-white/50"
        >
          <EyeOff v-if="!isHidden" class="w-4 h-4" />
          <Eye v-else class="w-4 h-4" />
        </button>
         <a
           :href="info.商品链接"
           target="_blank"
           rel="noopener noreferrer"
           :aria-label="t('results.card.detail')"
           class="flex rounded-full bg-white/30 p-1.5 text-white backdrop-blur-md border border-white/40 shadow-sm opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
         >
            <ExternalLink class="w-4 h-4" />
         </a>
      </div>
    </div>

    <CardHeader class="p-4 pb-2">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="text-base font-semibold text-slate-800 line-clamp-2 leading-snug flex-grow h-10">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-2">
        <span class="text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="info['商品原价']" class="text-xs text-slate-400 line-through mb-1">{{ info['商品原价'] }}</span>
      </div>
    </CardHeader>

    <CardContent class="p-4 pt-2 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-3 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
            <Badge
              v-if="isVetoed"
              variant="destructive"
              class="text-[9px] px-1.5 py-0 h-4"
              :title="vetoReason ? `触发硬性否决：${vetoReason}` : '触发硬性否决'"
            >
              {{ t('results.card.vetoed') }}
            </Badge>
          </div>
          <div class="flex items-center gap-1">
             <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">{{ t('results.card.fusedScore') }}</span>
             <span class="text-sm font-black" :class="recommendationStatus.text">{{ scoreLabel }}</span>
          </div>
        </div>

        <div class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-3">
          <div
            class="h-full transition-all duration-1000 ease-out rounded-full"
            :class="recommendationStatus.color"
            :style="{ width: `${scorePercent ?? 0}%` }"
          ></div>
        </div>

        <p class="text-xs leading-relaxed text-slate-600" :class="{ 'line-clamp-2': !expanded }">
           {{ ai?.reason || t('results.card.analyzing') }}
        </p>

        <!-- 降级提示：缺价格依据时评分只用了 AI + 关键词两个维度，
             不说清楚会让用户以为价格已被纳入考量 -->
        <p v-if="isDegraded" class="mt-1.5 text-[10px] text-amber-600 flex items-center gap-1">
          <Info class="w-3 h-3" />{{ t('results.card.degraded') }}
        </p>
        <p v-if="isVetoed && vetoReason" class="mt-1.5 text-[10px] text-rose-600 flex items-center gap-1">
          <AlertCircle class="w-3 h-3" />{{ t('results.card.vetoReason', { items: vetoReason }) }}
        </p>

        <!-- 卖家信用评分：独立于 AI 结论的确定性判定，用于交叉验证提示词里
             「信用等级必须极好」这类硬性条件。信息不足时显式提示未评分。 -->
        <div
          v-if="sellerCredit"
          class="mt-2 pt-2 border-t border-slate-100 flex items-center justify-between gap-2"
        >
          <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">
            {{ t('results.card.sellerCredit') }}
          </span>
          <div class="flex items-center gap-1.5 min-w-0">
            <span class="text-xs font-bold shrink-0" :class="sellerCreditTone">
              {{ sellerCreditLabel }}
            </span>
            <Badge
              v-if="sellerCreditRiskFlags.length"
              variant="outline"
              class="text-[9px] px-1.5 py-0 h-4 border-amber-300 text-amber-700 max-w-[9rem] truncate"
              :title="`${t('results.card.sellerCreditRisk')}：${sellerCreditRiskFlags.join('、')}`"
            >
              {{ t('results.card.sellerCreditRisk') }} {{ sellerCreditRiskFlags.length }}
            </Badge>
          </div>
        </div>
        
        <button
          type="button"
          v-if="ai?.reason && ai.reason.length > 50"
          @click="expanded = !expanded" 
          class="mt-1 text-[10px] font-bold uppercase text-primary/70 hover:text-primary transition-colors flex items-center gap-1"
        >
          {{ expanded ? t('results.card.collapse') : t('results.card.expand') }}
          <Info class="w-3 h-3" />
        </button>
      </div>

      <!-- Price Stats Grid -->
      <div v-if="priceInsight?.observation_count" class="mt-4 grid grid-cols-2 gap-3">
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50 group/stat">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingUp class="w-3 h-3" /> {{ t('results.card.marketAvg') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight.market_avg_price ? `¥${priceInsight.market_avg_price}` : '—' }}
          </div>
        </div>
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingDown class="w-3 h-3" /> {{ t('results.card.historicalLow') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight.min_price ? `¥${priceInsight.min_price}` : '—' }}
          </div>
        </div>
      </div>
    </CardContent>

    <CardFooter class="px-4 py-3 bg-slate-50/30 border-t border-slate-100/60 flex items-center justify-between text-[10px]">
      <div class="flex items-center gap-3 text-slate-400">
        <div class="flex items-center gap-1">
          <User class="w-3 h-3" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex items-center gap-1">
          <Clock class="w-3 h-3" />
          <span>{{ crawlTime }}</span>
        </div>
      </div>
      <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
        {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
      </a>
    </CardFooter>
  </Card>
</template>
