import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createTeamConnection } from '../src/teamConnection.ts'

class FakeSocket {
  readyState = 0
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  sent: string[] = []
  closed = 0
  open() { this.readyState = 1; this.onopen?.() }
  fail() { this.readyState = 3; this.onclose?.() }
  close() { this.closed++; this.readyState = 3 }
  send(message: string) { this.sent.push(message) }
}

function setup(requestTicket?: (id: number, signal: AbortSignal) => Promise<string>) {
  const states: string[] = []
  const messages: unknown[] = []
  const errors: unknown[] = []
  const calls: { id: number; signal: AbortSignal }[] = []
  const opened: { id: number; ticket: string; socket: FakeSocket }[] = []
  const connection = createTeamConnection({
    requestTicket(id, signal) {
      calls.push({ id, signal })
      return requestTicket ? requestTicket(id, signal) : Promise.resolve(`ticket-${calls.length}`)
    },
    openSocket(id, ticket) {
      const socket = new FakeSocket()
      opened.push({ id, ticket, socket })
      return socket as unknown as WebSocket
    },
    onState: (state) => states.push(state),
    onMessage: (event) => messages.push(event),
    onUnavailable: (error) => errors.push(error),
  })
  return { connection, states, messages, errors, calls, opened }
}

const flush = async () => { await Promise.resolve(); await Promise.resolve() }

test('reports connected only after open; sends only while connected and tolerates malformed events', async () => {
  const h = setup()
  h.connection.start(1)
  assert.equal(h.states.at(-1), 'connecting')
  assert.equal(h.connection.send('Draft'), false)
  await flush()
  assert.equal(h.states.at(-1), 'connecting')
  const socket = h.opened[0]!.socket
  socket.open()
  assert.equal(h.states.at(-1), 'connected')
  assert.equal(h.connection.send('Hello'), true)
  assert.deepEqual(socket.sent, ['Hello'])
  socket.onmessage?.({ data: 'not json' })
  socket.onmessage?.({ data: JSON.stringify({ type: 'NOTE', message: 'Hello' }) })
  assert.equal(h.messages.length, 1)
  h.connection.stop()
  assert.equal(h.states.at(-1), 'offline')
  assert.equal(h.connection.send('After logout'), false)
  assert.equal(socket.closed, 1)
})

test('reconnects with fresh tickets using exponential backoff and resets after a successful open', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const h = setup()
  h.connection.start(1)
  await flush()
  h.opened[0]!.socket.fail()
  assert.equal(h.states.at(-1), 'reconnecting')
  t.mock.timers.tick(999)
  assert.equal(h.calls.length, 1)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(h.opened[1]!.ticket, 'ticket-2')
  h.opened[1]!.socket.fail()
  t.mock.timers.tick(1999)
  assert.equal(h.calls.length, 2)
  t.mock.timers.tick(1)
  await flush()
  h.opened[2]!.socket.open()
  h.opened[2]!.socket.fail()
  t.mock.timers.tick(1000)
  await flush()
  assert.equal(h.calls.length, 4)
  assert.equal(h.opened[3]!.ticket, 'ticket-4')
  h.connection.stop()
})

test('team switching and logout abort pending tickets and ignore late responses', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const resolvers: ((ticket: string) => void)[] = []
  const h = setup(() => new Promise((resolve) => resolvers.push(resolve)))
  h.connection.start(1)
  h.connection.start(2)
  assert.equal(h.calls[0]!.signal.aborted, true)
  resolvers[0]!('stale-ticket')
  await flush()
  assert.equal(h.opened.length, 0)
  resolvers[1]!('current-ticket')
  await flush()
  assert.equal(h.opened[0]!.id, 2)
  h.connection.stop()
  t.mock.timers.tick(120_000)
  assert.equal(h.calls.length, 2)
  assert.equal(h.opened[0]!.socket.closed, 1)
})

test('offline cancels retries and online resumes the selected team with a new ticket', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const h = setup()
  h.connection.start(7)
  await flush()
  h.opened[0]!.socket.open()
  h.connection.setOnline(false)
  assert.equal(h.states.at(-1), 'offline')
  assert.equal(h.connection.send('Keep draft'), false)
  t.mock.timers.tick(120_000)
  assert.equal(h.calls.length, 1)
  h.connection.setOnline(true)
  await flush()
  assert.equal(h.calls[1]!.id, 7)
  assert.equal(h.opened[1]!.ticket, 'ticket-2')
  h.connection.stop()
})

test('authorization failures stop retrying and report the unavailable connection', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const error = Object.assign(new Error('Session expired'), { status: 401 })
  const h = setup(async () => { throw error })
  h.connection.start(1)
  await flush()
  assert.equal(h.states.at(-1), 'offline')
  assert.deepEqual(h.errors, [error])
  t.mock.timers.tick(120_000)
  assert.equal(h.calls.length, 1)
  h.connection.stop()
})

test('a hung handshake is bounded and a delayed ticket cannot resurrect a stopped connection', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const resolvers: ((ticket: string) => void)[] = []
  const h = setup(() => new Promise((resolve) => resolvers.push(resolve)))
  h.connection.start(1)
  t.mock.timers.tick(10_000)
  assert.equal(h.calls[0]!.signal.aborted, true)
  assert.equal(h.states.at(-1), 'reconnecting')
  t.mock.timers.tick(1000)
  assert.equal(h.calls.length, 2)
  h.connection.stop()
  resolvers.forEach((resolve) => resolve('too-late'))
  await flush()
  assert.equal(h.opened.length, 0)
  assert.equal(h.states.at(-1), 'offline')
})
