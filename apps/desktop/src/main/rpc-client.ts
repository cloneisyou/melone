/*
 * Newline-delimited JSON-RPC 2.0 client between Electron and the Python daemon.
 * Depends only on Node stream interfaces, so vitest drives it with PassThrough.
 */
import { errorMessage } from './errors'

/** Daemon error codes (JSON-RPC standard + app-specific) plus client-local codes. */
export const JSON_RPC_ERROR = {
  parseError: -32700,
  invalidRequest: -32600,
  methodNotFound: -32601,
  invalidParams: -32602,
  internalError: -32603,
  notSupportedOnPlatform: -32001,
  configParseError: -32003,
  serviceError: -32004,
  /** Client-local (made on the Electron side, never by the daemon): request timeout. */
  clientTimeout: -32098,
  /** Client-local: no connection, or the connection dropped. */
  clientDisconnected: -32099
} as const

export class RpcError extends Error {
  readonly code: number
  readonly data: unknown

  constructor(code: number, message: string, data?: unknown) {
    super(message)
    this.name = 'RpcError'
    this.code = code
    this.data = data
  }
}

const NEWLINE = 0x0a
const CARRIAGE_RETURN = 0x0d

/**
 * Newline framing parser. Accumulates bytes and splits on \n, so partial
 * chunks, multiple messages per chunk, and UTF-8 multibyte boundaries are all
 * safe (UTF-8 continuation bytes are 0x80–0xBF and never collide with 0x0A).
 */
export class LineBuffer {
  private buffer: Buffer = Buffer.alloc(0)

  /** Accumulate a chunk and return the completed lines as UTF-8 strings. */
  push(chunk: Buffer): string[] {
    this.buffer = this.buffer.length === 0 ? chunk : Buffer.concat([this.buffer, chunk])
    const lines: string[] = []
    let start = 0
    for (;;) {
      const newlineIndex = this.buffer.indexOf(NEWLINE, start)
      if (newlineIndex === -1) break
      let end = newlineIndex
      // Strip \r so CRLF from Windows pipes cannot break the protocol.
      if (end > start && this.buffer[end - 1] === CARRIAGE_RETURN) end -= 1
      lines.push(this.buffer.subarray(start, end).toString('utf8'))
      start = newlineIndex + 1
    }
    if (start > 0) this.buffer = this.buffer.subarray(start)
    return lines
  }

  reset(): void {
    this.buffer = Buffer.alloc(0)
  }
}

interface PendingRequest {
  resolve: (value: unknown) => void
  reject: (reason: RpcError) => void
  timer: ReturnType<typeof setTimeout>
}

export interface RpcClientOptions {
  /** Per-request timeout. Defaults to 10s. */
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 10_000

export class RpcClient {
  private readonly timeoutMs: number
  private readonly pending = new Map<number, PendingRequest>()
  private readonly lineBuffer = new LineBuffer()
  private nextId = 1
  private writable: NodeJS.WritableStream | null = null
  private readable: NodeJS.ReadableStream | null = null
  private dataListener: ((chunk: Buffer | string) => void) | null = null

  constructor(options: RpcClientOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  }

  get attached(): boolean {
    return this.writable !== null
  }

  /** Attach to the daemon's stdin/stdout, detaching any previous connection first. */
  attach(stdin: NodeJS.WritableStream, stdout: NodeJS.ReadableStream): void {
    if (this.writable !== null) this.detach('새 데몬 연결로 교체')
    this.lineBuffer.reset()
    this.writable = stdin
    this.readable = stdout
    this.dataListener = (chunk) => {
      this.handleChunk(chunk)
    }
    stdout.on('data', this.dataListener)
  }

  /** Detach and reject every pending request. */
  detach(reason = '데몬 연결이 끊어졌습니다'): void {
    if (this.readable !== null && this.dataListener !== null) {
      this.readable.removeListener('data', this.dataListener)
    }
    this.readable = null
    this.writable = null
    this.dataListener = null
    this.lineBuffer.reset()
    this.rejectAll(new RpcError(JSON_RPC_ERROR.clientDisconnected, reason))
  }

  /** One JSON-RPC request. Timeouts, disconnects, and daemon error responses reject with RpcError. */
  request(method: string, params?: Record<string, unknown>): Promise<unknown> {
    const writable = this.writable
    if (writable === null) {
      return Promise.reject(
        new RpcError(JSON_RPC_ERROR.clientDisconnected, '데몬에 연결되어 있지 않습니다')
      )
    }
    const id = this.nextId++
    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(
          new RpcError(
            JSON_RPC_ERROR.clientTimeout,
            `${method} 응답이 ${String(this.timeoutMs / 1000)}초 안에 오지 않았습니다`
          )
        )
      }, this.timeoutMs)
      // Keep the timeout timer from holding Electron open (absent under fake timers).
      timer.unref?.()
      this.pending.set(id, { resolve, reject, timer })
      const message =
        params === undefined
          ? { jsonrpc: '2.0' as const, id, method }
          : { jsonrpc: '2.0' as const, id, method, params }
      try {
        writable.write(`${JSON.stringify(message)}\n`)
      } catch (error) {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(
          new RpcError(
            JSON_RPC_ERROR.clientDisconnected,
            `요청을 보내지 못했습니다: ${errorMessage(error)}`
          )
        )
      }
    })
  }

  private rejectAll(error: RpcError): void {
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer)
      entry.reject(error)
    }
    this.pending.clear()
  }

  private handleChunk(chunk: Buffer | string): void {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, 'utf8')
    for (const line of this.lineBuffer.push(buffer)) {
      this.handleLine(line)
    }
  }

  private handleLine(line: string): void {
    if (line.trim() === '') return
    let parsed: unknown
    try {
      parsed = JSON.parse(line)
    } catch {
      // Drop non-protocol stdout output — daemon logs are stderr-only.
      return
    }
    if (typeof parsed !== 'object' || parsed === null) return
    const message = parsed as { id?: unknown; result?: unknown; error?: unknown }
    if (typeof message.id !== 'number') return
    const entry = this.pending.get(message.id)
    // Ignore late responses already cleaned up (e.g. after a timeout).
    if (entry === undefined) return
    this.pending.delete(message.id)
    clearTimeout(entry.timer)
    const error = toRpcError(message.error)
    if (error !== null) entry.reject(error)
    else entry.resolve(message.result)
  }
}

function toRpcError(value: unknown): RpcError | null {
  if (typeof value !== 'object' || value === null) return null
  const shape = value as { code?: unknown; message?: unknown; data?: unknown }
  const code = typeof shape.code === 'number' ? shape.code : JSON_RPC_ERROR.internalError
  const message =
    typeof shape.message === 'string' ? shape.message : '데몬이 알 수 없는 오류를 반환했습니다'
  return new RpcError(code, message, shape.data)
}
