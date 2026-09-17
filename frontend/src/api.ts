const API_BASE = import.meta.env.DEV ? 'http://127.0.0.1:5177' : ''

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

type ApiRequestInit = RequestInit & { timeoutMs?: number }

export async function api<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), init?.timeoutMs ?? 30000)
  let response: Response
  try {
    response = await fetch(apiUrl(path), { ...init, headers, signal: init?.signal || controller.signal })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw new Error('本机请求超时，请检查任务状态后重试')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
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
