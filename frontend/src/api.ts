const API_BASE = import.meta.env.DEV ? 'http://127.0.0.1:5177' : ''

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(apiUrl(path), { ...init, headers })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const data = await response.json()
      message = data.detail || message
    } catch { /* keep status message */ }
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export function jsonBody(method: string, value: unknown): RequestInit {
  return { method, body: JSON.stringify(value) }
}

export function eventUrl(workspaceId: string, afterId = 0): string {
  return `${API_BASE}/api/events?workspace_id=${encodeURIComponent(workspaceId)}&after_id=${afterId}`
}
