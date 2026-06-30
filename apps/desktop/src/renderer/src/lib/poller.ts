// Poll gate (docs/prod/desktop-plan.md): tick only while the window is visible and the bridge is connected.
// Pure logic (no document/window) so gating is verifiable in vitest's node environment.

export interface PollerOptions {
  intervalMs: number
  /** Called once immediately when the gate opens, then every intervalMs. */
  tick: () => void
}

export interface Poller {
  setVisible: (visible: boolean) => void
  setConnected: (connected: boolean) => void
  isActive: () => boolean
  /** After dispose, all further state changes are ignored. */
  dispose: () => void
}

export function createPoller(options: PollerOptions): Poller {
  let visible = false
  let connected = false
  let disposed = false
  let timer: ReturnType<typeof setInterval> | null = null

  // Tick immediately when the gate opens to fill the hidden/disconnected gap;
  // clear the interval when it closes to avoid background wakeups.
  const evaluate = (): void => {
    const open = visible && connected && !disposed
    if (open && timer === null) {
      options.tick()
      timer = setInterval(options.tick, options.intervalMs)
    } else if (!open && timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    setVisible(next: boolean): void {
      if (disposed || visible === next) return
      visible = next
      evaluate()
    },
    setConnected(next: boolean): void {
      if (disposed || connected === next) return
      connected = next
      evaluate()
    },
    isActive(): boolean {
      return timer !== null
    },
    dispose(): void {
      disposed = true
      if (timer !== null) {
        clearInterval(timer)
        timer = null
      }
    }
  }
}
