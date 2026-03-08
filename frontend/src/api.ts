const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const mediaTokenCookieName = 'generate_cloud_token'

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(options.headers ?? {})

  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(buildApiUrl(path), {
    ...options,
    headers,
  })

  const text = await response.text()
  const contentType = response.headers.get('content-type') ?? ''
  const payload = parsePayload(text, contentType)

  if (!response.ok) {
    if (payload && typeof payload === 'object' && 'message' in payload) {
      throw new Error(String(payload.message))
    }
    if (typeof payload === 'string' && payload.trim()) {
      throw new Error(compactHtml(payload))
    }
    throw new Error(`Request failed (${response.status})`)
  }

  return payload as T
}

export function buildApiUrl(path: string): string {
  return `${apiBase}${path}`
}

export function buildAssetUrl(path: string, token?: string): string {
  const url = new URL(buildApiUrl(path), window.location.origin)
  if (token) {
    url.searchParams.set('token', token)
  }
  return url.toString()
}

export function persistMediaToken(token: string) {
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `${mediaTokenCookieName}=${encodeURIComponent(token)}; Path=/; SameSite=Lax${secure}`
}

export function clearMediaToken() {
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `${mediaTokenCookieName}=; Path=/; Max-Age=0; SameSite=Lax${secure}`
}

export function buildWebSocketUrl(path: string): string {
  const base = apiBase ? new URL(apiBase, window.location.origin) : new URL(window.location.origin)
  const protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${base.host}${path}`
}

function parsePayload(text: string, contentType: string): unknown {
  if (!text) {
    return null
  }
  if (contentType.includes('application/json')) {
    return JSON.parse(text)
  }
  return text
}

function compactHtml(value: string): string {
  return value.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
}
