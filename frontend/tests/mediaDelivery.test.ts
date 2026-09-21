import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchProtectedAsset } from '../src/api.ts'

Object.defineProperty(globalThis, 'window', { value: { location: { origin: 'https://app.example' } }, configurable: true })

test('signed media uses authenticated URL resolution without fetching storage with the bearer token', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async (path, options) => {
    calls++
    assert.equal(path, '/api/files/7/url?variant=thumbnail')
    assert.equal(new Headers(options?.headers).get('Authorization'), 'Bearer private-token')
    assert.equal(options?.cache, 'no-store')
    return Response.json({ legacy: false, url: 'https://storage.example/signed' })
  }
  try {
    assert.equal(await fetchProtectedAsset('/api/files/7/thumbnail', 'private-token', new AbortController().signal), 'https://storage.example/signed')
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

test('a late signed URL is discarded after cancellation', async () => {
  const original = globalThis.fetch
  const controller = new AbortController()
  globalThis.fetch = async () => {
    controller.abort()
    return Response.json({ legacy: false, url: 'https://storage.example/signed' })
  }
  try {
    await assert.rejects(fetchProtectedAsset('/api/files/7', 'private-token', controller.signal), { name: 'AbortError' })
  } finally { globalThis.fetch = original }
})

test('absolute external assets never receive the application bearer token', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async (_path, options) => {
    assert.equal(new Headers(options?.headers).has('Authorization'), false)
    assert.equal(options?.credentials, 'omit')
    return new Response('image bytes')
  }
  try {
    const url = await fetchProtectedAsset('https://storage.example/object', 'private-token', new AbortController().signal)
    assert.ok(url.startsWith('blob:'))
    URL.revokeObjectURL(url)
  } finally { globalThis.fetch = original }
})
