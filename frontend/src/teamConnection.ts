import type { TeamEvent } from './types'

export type ConnectionState = 'offline' | 'connecting' | 'connected' | 'reconnecting'

interface Options {
  requestTicket: (teamId: number, signal: AbortSignal) => Promise<string>
  openSocket: (teamId: number, ticket: string) => WebSocket
  onState: (state: ConnectionState) => void
  onMessage: (event: TeamEvent) => void
  onUnavailable: (error: unknown) => void
}

/** One team connection, with fresh short-lived credentials for every attempt. */
export function createTeamConnection(options: Options) {
  let teamId: number | null = null
  let online = true
  let generation = 0
  let failures = 0
  let socket: WebSocket | undefined
  let request: AbortController | undefined
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let connectTimer: ReturnType<typeof setTimeout> | undefined
  let state: ConnectionState = 'offline'

  function setState(next: ConnectionState) {
    state = next
    options.onState(next)
  }

  function cleanup() {
    generation++
    clearTimeout(retryTimer)
    clearTimeout(connectTimer)
    request?.abort()
    request = undefined
    if (socket) {
      socket.onopen = null
      socket.onclose = null
      socket.onerror = null
      socket.onmessage = null
      socket.close()
      socket = undefined
    }
  }

  function retry(error?: unknown) {
    cleanup()
    const status = typeof error === 'object' && error && 'status' in error ? error.status : null
    if (status === 401 || status === 403 || status === 404) {
      teamId = null
      setState('offline')
      options.onUnavailable(error)
      return
    }
    if (teamId === null || !online) {
      setState('offline')
      return
    }
    setState('reconnecting')
    const delay = Math.min(1_000 * 2 ** Math.min(failures++, 5), 30_000)
    retryTimer = setTimeout(() => { void connect() }, delay)
  }

  async function connect() {
    if (teamId === null || !online) return
    const currentGeneration = ++generation
    const selectedTeam = teamId
    request = new AbortController()
    setState(failures ? 'reconnecting' : 'connecting')
    // Also bounds a hung ticket request or a WebSocket handshake that never opens.
    connectTimer = setTimeout(() => retry(), 10_000)
    try {
      const ticket = await options.requestTicket(selectedTeam, request.signal)
      if (currentGeneration !== generation) return
      const opened = options.openSocket(selectedTeam, ticket)
      socket = opened
      opened.onopen = () => {
        if (currentGeneration !== generation) return
        clearTimeout(connectTimer)
        failures = 0
        setState('connected')
      }
      opened.onclose = () => {
        if (currentGeneration === generation) retry()
      }
      opened.onerror = () => {
        if (currentGeneration === generation) retry()
      }
      opened.onmessage = (message) => {
        if (currentGeneration !== generation) return
        try {
          const event = JSON.parse(String(message.data)) as TeamEvent
          if (event && typeof event.type === 'string' && typeof event.message === 'string') {
            options.onMessage(event)
          }
        } catch {
          // Ignore malformed events without dropping a healthy connection.
        }
      }
    } catch (error) {
      if (currentGeneration === generation) retry(error)
    }
  }

  return {
    start(selectedTeam: number) {
      cleanup()
      teamId = selectedTeam
      failures = 0
      if (online) void connect()
      else setState('offline')
    },
    stop() {
      cleanup()
      teamId = null
      failures = 0
      setState('offline')
    },
    setOnline(value: boolean) {
      if (online === value) return
      online = value
      cleanup()
      if (value && teamId !== null) {
        failures = 0
        void connect()
      } else setState('offline')
    },
    send(message: string): boolean {
      if (state !== 'connected' || socket?.readyState !== 1) return false
      try {
        socket.send(message)
        return true
      } catch (error) {
        retry(error)
        return false
      }
    },
  }
}
