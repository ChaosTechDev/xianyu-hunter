<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useResults } from '@/composables/useResults'
import ResultsFilterBar from '@/components/results/ResultsFilterBar.vue'
import ResultsGrid from '@/components/results/ResultsGrid.vue'
import ResultsInsightsPanel from '@/components/results/ResultsInsightsPanel.vue'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

const { t } = useI18n()

const {
  files,
  selectedFile,
  results,
  insights,
  filters,
  isLoading,
  error,
  refreshResults,
  exportSelectedResults,
  deleteSelectedFile,
  toggleItemBlock,
  blacklistKeywords,
  isSavingBlacklist,
  saveBlacklistRules,
  fileOptions,
  isFileOptionsReady,
} = useResults()

const isDeleteDialogOpen = ref(false)
const isBlacklistDialogOpen = ref(false)
const blacklistDraft = ref('')

const selectedTaskLabel = computed(() => {
  if (!selectedFile.value || fileOptions.value.length === 0) return null
  const match = fileOptions.value.find((option) => option.value === selectedFile.value)
  if (!match) return null
  return match.taskName || null
})

const deleteConfirmText = computed(() => {
  return selectedTaskLabel.value
    ? t('results.filters.deleteDialogWithTask', { task: selectedTaskLabel.value })
    : t('results.filters.deleteDialogFallback')
})

function openDeleteDialog() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultToDelete'),
      variant: 'destructive',
    })
    return
  }
  isDeleteDialogOpen.value = true
}

function openBlacklistDialog() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultSelected'),
      variant: 'destructive',
    })
    return
  }
  blacklistDraft.value = blacklistKeywords.value.join('\n')
  isBlacklistDialogOpen.value = true
}

function handleExportResults() {
  if (!selectedFile.value) {
    toast({
      title: t('results.filters.noResultToExport'),
      variant: 'destructive',
    })
    return
  }
  exportSelectedResults()
}

async function handleDeleteResults() {
  if (!selectedFile.value) return
  try {
    await deleteSelectedFile(selectedFile.value)
    toast({ title: t('results.filters.resultDeleted') })
  } catch (e) {
    toast({
      title: t('results.filters.deleteFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  } finally {
    isDeleteDialogOpen.value = false
  }
}

function parseBlacklistKeywords(input: string) {
  return input
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function handleSaveBlacklistRules() {
  try {
    await saveBlacklistRules(parseBlacklistKeywords(blacklistDraft.value))
    toast({ title: t('results.filters.blacklistSaved') })
    isBlacklistDialogOpen.value = false
  } catch (e) {
    toast({
      title: t('results.filters.blacklistSaveFailed'),
      description: (e as Error).message,
      variant: 'destructive',
    })
  }
}
</script>

<template>
  <div>
    <h1 class="text-2xl font-bold text-gray-800 mb-6">
      {{ t('results.title') }}
    </h1>

    <div v-if="error" class="app-alert-error mb-4" role="alert">
      <strong class="font-bold">{{ t('common.error') }}</strong>
      <span class="block sm:inline">{{ error.message }}</span>
    </div>

    <ResultsFilterBar
      :files="files"
      :file-options="fileOptions"
      :is-ready="isFileOptionsReady"
      v-model:selectedFile="selectedFile"
      v-model:aiRecommendedOnly="filters.ai_recommended_only"
      v-model:keywordRecommendedOnly="filters.keyword_recommended_only"
      v-model:includeHidden="filters.include_hidden"
      v-model:sortBy="filters.sort_by"
      v-model:sortOrder="filters.sort_order"
      :is-loading="isLoading"
      @refresh="refreshResults"
      @manage-blacklist="openBlacklistDialog"
      @export="handleExportResults"
      @delete="openDeleteDialog"
    />

    <ResultsInsightsPanel :insights="insights" :selected-task-label="selectedTaskLabel" />

    <ResultsGrid :results="results" :is-loading="isLoading" @toggle-block="toggleItemBlock" />

    <Dialog v-model:open="isDeleteDialogOpen">
      <DialogContent class="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{{ t('results.filters.deleteDialogTitle') }}</DialogTitle>
          <DialogDescription>
            {{ deleteConfirmText }}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="isDeleteDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button variant="destructive" :disabled="isLoading" @click="handleDeleteResults">
            {{ t('results.filters.confirmDelete') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="isBlacklistDialogOpen">
      <DialogContent class="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{{ t('results.filters.blacklistDialogTitle') }}</DialogTitle>
          <DialogDescription>
            {{ t('results.filters.blacklistDialogDescription') }}
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-2">
          <label class="text-sm font-medium text-slate-700">
            {{ t('results.filters.blacklistRulesLabel') }}
          </label>
          <Textarea
            v-model="blacklistDraft"
            class="min-h-[180px]"
            :placeholder="t('results.filters.blacklistRulesPlaceholder')"
          />
          <p class="text-xs leading-5 text-slate-500">
            {{ t('results.filters.blacklistRulesHint') }}
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="isBlacklistDialogOpen = false">{{ t('common.cancel') }}</Button>
          <Button :disabled="isSavingBlacklist" @click="handleSaveBlacklistRules">
            {{ t('results.filters.confirmBlacklistSave') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
