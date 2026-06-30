import { EventEmitter } from 'node:events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  availableState,
  downloadedState,
  errorMessage,
  FAKE_UPDATE_VERSION,
  progressState,
  UpdateManager,
  type AppUpdaterLike,
  type UpdateState
} from '../src/main/updater'

// Pure event -> state mappers.
describe('update state mappers', () => {
  it('maps available / downloaded by version', () => {
    expect(availableState({ version: '1.2.3' })).toEqual({ phase: 'available', version: '1.2.3' })
    expect(downloadedState({ version: '1.2.3' })).toEqual({ phase: 'downloaded', version: '1.2.3' })
  })

  it('rounds the download percent for display', () => {
    expect(progressState({ percent: 42.7 })).toEqual({ phase: 'downloading', percent: 43 })
    expect(progressState({ percent: 0 })).toEqual({ phase: 'downloading', percent: 0 })
  })

  it('extracts a message from Error and non-Error values', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom')
    expect(errorMessage('nope')).toBe('nope')
  })
})

// autoUpdater stub: EventEmitter for on()/emit() plus the methods the manager calls.
class FakeUpdater extends EventEmitter {
  autoDownload = true
  autoInstallOnAppQuit = false
  checkForUpdates = vi.fn((): Promise<unknown> => Promise.resolve({}))
  downloadUpdate = vi.fn((): Promise<unknown> => Promise.resolve({}))
  quitAndInstall = vi.fn()
}

function packagedManager(): { manager: UpdateManager; fake: FakeUpdater; states: UpdateState[] } {
  const fake = new FakeUpdater()
  const manager = new UpdateManager({
    isPackaged: true,
    updater: fake as unknown as AppUpdaterLike,
    setTimer: (fn, ms) => {
      setTimeout(fn, ms)
    }
  })
  const states: UpdateState[] = []
  manager.onStateChange((state) => states.push(state))
  return { manager, fake, states }
}

describe('UpdateManager (real updater path)', () => {
  it('disables background auto-download on attach', () => {
    const { fake } = packagedManager()
    expect(fake.autoDownload).toBe(false)
    expect(fake.autoInstallOnAppQuit).toBe(true)
  })

  it('forwards updater events as state', () => {
    const { fake, states } = packagedManager()
    fake.emit('update-available', { version: '2.0.0' })
    fake.emit('download-progress', { percent: 50.4 })
    fake.emit('update-downloaded', { version: '2.0.0' })
    expect(states).toEqual([
      { phase: 'available', version: '2.0.0' },
      { phase: 'downloading', percent: 50 },
      { phase: 'downloaded', version: '2.0.0' }
    ])
  })

  it('surfaces an error from a user-initiated check', () => {
    const { manager, fake } = packagedManager()
    manager.check({ userInitiated: true })
    expect(fake.checkForUpdates).toHaveBeenCalledOnce()
    fake.emit('error', new Error('feed unreachable'))
    expect(manager.getState()).toEqual({ phase: 'error', message: 'feed unreachable' })
  })

  it('stays silent when a background check fails', () => {
    const { manager, fake } = packagedManager()
    manager.check({ userInitiated: false })
    expect(manager.getState()).toEqual({ phase: 'checking' })
    fake.emit('error', new Error('feed unreachable'))
    // Hidden again — no banner pops up from a background failure.
    expect(manager.getState()).toEqual({ phase: 'idle' })
  })

  it('downloads and installs through the updater', () => {
    const { manager, fake } = packagedManager()
    manager.download()
    expect(fake.downloadUpdate).toHaveBeenCalledOnce()
    expect(manager.getState()).toEqual({ phase: 'downloading', percent: 0 })
    manager.quitAndInstall()
    expect(fake.quitAndInstall).toHaveBeenCalledOnce()
  })

  it('stays idle in packaged builds when no public update feed is attached', () => {
    const manager = new UpdateManager({ isPackaged: true })
    manager.check({ userInitiated: true })
    expect(manager.getState()).toEqual({ phase: 'idle' })
    expect(() => manager.download()).not.toThrow()
    expect(() => manager.quitAndInstall()).not.toThrow()
  })
})

describe('UpdateManager (dev simulation, MELONE_FAKE_UPDATE)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  function fakeManager(): UpdateManager {
    return new UpdateManager({
      isPackaged: false,
      fakeUpdate: true,
      setTimer: (fn, ms) => {
        setTimeout(fn, ms)
      }
    })
  }

  it('simulates check -> available', async () => {
    const manager = fakeManager()
    manager.check()
    expect(manager.getState()).toEqual({ phase: 'checking' })
    await vi.advanceTimersByTimeAsync(600)
    expect(manager.getState()).toEqual({ phase: 'available', version: FAKE_UPDATE_VERSION })
  })

  it('simulates download progress through to downloaded', async () => {
    const manager = fakeManager()
    manager.download()
    await vi.advanceTimersByTimeAsync(2000)
    expect(manager.getState()).toEqual({ phase: 'downloaded', version: FAKE_UPDATE_VERSION })
  })

  it('does not touch a real updater when installing in dev', () => {
    const manager = fakeManager()
    // No updater attached in fake mode → quitAndInstall is a safe no-op.
    expect(() => manager.quitAndInstall()).not.toThrow()
  })
})
