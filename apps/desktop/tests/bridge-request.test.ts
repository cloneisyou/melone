import { describe, expect, it } from 'vitest'
import { handleBridgeRequest, type BridgeRequester } from '../src/main/bridge-request'
import { JSON_RPC_ERROR, RpcError } from '../src/main/rpc-client'

// The bridge surface is injected (open-target.ts pattern) — no Electron needed.
function fakeBridge(impl: BridgeRequester['request']): BridgeRequester {
  return { request: impl }
}

const okBridge = fakeBridge(async () => ({ pong: true }))

describe('handleBridgeRequest', () => {
  it('rejects a non-string or empty method with INVALID_REQUEST', async () => {
    for (const method of [undefined, null, 42, {}, '']) {
      const envelope = await handleBridgeRequest(okBridge, method, undefined)
      expect(envelope).toEqual({
        ok: false,
        error: {
          code: JSON_RPC_ERROR.invalidRequest,
          message: 'INVALID_REQUEST',
          data: 'method는 비어 있지 않은 문자열이어야 합니다'
        }
      })
    }
  })

  it('rejects non-object params (primitives, null, arrays) with INVALID_PARAMS', async () => {
    for (const params of ['x', 1, null, [1, 2]]) {
      const envelope = await handleBridgeRequest(okBridge, 'app.ping', params)
      expect(envelope).toEqual({
        ok: false,
        error: {
          code: JSON_RPC_ERROR.invalidParams,
          message: 'INVALID_PARAMS',
          data: 'params는 객체이거나 생략해야 합니다'
        }
      })
    }
  })

  it('wraps a successful bridge result as { ok: true, result }', async () => {
    const calls: Array<[string, Record<string, unknown> | undefined]> = []
    const bridge = fakeBridge(async (method, params) => {
      calls.push([method, params])
      return { version: '1.0' }
    })
    const envelope = await handleBridgeRequest(bridge, 'app.ping', undefined)
    expect(envelope).toEqual({ ok: true, result: { version: '1.0' } })
    expect(calls).toEqual([['app.ping', undefined]])
  })

  it('passes object params through to the bridge', async () => {
    const calls: Array<[string, Record<string, unknown> | undefined]> = []
    const bridge = fakeBridge(async (method, params) => {
      calls.push([method, params])
      return []
    })
    await handleBridgeRequest(bridge, 'context.rank', { limit: 5 })
    expect(calls).toEqual([['context.rank', { limit: 5 }]])
  })

  it('serializes an RpcError as { code, message: symbol, data } preserving all fields', async () => {
    const bridge = fakeBridge(async () => {
      throw new RpcError(JSON_RPC_ERROR.invalidParams, 'INVALID_PARAMS', 'query가 비어 있습니다')
    })
    const envelope = await handleBridgeRequest(bridge, 'context.search', { query: '' })
    expect(envelope).toEqual({
      ok: false,
      error: {
        code: JSON_RPC_ERROR.invalidParams,
        message: 'INVALID_PARAMS',
        data: 'query가 비어 있습니다'
      }
    })
  })

  it('maps non-RpcError failures to INTERNAL_ERROR with the message in data', async () => {
    const bridge = fakeBridge(async () => {
      throw new Error('boom')
    })
    const envelope = await handleBridgeRequest(bridge, 'app.ping', undefined)
    expect(envelope).toEqual({
      ok: false,
      error: {
        code: JSON_RPC_ERROR.internalError,
        message: 'INTERNAL_ERROR',
        data: 'boom'
      }
    })
  })

  it('stringifies non-Error throw values', async () => {
    const bridge = fakeBridge(async () => {
      throw 'raw failure'
    })
    const envelope = await handleBridgeRequest(bridge, 'app.ping', undefined)
    expect(envelope).toEqual({
      ok: false,
      error: {
        code: JSON_RPC_ERROR.internalError,
        message: 'INTERNAL_ERROR',
        data: 'raw failure'
      }
    })
  })
})
