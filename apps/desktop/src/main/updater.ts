/*
 * Auto-update manager — wraps electron-updater's autoUpdater and broadcasts a
 * single UpdateState to the renderer. The renderer banner appears ONLY when the
 * state is something other than idle/checking/none, so the user sees an update
 * button strictly when a new version is actually available.
 *
 * Imports no electron / electron-updater modules: the real autoUpdater is
 * injected by index.ts (same pattern as python.ts injecting spawn), keeping the
 * mapping/flow logic testable in vitest without pulling Electron.
 *
 * RELEASE PIPELINE (public GitHub Releases) — the delivery half lives in:
 *   - apps/desktop/electron-builder.yml: publish { provider: github, owner:
 *     cloneisyou, repo: melone }. electron-builder uploads installers,
 *     blockmaps, and channel manifests to a public GitHub Release, and the
 *     packaged app reads that provider metadata from app-update.yml.
 *     generateUpdatesFilesForAllChannels emits latest*.yml (stable) and
 *     beta*.yml (prerelease) manifests.
 *   - .github/workflows/release.yml: a clone maintainer manually dispatches
 *     either the stable `latest` channel or `beta` prerelease channel. Each
 *     platform uploads to a single DRAFT release; a final promote job flips it
 *     public only if every build succeeded, so a failed build never ships an
 *     update. Version is auto-derived per run (extraMetadata.version).
 *   - Python daemon bundling: built per-OS with PyInstaller, staged into
 *     resources/melone-daemon, bundled via electron-builder extraResources;
 *     index.ts (resolveDaemonSpawn) runs that standalone exe in packaged builds
 *     (dev still uses `python -m melone_service.rpc`). One update replaces the
 *     shell + daemon atomically, so a single install ships every feature.
 *
 *   macOS (arm64): zip (required for Squirrel.Mac) + dmg. Code signing +
 *     notarization are MANDATORY (Developer ID Application cert + notarytool API
 *     key) — Squirrel.Mac refuses unsigned updates. CI runner: macos-latest.
 *   Windows (x64): nsis, unsigned (updates still work; SmartScreen warns). CI
 *     runner: windows-latest.
 *   Auto-check is gated in index.ts: the real autoUpdater attaches in packaged
 *   builds on an allowed platform. Dev builds keep the real updater detached.
 *
 * electron-updater selects the right per-OS/channel manifest automatically, so the
 * runtime code below stays platform-agnostic. In dev the real autoUpdater path is
 * inert; use MELONE_FAKE_UPDATE=1 to drive the renderer flow with simulated states.
 */

import { Emitter } from './emitter'
import { errorMessage } from './errors'

// Re-exported for the existing public surface (and its test); the implementation
// now lives in errors.ts alongside the other unknown-error helpers.
export { errorMessage }

/** Version label used by the dev simulation (MELONE_FAKE_UPDATE=1). */
export const FAKE_UPDATE_VERSION = '9.9.9'

/**
 * Single source of truth for the renderer. idle/checking/none render nothing —
 * the banner only shows from `available` onward.
 */
export type UpdateState =
  | { phase: 'idle' }
  | { phase: 'checking' }
  | { phase: 'none' } // up to date
  | { phase: 'available'; version: string }
  | { phase: 'downloading'; percent: number }
  | { phase: 'downloaded'; version: string }
  | { phase: 'error'; message: string }

// Pure event -> state mappers (the vitest target).
export function availableState(info: { version: string }): UpdateState {
  return { phase: 'available', version: info.version }
}

export function progressState(progress: { percent: number }): UpdateState {
  // electron-updater reports a float 0..100; round for a stable display value.
  return { phase: 'downloading', percent: Math.round(progress.percent) }
}

export function downloadedState(info: { version: string }): UpdateState {
  return { phase: 'downloaded', version: info.version }
}

/**
 * Minimal shape of electron-updater's autoUpdater that this module uses. index.ts
 * passes the real autoUpdater cast to this; tests pass a stub.
 */
export interface AppUpdaterLike {
  autoDownload: boolean
  autoInstallOnAppQuit: boolean
  on(event: string, listener: (...args: unknown[]) => void): void
  checkForUpdates(): Promise<unknown>
  downloadUpdate(): Promise<unknown>
  quitAndInstall(): void
}

export interface UpdateManagerOptions {
  /** app.isPackaged — the real updater path runs only in packaged builds. */
  isPackaged: boolean
  /** process.env.MELONE_FAKE_UPDATE === '1' — drive simulated states in dev. */
  fakeUpdate?: boolean
  /** Real autoUpdater (injected by index.ts) or a test stub. */
  updater?: AppUpdaterLike
  /** Timer seam for the dev simulation. Defaults to a self-unref'd setTimeout. */
  setTimer?: (fn: () => void, ms: number) => void
}

export class UpdateManager {
  private readonly isPackaged: boolean
  private readonly fakeUpdate: boolean
  private readonly setTimer: (fn: () => void, ms: number) => void
  private readonly stateChanges = new Emitter<UpdateState>()
  private updater: AppUpdaterLike | null = null
  private state: UpdateState = { phase: 'idle' }
  // Errors surface only when the user started the flow (check/download); a failed
  // background check stays silent so the banner does not pop up unprompted.
  private userInitiated = false

  constructor(options: UpdateManagerOptions) {
    this.isPackaged = options.isPackaged
    this.fakeUpdate = options.fakeUpdate ?? false
    this.setTimer =
      options.setTimer ??
      ((fn, ms) => {
        const timer = setTimeout(fn, ms)
        timer.unref?.()
      })
    // Only attach the real updater when it will actually be used.
    if (this.isPackaged && !this.fakeUpdate && options.updater !== undefined) {
      this.updater = options.updater
      this.attach(this.updater)
    }
  }

  getState(): UpdateState {
    return this.state
  }

  /** Subscribe to state changes; the returned function unsubscribes. */
  onStateChange(listener: (state: UpdateState) => void): () => void {
    return this.stateChanges.subscribe(listener)
  }

  /** Check for updates. Background checks (startup) pass userInitiated: false. */
  check(options?: { userInitiated?: boolean }): void {
    this.userInitiated = options?.userInitiated ?? false
    if (this.fakeUpdate) {
      this.fakeCheck()
      return
    }
    if (this.updater === null) {
      // Dev (unpackaged) without simulation: nothing to do, keep the banner hidden.
      this.setState({ phase: 'idle' })
      return
    }
    this.setState({ phase: 'checking' })
    this.updater.checkForUpdates().catch((err) => {
      this.handleError(err)
    })
  }

  /** Start downloading the available update (always a user action). */
  download(): void {
    this.userInitiated = true
    if (this.fakeUpdate) {
      this.fakeDownload()
      return
    }
    if (this.updater === null) return
    this.setState({ phase: 'downloading', percent: 0 })
    this.updater.downloadUpdate().catch((err) => {
      this.handleError(err)
    })
  }

  /** Quit and install the downloaded update (restarts the app). */
  quitAndInstall(): void {
    // No-op in dev/simulation — there is no packaged binary to relaunch into.
    if (this.updater === null) return
    this.updater.quitAndInstall()
  }

  private attach(updater: AppUpdaterLike): void {
    // The button controls download/install; do not auto-download in the background.
    updater.autoDownload = false
    updater.autoInstallOnAppQuit = true
    updater.on('checking-for-update', () => {
      this.setState({ phase: 'checking' })
    })
    updater.on('update-available', (info) => {
      this.setState(availableState(info as { version: string }))
    })
    updater.on('update-not-available', () => {
      this.userInitiated = false
      this.setState({ phase: 'none' })
    })
    updater.on('download-progress', (progress) => {
      this.setState(progressState(progress as { percent: number }))
    })
    updater.on('update-downloaded', (info) => {
      this.setState(downloadedState(info as { version: string }))
    })
    updater.on('error', (err) => {
      this.handleError(err)
    })
  }

  private handleError(err: unknown): void {
    if (!this.userInitiated) {
      // Silent: a background check failed. Drop back to a hidden state.
      if (this.state.phase === 'checking') this.setState({ phase: 'idle' })
      return
    }
    this.setState({ phase: 'error', message: errorMessage(err) })
  }

  private fakeCheck(): void {
    this.setState({ phase: 'checking' })
    this.setTimer(() => {
      this.setState({ phase: 'available', version: FAKE_UPDATE_VERSION })
    }, 600)
  }

  private fakeDownload(): void {
    const steps = [0, 25, 50, 75, 100]
    steps.forEach((percent, index) => {
      this.setTimer(
        () => {
          if (percent < 100) {
            this.setState({ phase: 'downloading', percent })
          } else {
            this.setState({ phase: 'downloaded', version: FAKE_UPDATE_VERSION })
          }
        },
        400 * (index + 1)
      )
    })
  }

  private setState(next: UpdateState): void {
    if (JSON.stringify(next) === JSON.stringify(this.state)) return
    this.state = next
    this.stateChanges.emit(next)
  }
}
