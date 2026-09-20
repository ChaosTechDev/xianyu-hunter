<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import LocaleToggle from '@/components/layout/LocaleToggle.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { useI18n } from 'vue-i18n'

const username = ref('')
const password = ref('')
const isLoading = ref(false)
const error = ref('')

const { login } = useAuth()
const router = useRouter()
const route = useRoute()
const { t } = useI18n()

async function handleLogin() {
  if (!username.value || !password.value) {
    error.value = t('login.errors.missingCredentials')
    return
  }

  isLoading.value = true
  error.value = ''

  try {
    const success = await login(username.value, password.value)
    if (success) {
      const redirectPath = (route.query.redirect as string) || '/'
      router.push(redirectPath)
    } else {
      error.value = t('login.errors.invalidCredentials')
    }
  } catch (e) {
    error.value = t('login.errors.unexpected')
  } finally {
    isLoading.value = false
  }
}
</script>

<template>
  <div class="relative flex min-h-screen items-center justify-center overflow-hidden bg-slate-100 px-4">
    <div aria-hidden="true" class="absolute inset-0">
      <div class="absolute left-[-10%] top-[-10%] h-72 w-72 rounded-full bg-primary/10 blur-3xl"></div>
      <div class="absolute bottom-[-10%] right-[-5%] h-72 w-72 rounded-full bg-blue-300/10 blur-3xl"></div>
    </div>
    <div class="absolute right-6 top-6">
      <LocaleToggle />
    </div>
    <Card class="app-surface relative z-10 w-full max-w-md border-none">
      <CardHeader>
        <CardTitle class="text-2xl text-center">{{ t('login.title') }}</CardTitle>
        <CardDescription class="text-center">
          {{ t('login.description') }}
        </CardDescription>
      </CardHeader>
      <form @submit.prevent="handleLogin">
        <CardContent class="grid gap-4">
          <div class="grid gap-2">
            <Label for="username">{{ t('login.username') }}</Label>
            <Input id="username" type="text" v-model="username" placeholder="admin" required />
          </div>
          <div class="grid gap-2">
            <Label for="password">{{ t('login.password') }}</Label>
            <Input id="password" type="password" v-model="password" required />
          </div>
          <div v-if="error" class="text-sm font-medium text-red-500" role="alert">
            {{ error }}
          </div>
        </CardContent>
        <CardFooter>
          <Button class="w-full" type="submit" :disabled="isLoading">
            {{ isLoading ? t('login.submitting') : t('login.submit') }}
          </Button>
        </CardFooter>
      </form>
    </Card>
  </div>
</template>
