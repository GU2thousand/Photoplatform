import test from 'node:test'
import assert from 'node:assert/strict'
import { uploadDirect } from '../src/directUpload.ts'

test('direct upload sends bytes only to the signed storage URL, then completes', async () => {
  const originalFetch = globalThis.fetch
  const events: string[] = []
  const file = new File(['image bytes'], 'photo.png', { type: 'image/png' })
  let creation: Record<string, unknown> = {}
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'https://storage.example/upload')
    assert.equal(options?.body, file)
    assert.equal(options?.credentials, 'omit')
    assert.equal(new Headers(options?.headers).has('Authorization'), false)
    events.push('put')
    return new Response('', { status: 200 })
  }
  try {
    const request = async <T>(path: string, options: RequestInit): Promise<T> => {
      events.push(path)
      if (path === '/api/uploads') {
        creation = JSON.parse(options.body as string)
        assert.equal(new Headers(options.headers).get('Idempotency-Key'), 'retry-key')
        return { uploadId: 'one', status: 'UPLOADING', uploadUrl: 'https://storage.example/upload', headers: { 'if-none-match': '*' } } as T
      }
      return { uploadId: 'one', status: 'PROCESSING' } as T
    }
    const result = await uploadDirect(file, { title: 'Photo' }, 'retry-key', request, () => {}, new AbortController().signal)
    assert.equal(result.status, 'PROCESSING')
    assert.equal(typeof creation.sha256, 'string')
    assert.equal((creation.sha256 as string).length, 64)
    assert.equal(creation.size, file.size)
    assert.deepEqual(events, ['/api/uploads', 'put', '/api/uploads/one/complete'])
  } finally { globalThis.fetch = originalFetch }
})

test('retrying an already completed session never uploads bytes again', async () => {
  let calls = 0
  const request = async <T>(): Promise<T> => { calls++; return { status: 'READY' } as T }
  const result = await uploadDirect(new File(['a'], 'a.png'), {}, 'key', request, () => {}, new AbortController().signal)
  assert.equal(result.status, 'READY')
  assert.equal(calls, 1)
})

test('a cancelled upload does not create a session', async () => {
  const controller = new AbortController(); controller.abort()
  let called = false
  await assert.rejects(uploadDirect(new File(['a'], 'a.png'), {}, 'key', async <T>() => { called = true; return {} as T }, () => {}, controller.signal))
  assert.equal(called, false)
})

test('cancelled session creation cannot proceed to storage even if the request resolves late', async () => {
  const controller = new AbortController()
  const originalFetch = globalThis.fetch
  let uploaded = false
  globalThis.fetch = async () => { uploaded = true; return new Response() }
  try {
    await assert.rejects(uploadDirect(new File(['a'], 'a.png'), {}, 'key', async <T>() => {
      controller.abort()
      return { status: 'UPLOADING', uploadUrl: 'https://storage.example/upload', headers: {} } as T
    }, () => {}, controller.signal), { name: 'AbortError' })
    assert.equal(uploaded, false)
  } finally { globalThis.fetch = originalFetch }
})

test('an existing checksum-bound object completes safely after a lost upload response', async () => {
  const originalFetch = globalThis.fetch
  const requests: string[] = []
  globalThis.fetch = async () => new Response('', { status: 412 })
  try {
    const response = await uploadDirect(new File(['a'], 'a.png'), {}, 'key', async <T>(path: string) => {
      requests.push(path)
      return (path === '/api/uploads'
        ? { uploadId: 'existing', status: 'UPLOADING', uploadUrl: 'https://storage.example/upload', headers: {} }
        : { status: 'PROCESSING' }) as T
    }, () => {}, new AbortController().signal)
    assert.equal(response.status, 'PROCESSING')
    assert.deepEqual(requests, ['/api/uploads', '/api/uploads/existing/complete'])
  } finally { globalThis.fetch = originalFetch }
})

test('failed storage writes never complete the session', async () => {
  const originalFetch = globalThis.fetch
  let completed = false
  globalThis.fetch = async () => new Response('', { status: 503 })
  try {
    await assert.rejects(uploadDirect(new File(['a'], 'a.png'), {}, 'key', async <T>(path: string) => {
      if (path !== '/api/uploads') completed = true
      return { uploadId: 'one', status: 'UPLOADING', uploadUrl: 'https://storage.example/upload', headers: {} } as T
    }, () => {}, new AbortController().signal), /503/)
    assert.equal(completed, false)
  } finally { globalThis.fetch = originalFetch }
})
