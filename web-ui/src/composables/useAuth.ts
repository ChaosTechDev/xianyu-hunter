import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(localStorage.getItem('auth_username'))
const isLoggedIn = ref(localStorage.getItem('auth_logged_in') === 'true')

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true

    localStorage.setItem('auth_username', user)
    localStorage.setItem('auth_logged_in', 'true')

    // 启动 WebSocket 连接
    wsService.start()
  }

  async function logout() {
    // 必须先让服务端清 cookie。真正的凭证是 HttpOnly cookie，
    // JS 读不到也删不掉；只清 localStorage 的话界面回了登录页，
    // cookie 却仍然有效（默认 72 小时），后退或直接访问就能重新进系统。
    try {
      await fetch('/auth/logout', { method: 'POST' })
    } catch (e) {
      console.error('Logout request failed', e)
    }

    username.value = null
    isLoggedIn.value = false
    localStorage.removeItem('auth_username')
    localStorage.removeItem('auth_logged_in')

    // 停止 WebSocket 连接
    wsService.stop()

    // Redirect to login if using router
    if (router) {
      router.push('/login')
    } else {
      window.location.href = '/login'
    }
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/status', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        return false
      }
    } catch (e) {
      console.error('Login error', e)
      return false
    }
  }

  return {
    username,
    isAuthenticated,
    login,
    logout
  }
}
