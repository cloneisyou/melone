import { EventEmitter } from 'node:events'
import type { spawn } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import {
  openTarget,
  parseOpenTarget,
  resolveOpenAction,
  type OpenTarget
} from '../src/main/open-target'

function target(partial: Partial<OpenTarget>): OpenTarget {
  return { kind: 'url', url: null, appName: null, ...partial }
}

// Target -> action decision: http/https URLs only, app activation macOS-only (security/MVP scope).
describe('resolveOpenAction', () => {
  it('resolves http/https URLs to external (default browser)', () => {
    expect(resolveOpenAction(target({ url: 'http://example.com' }), 'win32')).toEqual({
      action: 'external',
      url: 'http://example.com'
    })
    expect(resolveOpenAction(target({ url: 'https://example.com/path?q=1' }), 'darwin')).toEqual({
      action: 'external',
      url: 'https://example.com/path?q=1'
    })
  })

  it('blocks non-http/https schemes such as file and javascript as invalid', () => {
    // Local file execution / script injection risk.
    expect(resolveOpenAction(target({ url: 'file:///etc/passwd' }), 'darwin')).toEqual({
      action: 'none',
      reason: 'invalid'
    })
    expect(resolveOpenAction(target({ url: 'javascript:alert(1)' }), 'darwin')).toEqual({
      action: 'none',
      reason: 'invalid'
    })
  })

  it('treats unparseable URL strings as invalid', () => {
    expect(resolveOpenAction(target({ url: 'not a url' }), 'darwin')).toEqual({
      action: 'none',
      reason: 'invalid'
    })
  })

  it('activates via mac-app on darwin when there is an appName and no url', () => {
    expect(resolveOpenAction(target({ appName: 'Safari' }), 'darwin')).toEqual({
      action: 'mac-app',
      appName: 'Safari'
    })
  })

  it('reports app activation as unsupported off darwin', () => {
    expect(resolveOpenAction(target({ appName: 'Code' }), 'win32')).toEqual({
      action: 'none',
      reason: 'unsupported'
    })
    expect(resolveOpenAction(target({ appName: 'Code' }), 'linux')).toEqual({
      action: 'none',
      reason: 'unsupported'
    })
  })

  it('is invalid when both url and appName are missing', () => {
    expect(resolveOpenAction(target({}), 'darwin')).toEqual({ action: 'none', reason: 'invalid' })
  })

  it('prefers url over appName when both are present', () => {
    expect(resolveOpenAction(target({ url: 'https://a.com', appName: 'Safari' }), 'win32')).toEqual(
      { action: 'external', url: 'https://a.com' }
    )
  })

  it('treats empty/whitespace-only urls as absent and falls through to appName', () => {
    expect(resolveOpenAction(target({ url: '  ', appName: 'Safari' }), 'darwin')).toEqual({
      action: 'mac-app',
      appName: 'Safari'
    })
  })
})

// Validation of arbitrary IPC values — main blocks preload-bypassing calls again.
describe('parseOpenTarget', () => {
  it('passes well-shaped objects through as OpenTarget', () => {
    expect(
      parseOpenTarget({ kind: 'url', url: 'https://example.com', appName: null })
    ).toEqual({ kind: 'url', url: 'https://example.com', appName: null })
  })

  it('rejects non-objects, null, and arrays', () => {
    expect(parseOpenTarget(null)).toBeNull()
    expect(parseOpenTarget('https://example.com')).toBeNull()
    expect(parseOpenTarget([])).toBeNull()
  })

  it('rejects unknown kinds and wrong field types', () => {
    expect(parseOpenTarget({ kind: 'shell', url: null, appName: 'Safari' })).toBeNull()
    expect(parseOpenTarget({ kind: 'url', url: 42, appName: null })).toBeNull()
    expect(parseOpenTarget({ kind: 'app', url: null, appName: { name: 'x' } })).toBeNull()
  })
})

// Execution — Electron shell and spawn are verified through injected mocks (python.ts test style).
describe('openTarget', () => {
  // Fake spawn imitating an `open -a` result with the given exit code.
  function fakeSpawn(record: { args?: string[] }, exitCode: number | 'error'): typeof spawn {
    return ((command: string, args: string[]) => {
      record.args = [command, ...args]
      const child = new EventEmitter()
      queueMicrotask(() => {
        if (exitCode === 'error') child.emit('error', new Error('spawn ENOENT'))
        else child.emit('exit', exitCode)
      })
      return child
    }) as unknown as typeof spawn
  }

  it('external: calls openExternal and returns opened', async () => {
    const opened: string[] = []
    const result = await openTarget(target({ url: 'https://example.com' }), {
      platform: 'win32',
      openExternal: async (url) => {
        opened.push(url)
      }
    })
    expect(result).toEqual({ ok: true, reason: 'opened' })
    expect(opened).toEqual(['https://example.com'])
  })

  it('external: reports invalid when openExternal rejects (does not throw)', async () => {
    const result = await openTarget(target({ url: 'https://example.com' }), {
      platform: 'darwin',
      openExternal: async () => {
        throw new Error('no handler')
      }
    })
    expect(result).toEqual({ ok: false, reason: 'invalid' })
  })

  it('mac-app: returns opened when open -a <app> exits with 0', async () => {
    const record: { args?: string[] } = {}
    const result = await openTarget(target({ appName: 'Safari' }), {
      platform: 'darwin',
      openExternal: async () => undefined,
      spawnFn: fakeSpawn(record, 0)
    })
    expect(result).toEqual({ ok: true, reason: 'opened' })
    expect(record.args).toEqual(['open', '-a', 'Safari'])
  })

  it('mac-app: reports unsupported when open fails (app missing or spawn error)', async () => {
    const recordExit: { args?: string[] } = {}
    expect(
      await openTarget(target({ appName: 'NoSuchApp' }), {
        platform: 'darwin',
        openExternal: async () => undefined,
        spawnFn: fakeSpawn(recordExit, 1)
      })
    ).toEqual({ ok: false, reason: 'unsupported' })

    const recordError: { args?: string[] } = {}
    expect(
      await openTarget(target({ appName: 'Safari' }), {
        platform: 'darwin',
        openExternal: async () => undefined,
        spawnFn: fakeSpawn(recordError, 'error')
      })
    ).toEqual({ ok: false, reason: 'unsupported' })
  })

  it('none decision: executes nothing and only returns the reason', async () => {
    let called = false
    const record: { args?: string[] } = {}
    const result = await openTarget(target({ appName: 'Code' }), {
      platform: 'win32',
      openExternal: async () => {
        called = true
      },
      spawnFn: fakeSpawn(record, 0)
    })
    expect(result).toEqual({ ok: false, reason: 'unsupported' })
    expect(called).toBe(false)
    expect(record.args).toBeUndefined()
  })
})
