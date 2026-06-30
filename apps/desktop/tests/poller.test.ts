import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPoller, type Poller } from '../src/renderer/src/lib/poller'

const INTERVAL_MS = 5000

interface Harness {
  poller: Poller
  tick: ReturnType<typeof vi.fn>
}

function createHarness(): Harness {
  const tick = vi.fn()
  const poller = createPoller({ intervalMs: INTERVAL_MS, tick })
  return { poller, tick }
}

function activate(poller: Poller): void {
  poller.setVisible(true)
  poller.setConnected(true)
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('createPoller — poll gating (docs/prod/desktop-plan.md)', () => {
  it('does not tick before visible and connected', () => {
    const { poller, tick } = createHarness()
    expect(tick).not.toHaveBeenCalled()

    poller.setVisible(true)
    vi.advanceTimersByTime(INTERVAL_MS * 3)
    expect(tick).not.toHaveBeenCalled()
    expect(poller.isActive()).toBe(false)
  })

  it('ticks once immediately when the gate opens, then repeats every interval', () => {
    const { poller, tick } = createHarness()
    activate(poller)
    expect(tick).toHaveBeenCalledTimes(1)
    expect(poller.isActive()).toBe(true)

    vi.advanceTimersByTime(INTERVAL_MS)
    expect(tick).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(INTERVAL_MS * 2)
    expect(tick).toHaveBeenCalledTimes(4)
  })

  it('stops polling when hidden', () => {
    const { poller, tick } = createHarness()
    activate(poller)
    poller.setVisible(false)

    vi.advanceTimersByTime(INTERVAL_MS * 10)
    expect(tick).toHaveBeenCalledTimes(1)
    expect(poller.isActive()).toBe(false)
  })

  it('ticks immediately and resumes when visible again', () => {
    const { poller, tick } = createHarness()
    activate(poller)
    poller.setVisible(false)
    vi.advanceTimersByTime(INTERVAL_MS * 10)

    poller.setVisible(true)
    expect(tick).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(INTERVAL_MS)
    expect(tick).toHaveBeenCalledTimes(3)
  })

  it('stops when the bridge drops and resumes on recovery', () => {
    const { poller, tick } = createHarness()
    activate(poller)

    poller.setConnected(false)
    vi.advanceTimersByTime(INTERVAL_MS * 10)
    expect(tick).toHaveBeenCalledTimes(1)
    expect(poller.isActive()).toBe(false)

    poller.setConnected(true)
    expect(tick).toHaveBeenCalledTimes(2)
    expect(poller.isActive()).toBe(true)
  })

  it('does not poll while hidden even if the bridge recovers', () => {
    const { poller, tick } = createHarness()
    poller.setVisible(false)
    poller.setConnected(true)
    vi.advanceTimersByTime(INTERVAL_MS * 3)
    expect(tick).not.toHaveBeenCalled()
    expect(poller.isActive()).toBe(false)
  })

  it('does not duplicate intervals on repeated identical state pushes', () => {
    const { poller, tick } = createHarness()
    activate(poller)
    // onBridgeState may re-push the same connected state (e.g. on detail changes).
    poller.setVisible(true)
    poller.setConnected(true)
    expect(tick).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(INTERVAL_MS)
    expect(tick).toHaveBeenCalledTimes(2)
  })

  it('never ticks after dispose, regardless of state changes', () => {
    const { poller, tick } = createHarness()
    activate(poller)
    poller.dispose()
    expect(poller.isActive()).toBe(false)

    poller.setVisible(true)
    poller.setConnected(true)
    poller.setVisible(false)
    poller.setVisible(true)
    vi.advanceTimersByTime(INTERVAL_MS * 10)
    expect(tick).toHaveBeenCalledTimes(1)
  })
})
