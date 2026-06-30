import type { ChildProcess, spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MAX_RESPAWN_ATTEMPTS,
  PythonBridge,
  resolveDaemonSpawn,
  respawnDelayMs,
  resolvePythonCommand
} from '../src/main/python'

// Exponential backoff: 1s, 2s, 4s, at most 5 attempts.
describe('respawnDelayMs', () => {
  it('starts at 1s, doubles, and caps at 4s', () => {
    expect([1, 2, 3, 4, 5].map((count) => respawnDelayMs(count))).toEqual([
      1000, 2000, 4000, 4000, 4000
    ])
  })

  it('returns null past the max attempts (5) — transitions to bridge down', () => {
    expect(MAX_RESPAWN_ATTEMPTS).toBe(5)
    expect(respawnDelayMs(MAX_RESPAWN_ATTEMPTS + 1)).toBeNull()
    expect(respawnDelayMs(99)).toBeNull()
  })

  it('starts again at 1s after a successful ping resets the counter', () => {
    // PythonBridge performs the reset — for the pure function this equals failureCount back at 1.
    expect(respawnDelayMs(5)).toBe(4000)
    expect(respawnDelayMs(1)).toBe(1000)
  })

  it('returns null for counters below 1 (invalid input)', () => {
    expect(respawnDelayMs(0)).toBeNull()
  })
})

// Python command resolution: env MELONE_PYTHON > dev venv > PATH python.
describe('resolvePythonCommand', () => {
  const serviceDir = path.join('C:', 'repo', 'apps', 'service')

  it('uses the MELONE_PYTHON environment variable when set', () => {
    const command = resolvePythonCommand({
      env: { MELONE_PYTHON: '/usr/local/bin/python3.12' },
      platform: 'win32',
      isPackaged: false,
      serviceDir,
      exists: () => true
    })
    expect(command).toBe('/usr/local/bin/python3.12')
  })

  it('ignores a whitespace-only MELONE_PYTHON', () => {
    const command = resolvePythonCommand({
      env: { MELONE_PYTHON: '   ' },
      platform: 'win32',
      isPackaged: false,
      serviceDir,
      exists: () => false
    })
    expect(command).toBe('python')
  })

  it('uses the repo venv Scripts/python.exe on Windows in dev mode', () => {
    const expected = path.join(serviceDir, '.venv', 'Scripts', 'python.exe')
    const command = resolvePythonCommand({
      env: {},
      platform: 'win32',
      isPackaged: false,
      serviceDir,
      exists: (candidate) => candidate === expected
    })
    expect(command).toBe(expected)
  })

  it('uses the repo venv bin/python on unix in dev mode', () => {
    const expected = path.join(serviceDir, '.venv', 'bin', 'python')
    const command = resolvePythonCommand({
      env: {},
      platform: 'darwin',
      isPackaged: false,
      serviceDir,
      exists: (candidate) => candidate === expected
    })
    expect(command).toBe(expected)
  })

  it('skips the venv candidate in packaged builds and uses PATH python', () => {
    const command = resolvePythonCommand({
      env: {},
      platform: 'darwin',
      isPackaged: true,
      serviceDir: null,
      exists: () => true
    })
    expect(command).toBe('python')
  })

  it('falls back to PATH python when the venv does not exist', () => {
    const command = resolvePythonCommand({
      env: {},
      platform: 'win32',
      isPackaged: false,
      serviceDir,
      exists: () => false
    })
    expect(command).toBe('python')
  })
})

// Daemon spawn: packaged runs the bundled standalone exe (no args); dev runs python -m.
describe('resolveDaemonSpawn', () => {
  const serviceDir = path.join('C:', 'repo', 'apps', 'service')

  it('runs the bundled daemon executable with no args in packaged builds', () => {
    const resourcesPath = path.join('C:', 'app', 'resources')
    expect(
      resolveDaemonSpawn({
        env: {},
        platform: 'win32',
        isPackaged: true,
        serviceDir: null,
        resourcesPath,
        exists: () => true
      })
    ).toEqual({
      command: path.join(resourcesPath, 'melone-daemon', 'melone-daemon.exe'),
      args: []
    })
  })

  it('uses the extensionless executable name on unix', () => {
    const resourcesPath = path.join('/', 'app', 'Contents', 'Resources')
    const spawn = resolveDaemonSpawn({
      env: {},
      platform: 'darwin',
      isPackaged: true,
      serviceDir: null,
      resourcesPath,
      exists: () => true
    })
    expect(spawn.command).toBe(path.join(resourcesPath, 'melone-daemon', 'melone-daemon'))
    expect(spawn.args).toEqual([])
  })

  it('falls back to python -m melone_service.rpc in dev (unpackaged)', () => {
    expect(
      resolveDaemonSpawn({
        env: {},
        platform: 'darwin',
        isPackaged: false,
        serviceDir,
        resourcesPath: null,
        exists: () => false
      })
    ).toEqual({ command: 'python', args: ['-m', 'melone_service.rpc'] })
  })
})

// PythonBridge state machine, driven through the spawnFn injection hook with a
// fake ChildProcess (EventEmitter + PassThrough stdio) and fake timers.
class FakeChild extends EventEmitter {
  stdin = new PassThrough()
  stdout = new PassThrough()
  stderr = new PassThrough()
  pid = 4242
  killed = false

  kill(): boolean {
    this.killed = true
    return true
  }
}

const PING_TIMEOUT_MS = 100

function createHarness(): {
  bridge: PythonBridge
  children: FakeChild[]
  spawnCount: () => number
} {
  const children: FakeChild[] = []
  const spawnFn = ((): ChildProcess => {
    const child = new FakeChild()
    children.push(child)
    return child as unknown as ChildProcess
  }) as unknown as typeof spawn
  const bridge = new PythonBridge({
    command: 'python',
    requestTimeoutMs: PING_TIMEOUT_MS,
    spawnFn
  })
  return { bridge, children, spawnCount: () => children.length }
}

/** Answer every JSON-RPC request (the handshake ping) on the fake child's stdout. */
function respondToRequests(child: FakeChild): void {
  child.stdin.on('data', (chunk: Buffer) => {
    for (const line of chunk.toString('utf8').split('\n')) {
      if (line.trim() === '') continue
      const message = JSON.parse(line) as { id: number }
      child.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result: {} })}\n`)
    }
  })
}

/** Drain PassThrough data events — process.nextTick stays real under fake timers. */
async function flushStreams(): Promise<void> {
  for (let round = 0; round < 20; round += 1) {
    await new Promise<void>((resolve) => {
      process.nextTick(resolve)
    })
  }
}

describe('PythonBridge state machine', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('spawns only once when start() is called twice', () => {
    const { bridge, spawnCount } = createHarness()
    bridge.start()
    bridge.start()
    expect(spawnCount()).toBe(1)
    bridge.stop()
  })

  it('respawns with 1s/2s/4s backoff and goes down after the budget is spent', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    expect(spawnCount()).toBe(1)

    // failure 1 -> 1s delay
    children[0].emit('exit', 1, null)
    expect(bridge.getState().status).toBe('connecting')
    await vi.advanceTimersByTimeAsync(999)
    expect(spawnCount()).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(spawnCount()).toBe(2)

    // failure 2 -> 2s delay
    children[1].emit('exit', 1, null)
    await vi.advanceTimersByTimeAsync(1999)
    expect(spawnCount()).toBe(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(spawnCount()).toBe(3)

    // failures 3..5 -> capped 4s delays
    for (let attempt = 3; attempt <= 5; attempt += 1) {
      children[spawnCount() - 1].emit('exit', 1, null)
      await vi.advanceTimersByTimeAsync(3999)
      expect(spawnCount()).toBe(attempt)
      await vi.advanceTimersByTimeAsync(1)
      expect(spawnCount()).toBe(attempt + 1)
    }

    // failure 6 exceeds MAX_RESPAWN_ATTEMPTS -> down, no more respawns
    children[5].emit('exit', 1, null)
    expect(bridge.getState().status).toBe('down')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(spawnCount()).toBe(MAX_RESPAWN_ATTEMPTS + 1)
    bridge.stop()
  })

  it('does not respawn on a late exit event after stop()', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    const child = children[0]
    bridge.stop()
    expect(child.killed).toBe(true)
    child.emit('exit', 0, null)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(spawnCount()).toBe(1)
  })

  it('disable() kills the child, goes disabled, and suppresses respawn', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    const child = children[0]
    bridge.disable()
    expect(child.killed).toBe(true)
    expect(bridge.getState().status).toBe('disabled')
    // A late exit from the killed child must not schedule a respawn.
    child.emit('exit', 0, null)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(spawnCount()).toBe(1)
  })

  it('disable() with no running child just records the disabled state', () => {
    const { bridge, spawnCount } = createHarness()
    // Launch-disabled path: no child to kill, no spawn.
    bridge.disable()
    expect(spawnCount()).toBe(0)
    expect(bridge.getState().status).toBe('disabled')
  })

  it('start() is a no-op while disabled', () => {
    const { bridge, spawnCount } = createHarness()
    bridge.disable()
    bridge.start()
    expect(spawnCount()).toBe(0)
    expect(bridge.getState().status).toBe('disabled')
  })

  it('enable() restarts the daemon after disable()', () => {
    const { bridge, spawnCount } = createHarness()
    bridge.start()
    bridge.disable()
    expect(spawnCount()).toBe(1)
    bridge.enable()
    expect(spawnCount()).toBe(2)
    expect(bridge.getState().status).toBe('connecting')
    bridge.stop()
  })

  it('kills an unresponsive child on handshake timeout and joins the backoff path', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    const child = children[0]

    // No ping response -> client timeout fires -> bridge kills the child.
    await vi.advanceTimersByTimeAsync(PING_TIMEOUT_MS)
    expect(child.killed).toBe(true)

    child.emit('exit', null, 'SIGTERM')
    expect(bridge.getState().status).toBe('connecting')
    await vi.advanceTimersByTimeAsync(1000)
    expect(spawnCount()).toBe(2)
    bridge.stop()
  })

  it('resets the failure count after a successful ping', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()

    // failure 1 -> respawn at 1s
    children[0].emit('exit', 1, null)
    await vi.advanceTimersByTimeAsync(1000)
    expect(spawnCount()).toBe(2)

    // Second child answers the ping -> connected, counter reset.
    respondToRequests(children[1])
    await flushStreams()
    expect(bridge.getState()).toEqual({ status: 'connected', pid: 4242, detail: null })

    // Next failure starts the backoff at 1s again, not 2s.
    children[1].emit('exit', 1, null)
    await vi.advanceTimersByTimeAsync(999)
    expect(spawnCount()).toBe(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(spawnCount()).toBe(3)
    bridge.stop()
  })

  it('start() after down restores the full respawn budget', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    for (let index = 0; index < MAX_RESPAWN_ATTEMPTS + 1; index += 1) {
      children[spawnCount() - 1].emit('exit', 1, null)
      await vi.advanceTimersByTimeAsync(4000)
    }
    expect(bridge.getState().status).toBe('down')
    const downSpawns = spawnCount()

    bridge.start()
    expect(spawnCount()).toBe(downSpawns + 1)
    // With the counter reset, a failure schedules a respawn instead of going down.
    children[spawnCount() - 1].emit('exit', 1, null)
    expect(bridge.getState().status).toBe('connecting')
    await vi.advanceTimersByTimeAsync(1000)
    expect(spawnCount()).toBe(downSpawns + 2)
    bridge.stop()
  })

  it('appends the last stderr lines to the down detail', async () => {
    const { bridge, children, spawnCount } = createHarness()
    bridge.start()
    for (let index = 0; index < MAX_RESPAWN_ATTEMPTS + 1; index += 1) {
      const child = children[spawnCount() - 1]
      // Split writes exercise the partial-line accumulation.
      child.stderr.write(`Traceback line ${String(index)}`)
      child.stderr.write(': boom\n')
      await flushStreams()
      child.emit('exit', 1, null)
      await vi.advanceTimersByTimeAsync(4000)
    }
    const state = bridge.getState()
    expect(state.status).toBe('down')
    expect(state.detail).toContain('stderr:')
    expect(state.detail).toContain('Traceback line 5: boom')
    bridge.stop()
  })
})

// Graceful shutdown: stop the collector (service.stop) before tearing down the
// daemon, then wait for exit and tree-kill only if it overstays.
describe('PythonBridge.shutdown', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  function createShutdownHarness(): {
    bridge: PythonBridge
    children: FakeChild[]
    killTree: ReturnType<typeof vi.fn>
  } {
    const children: FakeChild[] = []
    const spawnFn = ((): ChildProcess => {
      const child = new FakeChild()
      children.push(child)
      return child as unknown as ChildProcess
    }) as unknown as typeof spawn
    const killTree = vi.fn()
    const bridge = new PythonBridge({
      command: 'python',
      requestTimeoutMs: PING_TIMEOUT_MS,
      spawnFn,
      killTree
    })
    return { bridge, children, killTree }
  }

  it('sends service.stop before exit and does not force-kill when the daemon exits', async () => {
    const { bridge, children, killTree } = createShutdownHarness()
    bridge.start()
    const child = children[0]
    const methods: string[] = []
    child.stdin.on('data', (chunk: Buffer) => {
      for (const line of chunk.toString('utf8').split('\n')) {
        if (line.trim() === '') continue
        const message = JSON.parse(line) as { id: number; method: string }
        methods.push(message.method)
        child.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: message.id, result: {} })}\n`)
      }
    })

    const done = bridge.shutdown(1000)
    await flushStreams()
    child.emit('exit', 0, null) // daemon self-exits on stdin EOF
    await done

    expect(methods).toContain('service.stop')
    expect(killTree).not.toHaveBeenCalled()
  })

  it('tree-kills the daemon when it overstays the shutdown timeout', async () => {
    const { bridge, children, killTree } = createShutdownHarness()
    bridge.start()
    const child = children[0]
    respondToRequests(child) // answer service.stop so we reach the wait-for-exit step

    const done = bridge.shutdown(50)
    await flushStreams()
    await vi.advanceTimersByTimeAsync(50) // no 'exit' arrives -> wait times out
    await done

    expect(killTree).toHaveBeenCalledWith(child)
  })

  it('does not force-kill when the daemon has already exited', async () => {
    const { bridge, children, killTree } = createShutdownHarness()
    bridge.start()
    const child = children[0]
    respondToRequests(child)
    // The daemon exited before waitForExit runs: its 'exit' event already fired,
    // so waitForExit must read exitCode rather than wait out the timeout + kill.
    ;(child as unknown as { exitCode: number | null }).exitCode = 0

    await bridge.shutdown(50)

    expect(killTree).not.toHaveBeenCalled()
  })

  it('returns at once without a service.stop request when there is no daemon', async () => {
    const { bridge, killTree } = createShutdownHarness()
    // Never start(): child is null (down/disabled). No RPC channel to use.
    await bridge.shutdown(50)
    expect(killTree).not.toHaveBeenCalled()
  })
})
