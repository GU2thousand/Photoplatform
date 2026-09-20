const apiBase = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

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
      throw new ApiError(String(payload.message), response.status)
    }
    if (typeof payload === 'string' && payload.trim()) {
      throw new ApiError(compactHtml(payload), response.status)
    }
    throw new ApiError(`Request failed (${response.status})`, response.status)
  }

  return payload as T
}

export function buildApiUrl(path: string): string {
  return `${apiBase}${path}`
}

export function buildAssetUrl(path: string): string {
  return new URL(buildApiUrl(path), window.location.origin).toString()
}

export function expireLegacyMediaCookie() {
  // Migration only: older builds wrote a JWT cookie; never create or refresh it.
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `generate_cloud_token=; Path=/; Max-Age=0; SameSite=Lax${secure}`
}

export async function fetchProtectedAsset(path: string, token: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(buildAssetUrl(path), {
    headers: { Authorization: `Bearer ${token}` },
    credentials: 'omit',
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    throw new ApiError(`Image could not be loaded (${response.status}).`, response.status)
  }
  const blob = await response.blob()
  signal.throwIfAborted()
  return URL.createObjectURL(blob)
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
