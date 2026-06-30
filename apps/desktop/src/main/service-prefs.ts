/*
 * Service preferences persistence — currently just the daemon power toggle.
 * Mirrors window-state.ts: a tiny JSON file in userData, sanitized on read so a
 * corrupt or partial file falls back to defaults. Decision logic stays pure for
 * vitest. The pause/recording state is NOT here — it lives in the daemon's pause
 * flag file (service.pause/resume), which already persists across restarts.
 */
import { readFileSync, writeFileSync } from 'node:fs'

export interface ServicePrefs {
  /** When false, the app launches with the RPC daemon off and keeps it off. */
  daemonEnabled: boolean
  /** Set once the first-run onboarding has been completed (or skipped). */
  onboardingComplete: boolean
}

export const DEFAULT_SERVICE_PREFS: ServicePrefs = { daemonEnabled: true, onboardingComplete: false }

/** Coerce an arbitrary parsed value into valid prefs, defaulting missing fields. */
export function sanitizeServicePrefs(raw: unknown): ServicePrefs {
  if (typeof raw !== 'object' || raw === null) return { ...DEFAULT_SERVICE_PREFS }
  const candidate = raw as { daemonEnabled?: unknown; onboardingComplete?: unknown }
  return {
    daemonEnabled:
      typeof candidate.daemonEnabled === 'boolean'
        ? candidate.daemonEnabled
        : DEFAULT_SERVICE_PREFS.daemonEnabled,
    onboardingComplete:
      typeof candidate.onboardingComplete === 'boolean'
        ? candidate.onboardingComplete
        : DEFAULT_SERVICE_PREFS.onboardingComplete
  }
}

/** Read prefs; defaults when missing or corrupt (treated as a first run). */
export function loadServicePrefs(filePath: string): ServicePrefs {
  try {
    return sanitizeServicePrefs(JSON.parse(readFileSync(filePath, 'utf8')))
  } catch {
    return { ...DEFAULT_SERVICE_PREFS }
  }
}

/** Synchronous write is fine for one small JSON. */
export function saveServicePrefs(filePath: string, prefs: ServicePrefs): void {
  try {
    writeFileSync(filePath, JSON.stringify(prefs, null, 2), 'utf8')
  } catch {
    // A failed save only means the next launch may fall back to defaults.
  }
}
