// Shared lifecycle for a one-shot daemon mutation triggered from the UI
// (start/stop service, seed demo, …): track an in-flight flag, clear the prior
// error on each attempt, and capture failures as a human-readable message.
import { useCallback, useState } from 'react'
import { humanErrorMessage } from './daemon'

export interface DaemonAction {
  /** True while an invocation is in flight — disable the trigger control. */
  pending: boolean
  /** Message from the last failed invocation, or null. */
  error: string | null
  /** Clear the current error. */
  clearError: () => void
  /** Run a daemon mutation; pending toggles around it and errors are captured. */
  run: (action: () => Promise<void>) => Promise<void>
}

export function useDaemonAction(): DaemonAction {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(async (action: () => Promise<void>): Promise<void> => {
    setPending(true)
    setError(null)
    try {
      await action()
    } catch (caught) {
      setError(humanErrorMessage(caught))
    } finally {
      setPending(false)
    }
  }, [])

  const clearError = useCallback((): void => {
    setError(null)
  }, [])

  return { pending, error, clearError, run }
}
