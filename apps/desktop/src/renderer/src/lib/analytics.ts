// PostHog product analytics (renderer-only). No-op without a key.
// Privacy: autocapture is unmasked, so autocaptured DOM text includes the user's
// own content (window titles, URLs, queries); explicit track() events stay
// structural-only (counts/kinds/lengths). Session recording is off.
import posthog from 'posthog-js'

const POSTHOG_HOST = 'https://us.i.posthog.com'

export interface AnalyticsConfig {
  /** Empty or undefined keeps analytics a no-op. */
  key: string | undefined
  appVersion: string
  platform: string
  isProd: boolean
}

let enabled = false

export function init(config: AnalyticsConfig): void {
  if (enabled || !config.key) return

  posthog.init(config.key, {
    api_host: POSTHOG_HOST,
    capture_pageview: false,
    autocapture: true,
    capture_exceptions: false,
    disable_session_recording: true,
    advanced_disable_decide: true, // no remote scripts → CSP needs only connect-src
    person_profiles: 'identified_only'
  })
  posthog.register({
    client: 'melone-desktop', // web registers 'web'
    app_version: config.appVersion,
    platform: config.platform,
    environment: config.isProd ? 'production' : 'development'
  })
  enabled = true
}

export function track(event: string, props?: Record<string, unknown>): void {
  if (!enabled) return
  // source mirrors the web client's per-event source:'web' for consistent splitting
  posthog.capture(event, { ...props, source: 'desktop' })
}

// identify/reset are deferred until web + desktop share a login.
export function identify(id: string, props?: Record<string, unknown>): void {
  if (!enabled) return
  posthog.identify(id, props)
}

export function reset(): void {
  if (!enabled) return
  posthog.reset()
}

type RecordingAction = 'started' | 'stopped' | 'paused' | 'resumed'

export function trackRecording(action: RecordingAction): void {
  track(`recording_${action}`)
}

export function trackSearch(info: { resultsCount: number; queryLength: number }): void {
  track('memory_searched', { results_count: info.resultsCount, query_length: info.queryLength })
}

export function trackMemoryOpened(info: { resultKind: string }): void {
  track('memory_opened', { result_kind: info.resultKind })
}

export function trackMcpToggle(info: { target: string; enabled: boolean }): void {
  track(info.enabled ? 'mcp_enabled' : 'mcp_disabled', { target: info.target })
}
