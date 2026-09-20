<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultInsights } from '@/types/result.d.ts'
import PriceTrendChart from './PriceTrendChart.vue'
import { formatDateTime } from '@/i18n'

const props = defineProps<{
  insights: ResultInsights | null
  selectedTaskLabel?: string | null
}>()
const { t } = useI18n()

const summaryCards = computed(() => {
  if (!props.insights) return []
  const market = props.insights.market_summary
  const history = props.insights.history_summary
  return [
    {
      label: t('results.insights.currentAvg'),
      value: market.avg_price ? `¥${market.avg_price}` : '—',
      hint: t('results.insights.sampleCount', { count: market.sample_count || 0 }),
    },
    {
      label: t('results.insights.historyAvg'),
      value: history.avg_price ? `¥${history.avg_price}` : '—',
      hint: t('results.insights.uniqueItems', { count: history.unique_items || 0 }),
    },
    {
      label: t('results.insights.currentMin'),
      value: market.min_price ? `¥${market.min_price}` : '—',
      hint: market.max_price
        ? t('results.insights.highestPrice', { price: market.max_price })
        : t('results.insights.noRange'),
    },
  ]
})

const latestSnapshotText = computed(() => {
  if (!props.insights?.latest_snapshot_at) return t('results.insights.noSnapshot')
  return t('results.insights.latestSnapshot', {
    time: formatDateTime(props.insights.latest_snapshot_at, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }),
  })
})
</script>

<template>
  <section class="app-surface mb-6 overflow-hidden border-none">
    <div class="grid gap-8 px-6 py-6 lg:grid-cols-[1.15fr_0.85fr] lg:px-8">
      <div class="space-y-5">
        <div class="space-y-2">
          <p class="text-xs uppercase tracking-[0.28em] text-primary/70">Market Intelligence</p>
          <h2 class="text-3xl font-semibold text-slate-900">
            {{ selectedTaskLabel || t('results.insights.defaultTitle') }}
          </h2>
          <p class="max-w-2xl text-sm leading-6 text-slate-500">
            {{ t('results.insights.subtitle') }}
          </p>
        </div>

        <div class="grid gap-4 md:grid-cols-3">
          <article
            v-for="card in summaryCards"
            :key="card.label"
            class="app-surface-subtle p-4"
          >
            <p class="text-xs uppercase tracking-[0.18em] text-slate-500">{{ card.label }}</p>
            <p class="mt-3 text-2xl font-semibold text-slate-900">{{ card.value }}</p>
            <p class="mt-2 text-xs text-slate-500">{{ card.hint }}</p>
          </article>
        </div>

        <PriceTrendChart :points="insights?.daily_trend || []" />
      </div>

      <div class="space-y-4">
        <div class="rounded-[28px] border border-primary/10 bg-gradient-to-br from-primary to-sky-700 p-6 text-primary-foreground shadow-[0_16px_40px_rgba(37,99,235,0.22)]">
          <p class="text-xs uppercase tracking-[0.24em] text-primary-foreground/70">Trend Reading</p>
          <p class="mt-4 text-3xl font-semibold">
            {{ t('results.insights.snapshotCount', { count: insights?.market_summary.sample_count || 0 }) }}
          </p>
          <p class="mt-2 text-sm leading-6 text-primary-foreground/80">
            {{ t('results.insights.trendReading') }}
          </p>
        </div>

        <div class="app-surface-subtle p-5">
          <p class="text-xs uppercase tracking-[0.2em] text-slate-500">Snapshot Note</p>
          <p class="mt-4 text-sm leading-6 text-slate-600">
            {{ latestSnapshotText }}
          </p>
          <div class="mt-4 grid gap-3 text-sm text-slate-600">
            <div class="rounded-2xl bg-slate-50 px-4 py-3">
              {{ t('results.insights.currentMedian') }}
              <span class="font-semibold text-slate-900">
                {{ insights?.market_summary.median_price ? `¥${insights.market_summary.median_price}` : '—' }}
              </span>
            </div>
            <div class="rounded-2xl bg-slate-50 px-4 py-3">
              {{ t('results.insights.historyMin') }}
              <span class="font-semibold text-slate-900">
                {{ insights?.history_summary.min_price ? `¥${insights.history_summary.min_price}` : '—' }}
              </span>
            </div>
            <div class="rounded-2xl bg-slate-50 px-4 py-3">
              {{ t('results.insights.historyMax') }}
              <span class="font-semibold text-slate-900">
                {{ insights?.history_summary.max_price ? `¥${insights.history_summary.max_price}` : '—' }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
