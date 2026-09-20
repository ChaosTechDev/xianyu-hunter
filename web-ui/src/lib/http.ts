import { useAuth } from '@/composables/useAuth'

interface FetchOptions extends RequestInit {
  params?: Record<string, string | number | boolean | undefined>;
}

export async function http(url: string, options: FetchOptions = {}) {
  const { logout } = useAuth()
  
  const headers = new Headers(options.headers)

  // Handle Query Params
  let fullUrl = url
  if (options.params) {
    const searchParams = new URLSearchParams()
    Object.entries(options.params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        searchParams.append(key, String(value))
      }
    })
    const queryString = searchParams.toString()
    if (queryString) {
      fullUrl += (url.includes('?') ? '&' : '?') + queryString
    }
  }

  const config: RequestInit = {
    ...options,
    headers,
  }

  const response = await fetch(fullUrl, config)

  if (response.status === 401) {
    // Basic Auth failed or session expired.
    // logout() 现在会请求服务端清 cookie，因此是异步的。这里不 await：
    // 已经在错误路径上，等它返回只会拖慢报错；同时显式 void + catch，
    // 避免清理失败时抛 unhandled rejection 盖住真正的 401 原因。
    void logout().catch(() => {})
    // Optional: Redirect to login handled by router or state change
    throw new Error('Unauthorized')
  }

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new Error(errorData.detail || `HTTP error! status: ${response.status}`)
  }

  // Handle 204 No Content
  if (response.status === 204) {
    return null
  }

  return response.json()
}
