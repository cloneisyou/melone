/*
 * Shared unknown-error narrowing: `catch` and rejection values are typed
 * `unknown`, and several modules need the same human-readable conversion.
 */

/** Human-readable message from any thrown/rejected value. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
