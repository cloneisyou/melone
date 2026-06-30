import { PassThrough } from 'node:stream'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { JSON_RPC_ERROR, RpcClient, RpcError } from '../src/main/rpc-client'

interface Harness {
  client: RpcClient
  stdout: PassThrough
  sent: string[]
}

function createHarness(timeoutMs?: number): Harness {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const client = new RpcClient(timeoutMs === undefined ? {} : { timeoutMs })
  client.attach(stdin, stdout)
  const sent: string[] = []
  stdin.on('data', (chunk: Buffer) => {
    sent.push(chunk.toString('utf8'))
  })
  return { client, stdout, sent }
}

function respond(stdout: PassThrough, message: unknown): void {
  stdout.write(`${JSON.stringify(message)}\n`)
}

afterEach(() => {
  vi.useRealTimers()
})

describe('RpcClient', () => {
  it('sends requests as single newline-terminated JSON-RPC 2.0 lines', async () => {
    const { client, stdout, sent } = createHarness()
    const promise = client.request('context.rank', { sinceMinutes: 120, limit: 5 })
    respond(stdout, { jsonrpc: '2.0', id: 1, result: [] })
    await promise
    expect(sent.join('')).toBe(
      '{"jsonrpc":"2.0","id":1,"method":"context.rank","params":{"sinceMinutes":120,"limit":5}}\n'
    )
  })

  it('omits the params key when no params are given', async () => {
    const { client, stdout, sent } = createHarness()
    const promise = client.request('app.ping')
    respond(stdout, { jsonrpc: '2.0', id: 1, result: { version: '0.1.0' } })
    await expect(promise).resolves.toEqual({ version: '0.1.0' })
    expect(sent.join('')).toBe('{"jsonrpc":"2.0","id":1,"method":"app.ping"}\n')
  })

  it('resolves with the result even when the response arrives in partial chunks', async () => {
    const { client, stdout } = createHarness()
    const promise = client.request('context.current')
    const payload = `${JSON.stringify({ jsonrpc: '2.0', id: 1, result: { app: '크롬' } })}\n`
    const encoded = Buffer.from(payload, 'utf8')
    stdout.write(encoded.subarray(0, 10))
    stdout.write(encoded.subarray(10))
    await expect(promise).resolves.toEqual({ app: '크롬' })
  })

  it('routes multiple responses packed into one chunk to their requests', async () => {
    const { client, stdout } = createHarness()
    const first = client.request('app.ping')
    const second = client.request('service.status')
    stdout.write(
      `${JSON.stringify({ jsonrpc: '2.0', id: 2, result: 'second' })}\n` +
        `${JSON.stringify({ jsonrpc: '2.0', id: 1, result: 'first' })}\n`
    )
    await expect(second).resolves.toBe('second')
    await expect(first).resolves.toBe('first')
  })

  it('rejects with an RpcError carrying code and data on error responses', async () => {
    const { client, stdout } = createHarness()
    const promise = client.request('service.start')
    respond(stdout, {
      jsonrpc: '2.0',
      id: 1,
      error: {
        code: JSON_RPC_ERROR.notSupportedOnPlatform,
        message: 'NOT_SUPPORTED_ON_PLATFORM',
        data: '이 플랫폼에서는 수집 서비스를 시작할 수 없습니다'
      }
    })
    const error = await promise.catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(RpcError)
    expect((error as RpcError).code).toBe(JSON_RPC_ERROR.notSupportedOnPlatform)
    expect((error as RpcError).data).toBe('이 플랫폼에서는 수집 서비스를 시작할 수 없습니다')
  })

  it('ignores responses with unknown ids and non-protocol output', async () => {
    const { client, stdout } = createHarness()
    const promise = client.request('app.ping')
    stdout.write('데몬이 실수로 stdout에 찍은 로그\n')
    respond(stdout, { jsonrpc: '2.0', id: 99, result: '엉뚱한 응답' })
    respond(stdout, { jsonrpc: '2.0', id: 1, result: 'pong' })
    await expect(promise).resolves.toBe('pong')
  })

  it('rejects with a timeout after 10s and removes the entry from pending', async () => {
    vi.useFakeTimers()
    const { client, stdout } = createHarness()
    const captured = client.request('app.ping').catch((reason: unknown) => reason)
    vi.advanceTimersByTime(10_000)
    const error = await captured
    expect(error).toBeInstanceOf(RpcError)
    expect((error as RpcError).code).toBe(JSON_RPC_ERROR.clientTimeout)

    // Removed from pending, so the late response cannot mix into the next request.
    vi.useRealTimers()
    const next = client.request('service.status')
    respond(stdout, { jsonrpc: '2.0', id: 1, result: '늦은 응답' })
    respond(stdout, { jsonrpc: '2.0', id: 2, result: 'status' })
    await expect(next).resolves.toBe('status')
  })

  it('does not reject when the response arrives before the timeout', async () => {
    vi.useFakeTimers()
    const { client, stdout } = createHarness()
    const promise = client.request('app.ping')
    respond(stdout, { jsonrpc: '2.0', id: 1, result: 'pong' })
    await expect(promise).resolves.toBe('pong')
    // The timer is cleared on response, so nothing happens as time passes.
    vi.advanceTimersByTime(60_000)
  })

  it('rejects every pending request when the connection drops', async () => {
    const { client } = createHarness()
    const first = client.request('app.ping').catch((reason: unknown) => reason)
    const second = client.request('context.current').catch((reason: unknown) => reason)
    client.detach('데몬 종료 (code 1, signal null)')
    const errors = await Promise.all([first, second])
    for (const error of errors) {
      expect(error).toBeInstanceOf(RpcError)
      expect((error as RpcError).code).toBe(JSON_RPC_ERROR.clientDisconnected)
      expect((error as RpcError).message).toContain('데몬 종료')
    }
  })

  it('rejects immediately when not attached', async () => {
    const client = new RpcClient()
    const error = await client.request('app.ping').catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(RpcError)
    expect((error as RpcError).code).toBe(JSON_RPC_ERROR.clientDisconnected)
  })

  it('sends requests over the new streams after detach and re-attach', async () => {
    const { client } = createHarness()
    client.detach()
    const stdin = new PassThrough()
    const stdout = new PassThrough()
    client.attach(stdin, stdout)
    const promise = client.request('app.ping')
    respond(stdout, { jsonrpc: '2.0', id: 1, result: 'pong' })
    await expect(promise).resolves.toBe('pong')
  })
})
