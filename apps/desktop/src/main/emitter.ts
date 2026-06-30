/*
 * Tiny synchronous fan-out used by the state-holding main-process classes
 * (PythonBridge, UpdateManager). Each owns one Emitter and exposes its
 * subscribe() as the public onStateChange; the IPC layer
 * (registerStateBroadcast) re-broadcasts every emitted value to all windows.
 */
export class Emitter<T> {
  private readonly listeners = new Set<(value: T) => void>()

  /** Subscribe to emitted values; the returned function unsubscribes. */
  subscribe(listener: (value: T) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** Notify every current subscriber. */
  emit(value: T): void {
    for (const listener of this.listeners) listener(value)
  }
}
