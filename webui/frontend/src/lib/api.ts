export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(message: string, status: number, payload: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export async function api<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')

  const response = await fetch(url, {
    ...init,
    headers,
    credentials: 'same-origin',
  })
  const payload: unknown = await response.json().catch(() => ({}))

  if (!response.ok) {
    const message =
      typeof payload === 'object' && payload !== null && 'error' in payload
        ? String(payload.error)
        : `HTTP ${response.status}`
    throw new ApiError(message, response.status, payload)
  }

  return payload as T
}
