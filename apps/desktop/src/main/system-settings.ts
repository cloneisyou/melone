/*
 * Deep-link into a macOS System Settings privacy pane (Screen Recording /
 * Accessibility) so onboarding can send the user straight to the right toggle.
 * Kept separate from open-target.ts on purpose: that module's strict http(s)-only
 * allowlist must stay intact, while this one is an allowlist of known Apple
 * `x-apple.systempreferences:` deep links and nothing else. shell.openExternal is
 * injected by index.ts (this module imports no electron), and the pane→URL map
 * stays a pure lookup for vitest.
 */

/** Privacy panes the onboarding flow can deep-link into. */
export type SettingsPane = 'screen-recording' | 'accessibility'

export interface OpenSettingsResult {
  ok: boolean
  reason: 'opened' | 'unsupported' | 'invalid'
}

// The `Privacy_*` anchor selects the specific list inside Privacy & Security.
const PANE_URLS: Record<SettingsPane, string> = {
  'screen-recording':
    'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
  accessibility: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
}

const PANES = Object.keys(PANE_URLS) as SettingsPane[]

/**
 * Validate an arbitrary IPC value into a known pane (pure function). Main
 * re-validates because calls could bypass the preload even with isolation.
 */
export function parseSettingsPane(value: unknown): SettingsPane | null {
  return typeof value === 'string' && (PANES as string[]).includes(value)
    ? (value as SettingsPane)
    : null
}

export interface OpenSettingsDeps {
  platform: NodeJS.Platform
  /** electron shell.openExternal — injected because this module does not import electron. */
  openExternal: (url: string) => Promise<void>
}

/** Open the pane. macOS only; other platforms have no equivalent deep link. */
export async function openSystemSettings(
  pane: SettingsPane,
  deps: OpenSettingsDeps
): Promise<OpenSettingsResult> {
  if (deps.platform !== 'darwin') return { ok: false, reason: 'unsupported' }
  try {
    await deps.openExternal(PANE_URLS[pane])
    return { ok: true, reason: 'opened' }
  } catch {
    // The OS refused the deep link (e.g. unknown anchor on a future macOS).
    return { ok: false, reason: 'invalid' }
  }
}
