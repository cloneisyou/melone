/*
 * Validation + envelope conversion for melone:request, extracted from ipc.ts
 * so vitest can drive it without Electron (open-target.ts pattern). Errors are
 * serialized as an envelope because contextBridge strips custom Error
 * properties (code etc.).
 */
import { JSON_RPC_ERROR, RpcError } from './rpc-client'

export interface BridgeErrorPayload {
  code: number
  /** Machine-readable symbol (e.g. "INVALID_PARAMS") for renderer branching. */
  message: string
  /** Human-readable (Korean) text from the daemon. */
  data?: unknown
}

export type BridgeRequestEnvelope =
  | { ok: true; result: unknown }
  | { ok: false; error: BridgeErrorPayload }

/** The only PythonBridge surface this module needs — tests inject a fake. */
export interface BridgeRequester {
  request: (method: string, params?: Record<string, unknown>) => Promise<unknown>
}

export async function handleBridgeRequest(
  bridge: BridgeRequester,
  method: unknown,
  params: unknown
): Promise<BridgeRequestEnvelope> {
  if (typeof method !== 'string' || method === '') {
    return {
      ok: false,
      error: {
        code: JSON_RPC_ERROR.invalidRequest,
        message: 'INVALID_REQUEST',
        data: 'method는 비어 있지 않은 문자열이어야 합니다'
      }
    }
  }
  if (
    params !== undefined &&
    (typeof params !== 'object' || params === null || Array.isArray(params))
  ) {
    return {
      ok: false,
      error: {
        code: JSON_RPC_ERROR.invalidParams,
        message: 'INVALID_PARAMS',
        data: 'params는 객체이거나 생략해야 합니다'
      }
    }
  }
  try {
    const result = await bridge.request(method, params as Record<string, unknown> | undefined)
    return { ok: true, result }
  } catch (error) {
    if (error instanceof RpcError) {
      return {
        ok: false,
        error: { code: error.code, message: error.message, data: error.data }
      }
    }
    return {
      ok: false,
      error: {
        code: JSON_RPC_ERROR.internalError,
        message: 'INTERNAL_ERROR',
        data: error instanceof Error ? error.message : String(error)
      }
    }
  }
}
