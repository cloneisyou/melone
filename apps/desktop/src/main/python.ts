/*
 * Python RPC daemon supervisor — spawn, exponential-backoff respawn, shutdown cleanup.
 * Imports no Electron modules: values like app.isPackaged are injected by the
 * caller (index.ts), keeping the pure logic testable in vitest.
 */
import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { Emitter } from './emitter'
import { RpcClient } from './rpc-client'

/** Daemon spawn args: <python> -m melone_service.rpc */
export const RPC_DAEMON_ARGS = ['-m', 'melone_service.rpc']

export const MAX_RESPAWN_ATTEMPTS = 5
export const REQUEST_TIMEOUT_MS = 30_000
const BASE_RESPAWN_DELAY_MS = 1000
const MAX_RESPAWN_DELAY_MS = 4000
/** Ring buffer of recent daemon stderr lines, surfaced on the final down transition. */
export const STDERR_RING_SIZE = 20
const STDERR_DETAIL_LINES = 3
/** Grace period for the collector-stop RPC and the daemon's own exit during quit. */
const SHUTDOWN_TIMEOUT_MS = 3000

/**
 * Delay before the nth consecutive respawn (pure function).
 * Grows 1s -> 2s -> 4s and caps at 4s; past MAX_RESPAWN_ATTEMPTS it returns
 * null (give up = bridge down). A successful ping makes the caller reset the
 * failure counter so the next failure starts at 1s again.
 */
export function respawnDelayMs(failureCount: number): number | null {
  if (failureCount < 1 || failureCount > MAX_RESPAWN_ATTEMPTS) return null
  return Math.min(MAX_RESPAWN_DELAY_MS, BASE_RESPAWN_DELAY_MS * 2 ** (failureCount - 1))
}

export interface ResolvePythonOptions {
  env: NodeJS.ProcessEnv
  platform: NodeJS.Platform
  /** app.isPackaged — packaged builds skip the repo venv candidate. */
  isPackaged: boolean
  /** Absolute path to the dev repo's apps/service. Null in packaged builds. */
  serviceDir: string | null
  /** Test injection point. Defaults to fs.existsSync. */
  exists?: (candidate: string) => boolean
}

/** Resolve the python command (pure function). Priority: MELONE_PYTHON > dev venv > PATH python. */
export function resolvePythonCommand(options: ResolvePythonOptions): string {
  const exists = options.exists ?? existsSync
  const fromEnv = options.env['MELONE_PYTHON']?.trim()
  if (fromEnv !== undefined && fromEnv !== '') return fromEnv
  if (!options.isPackaged && options.serviceDir !== null) {
    const venvPython =
      options.platform === 'win32'
        ? path.join(options.serviceDir, '.venv', 'Scripts', 'python.exe')
        : path.join(options.serviceDir, '.venv', 'bin', 'python')
    if (exists(venvPython)) return venvPython
  }
  return 'python'
}

export interface ResolveDaemonOptions extends ResolvePythonOptions {
  /**
   * process.resourcesPath in packaged builds — electron-builder bundles the
   * standalone PyInstaller daemon under resources/melone-daemon there. Null in dev.
   */
  resourcesPath: string | null
}

/** Spawn descriptor: the executable plus its argv. */
export interface DaemonSpawn {
  command: string
  args: string[]
}

/**
 * Resolve how to spawn the RPC daemon (pure function).
 * Packaged builds run the bundled standalone PyInstaller executable (the user has
 * no Python, and it is not a module so it takes no `-m` args). Dev runs the
 * resolved python with `-m melone_service.rpc`.
 */
export function resolveDaemonSpawn(options: ResolveDaemonOptions): DaemonSpawn {
  if (options.isPackaged && options.resourcesPath !== null) {
    const exe = options.platform === 'win32' ? 'melone-daemon.exe' : 'melone-daemon'
    return { command: path.join(options.resourcesPath, 'melone-daemon', exe), args: [] }
  }
  return { command: resolvePythonCommand(options), args: [...RPC_DAEMON_ARGS] }
}

function delayResolve(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    timer.unref?.()
  })
}

/** Resolve true if the child exits within `ms`, false on timeout. */
function waitForExit(child: ChildProcess, ms: number): Promise<boolean> {
  return new Promise((resolve) => {
    // Already exited: the 'exit' event would never fire again, so resolve now
    // instead of waiting out the timeout and force-killing a dead process.
    if (child.exitCode != null || child.signalCode != null) {
      resolve(true)
      return
    }
    let settled = false
    const finish = (value: boolean): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => finish(false), ms)
    timer.unref?.()
    child.once('exit', () => finish(true))
  })
}

/** Force/tree-kill a stuck daemon so no spawned descendant lingers on the DB lock. */
function killProcessTree(child: ChildProcess): void {
  const pid = child.pid
  if (process.platform === 'win32' && pid !== undefined) {
    try {
      // child.kill() TerminateProcess's only the direct child; /T kills the tree.
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
      return
    } catch {
      // Fall through to the direct kill below.
    }
  }
  child.kill('SIGKILL')
}

export type BridgeStatus = 'connecting' | 'connected' | 'down' | 'disabled'

export interface BridgeState {
  status: BridgeStatus
  pid: number | null
  /** Short retry/down reason for UI captions. Null while connected. */
  detail: string | null
}

export interface PythonBridgeOptions {
  command: string
  args?: string[]
  cwd?: string
  requestTimeoutMs?: number
  /** Test injection point. Defaults to child_process.spawn. */
  spawnFn?: typeof spawn
  /** Test injection point. Defaults to a taskkill /T (win) | SIGKILL tree-kill. */
  killTree?: (child: ChildProcess) => void
}

export class PythonBridge {
  private readonly command: string
  private readonly args: string[]
  private readonly cwd: string | undefined
  private readonly spawnFn: typeof spawn
  private readonly killTree: (child: ChildProcess) => void
  private readonly client: RpcClient
  private readonly stateChanges = new Emitter<BridgeState>()
  private child: ChildProcess | null = null
  private restartTimer: ReturnType<typeof setTimeout> | null = null
  private failureCount = 0
  private stopping = false
  // User toggled the daemon off (distinct from `stopping`, which is app quit).
  // While true, exits never respawn and start() is a no-op until enable().
  private userDisabled = false
  private state: BridgeState = { status: 'connecting', pid: null, detail: null }
  /** Last STDERR_RING_SIZE complete stderr lines — packaged builds lose tracebacks otherwise. */
  private stderrRing: string[] = []
  private stderrPartial = ''

  constructor(options: PythonBridgeOptions) {
    this.command = options.command
    this.args = options.args ?? [...RPC_DAEMON_ARGS]
    this.cwd = options.cwd
    this.spawnFn = options.spawnFn ?? spawn
    this.killTree = options.killTree ?? killProcessTree
    this.client = new RpcClient({
      timeoutMs: options.requestTimeoutMs ?? REQUEST_TIMEOUT_MS
    })
  }

  start(): void {
    // Honor a user-off state: the launch path must not spawn when disabled.
    if (this.userDisabled) return
    if (this.child !== null || this.restartTimer !== null) return
    this.stopping = false
    // A manual restart after "down" gets a fresh respawn budget.
    this.failureCount = 0
    this.spawnChild()
  }

  /**
   * User turned the daemon off: kill the child and stay down — no respawn —
   * until enable(). Distinct from stop() (app quit) and from a crash ('down'),
   * so the UI can show an explicit "off by user" state. Safe with no child
   * (launch-disabled): just records the flag and the 'disabled' state.
   */
  disable(): void {
    this.userDisabled = true
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer)
      this.restartTimer = null
    }
    const child = this.child
    this.child = null
    this.client.detach('데몬이 꺼져 있습니다')
    if (child !== null) {
      child.stdin?.end()
      child.kill()
    }
    this.setState({ status: 'disabled', pid: null, detail: null })
  }

  /** User turned the daemon back on: fresh respawn budget, then spawn. */
  enable(): void {
    this.userDisabled = false
    if (this.child !== null || this.restartTimer !== null) return
    this.failureCount = 0
    this.spawnChild()
  }

  /** Whether the user has the daemon enabled (false only after disable()). */
  isEnabled(): boolean {
    return !this.userDisabled
  }

  /** Electron shutdown path: close stdin to trigger the daemon's stdin-EOF self-exit, then kill. */
  stop(): void {
    this.stopping = true
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer)
      this.restartTimer = null
    }
    const child = this.child
    this.child = null
    this.client.detach('앱 종료')
    if (child !== null) {
      child.stdin?.end()
      child.kill()
    }
  }

  /**
   * Graceful shutdown for app quit / auto-update install. On macOS, ask the
   * daemon to stop the collector it spawned FIRST (`service.stop`) so the
   * collector cannot outlive the daemon holding the SQLite lock; then close
   * stdin (the daemon self-exits on EOF, running its own collector cleanup as a
   * backstop) and wait for it to exit, force/tree-killing only if it overstays.
   * `service.stop` rejects off macOS / when not running — fine, it is
   * best-effort. Call once on quit.
   */
  async shutdown(timeoutMs: number = SHUTDOWN_TIMEOUT_MS): Promise<void> {
    this.stopping = true
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer)
      this.restartTimer = null
    }
    const child = this.child
    this.child = null
    // No daemon (down/disabled): nothing to stop and no RPC channel, so skip the
    // service.stop round-trip entirely and return at once.
    if (child === null) {
      this.client.detach('앱 종료')
      return
    }
    const stopRequest = this.client.request('service.stop').catch(() => undefined)
    await Promise.race([stopRequest, delayResolve(timeoutMs)])
    this.client.detach('앱 종료')
    child.stdin?.end()
    if (!(await waitForExit(child, timeoutMs))) this.killTree(child)
  }

  request(method: string, params?: Record<string, unknown>): Promise<unknown> {
    return this.client.request(method, params)
  }

  getState(): BridgeState {
    return this.state
  }

  /** Subscribe to state changes; the returned function unsubscribes. */
  onStateChange(listener: (state: BridgeState) => void): () => void {
    return this.stateChanges.subscribe(listener)
  }

  private setState(next: BridgeState): void {
    const current = this.state
    if (
      current.status === next.status &&
      current.pid === next.pid &&
      current.detail === next.detail
    ) {
      return
    }
    this.state = next
    this.stateChanges.emit(next)
  }

  private spawnChild(): void {
    this.restartTimer = null
    this.setState({ status: 'connecting', pid: null, detail: this.state.detail })

    const child = this.spawnFn(this.command, this.args, {
      cwd: this.cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        ...process.env,
        // Block-buffered stdout in pipe mode would batch and delay responses.
        PYTHONUNBUFFERED: '1',
        // Pin protocol stdio to UTF-8 even on legacy Windows code pages.
        PYTHONUTF8: '1'
      }
    })
    this.child = child

    // Daemon logs are stderr-only — not protocol, so stream them to the dev console.
    child.stderr?.on('data', (chunk: Buffer) => {
      process.stderr.write(chunk)
      this.recordStderr(chunk)
    })

    child.on('error', (error) => {
      this.handleChildDown(child, `python 실행 실패: ${error.message}`)
    })
    child.on('exit', (code, signal) => {
      this.handleChildDown(child, `데몬 종료 (code ${String(code)}, signal ${String(signal)})`)
    })

    if (child.stdin === null || child.stdout === null) {
      // Kill first so a child alive without pipes cannot linger as an orphan
      // (symmetric with the handshake-failure path).
      child.kill()
      this.handleChildDown(child, '데몬 stdio 파이프를 열지 못했습니다')
      return
    }
    this.client.attach(child.stdin, child.stdout)
    void this.handshake(child)
  }

  private async handshake(child: ChildProcess): Promise<void> {
    try {
      await this.client.request('app.ping')
    } catch {
      // Alive but unresponsive (timeout): kill so the exit path's backoff respawn takes over.
      if (this.child === child && !this.stopping) child.kill()
      return
    }
    if (this.child !== child || this.stopping) return
    this.failureCount = 0
    this.setState({ status: 'connected', pid: child.pid ?? null, detail: null })
  }

  /** Accumulate stderr into the line ring, holding partial lines until their newline arrives. */
  private recordStderr(chunk: Buffer): void {
    const text = this.stderrPartial + chunk.toString('utf8')
    const lines = text.split('\n')
    this.stderrPartial = lines.pop() ?? ''
    for (const rawLine of lines) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      if (line.trim() === '') continue
      this.stderrRing.push(line)
      if (this.stderrRing.length > STDERR_RING_SIZE) this.stderrRing.shift()
    }
  }

  private stderrTail(): string {
    return this.stderrRing.slice(-STDERR_DETAIL_LINES).join(' | ')
  }

  private handleChildDown(child: ChildProcess, detail: string): void {
    // Ignore late events from an already-replaced child (e.g. exit after error).
    if (this.child !== child) return
    this.child = null
    this.client.detach(detail)
    // Neither app quit (stopping) nor a user-off (userDisabled) respawns.
    if (this.stopping || this.userDisabled) return
    this.failureCount += 1
    const delay = respawnDelayMs(this.failureCount)
    if (delay === null) {
      const tail = this.stderrTail()
      this.setState({
        status: 'down',
        pid: null,
        detail: tail === '' ? detail : `${detail} — stderr: ${tail}`
      })
      return
    }
    this.setState({ status: 'connecting', pid: null, detail })
    const timer = setTimeout(() => {
      this.spawnChild()
    }, delay)
    timer.unref?.()
    this.restartTimer = timer
  }
}
