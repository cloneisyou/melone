/*
 * Opens a clicked search result (URLs in the default browser; apps activated
 * via macOS `open -a`). Imports no Electron modules (python.ts style):
 * shell.openExternal is injected by index.ts, and the decision logic
 * (resolveOpenAction) stays a pure function for vitest.
 */
import { spawn } from 'node:child_process'

/** Open target — same kind taxonomy as context.graph nodes (url | app_window | app). */
export interface OpenTarget {
  kind: 'url' | 'app_window' | 'app'
  url: string | null
  appName: string | null
}

export interface OpenResult {
  ok: boolean
  reason: 'opened' | 'unsupported' | 'invalid'
}

export type OpenAction =
  | { action: 'external'; url: string }
  | { action: 'mac-app'; appName: string }
  | { action: 'none'; reason: 'unsupported' | 'invalid' }

const TARGET_KINDS: ReadonlyArray<OpenTarget['kind']> = ['url', 'app_window', 'app']

/** Treat empty/whitespace-only values as absent — collected data may contain blank urls. */
function normalize(value: string | null): string | null {
  if (value === null) return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/**
 * Validate an arbitrary IPC value into an OpenTarget (pure function).
 * Main re-validates because calls could bypass the preload even with isolation.
 */
export function parseOpenTarget(value: unknown): OpenTarget | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const candidate = value as Record<string, unknown>
  if (!TARGET_KINDS.includes(candidate['kind'] as OpenTarget['kind'])) return null
  const url = candidate['url']
  const appName = candidate['appName']
  if (url !== null && typeof url !== 'string') return null
  if (appName !== null && typeof appName !== 'string') return null
  return {
    kind: candidate['kind'] as OpenTarget['kind'],
    url: url as string | null,
    appName: appName as string | null
  }
}

/**
 * Decide how to execute the target (pure function).
 *  - http/https URLs go external — other schemes (file://, javascript:, ...)
 *    are blocked as invalid: local file execution / script injection risk.
 *  - With no url and an appName, mac-app on darwin only — app activation on
 *    other platforms is out of MVP scope, hence unsupported.
 */
export function resolveOpenAction(
  target: Pick<OpenTarget, 'url' | 'appName'>,
  platform: NodeJS.Platform
): OpenAction {
  const url = normalize(target.url)
  if (url !== null) {
    let protocol: string
    try {
      protocol = new URL(url).protocol
    } catch {
      return { action: 'none', reason: 'invalid' }
    }
    if (protocol === 'http:' || protocol === 'https:') return { action: 'external', url }
    return { action: 'none', reason: 'invalid' }
  }

  const appName = normalize(target.appName)
  if (appName !== null) {
    if (platform === 'darwin') return { action: 'mac-app', appName }
    return { action: 'none', reason: 'unsupported' }
  }

  return { action: 'none', reason: 'invalid' }
}

export interface OpenTargetDeps {
  platform: NodeJS.Platform
  /** electron shell.openExternal — injected because this module does not import electron. */
  openExternal: (url: string) => Promise<void>
  /** Test injection point. Defaults to child_process.spawn. */
  spawnFn?: typeof spawn
}

/** Run macOS `open -a <app>`. Exit code 0 means activated; anything else (e.g. app missing) fails. */
function activateMacApp(appName: string, spawnFn: typeof spawn): Promise<boolean> {
  return new Promise((resolve) => {
    const child = spawnFn('open', ['-a', appName], { stdio: 'ignore' })
    // spawn itself failed (e.g. ENOENT) — not darwin, or `open` is missing.
    child.on('error', () => {
      resolve(false)
    })
    child.on('exit', (code) => {
      resolve(code === 0)
    })
  })
}

/** Execute the resolved action. Failures are reported via reason instead of throwing. */
export async function openTarget(target: OpenTarget, deps: OpenTargetDeps): Promise<OpenResult> {
  const action = resolveOpenAction(target, deps.platform)
  switch (action.action) {
    case 'external':
      try {
        await deps.openExternal(action.url)
        return { ok: true, reason: 'opened' }
      } catch {
        // The OS refused to open it (e.g. no handler) — treat as a bad target.
        return { ok: false, reason: 'invalid' }
      }
    case 'mac-app': {
      const activated = await activateMacApp(action.appName, deps.spawnFn ?? spawn)
      return activated ? { ok: true, reason: 'opened' } : { ok: false, reason: 'unsupported' }
    }
    case 'none':
      return { ok: false, reason: action.reason }
  }
}
