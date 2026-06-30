import { describe, expect, it, vi } from 'vitest'

import type { AnalyticsConfig } from '../src/renderer/src/lib/analytics'

// Stable mock across vi.resetModules(): each fresh import of the analytics module
// (which resets its internal `enabled` flag) talks to the same spies.
const { posthog } = vi.hoisted(() => ({
  posthog: {
    init: vi.fn(),
    register: vi.fn(),
    capture: vi.fn(),
    identify: vi.fn(),
    reset: vi.fn()
  }
}))
vi.mock('posthog-js', () => ({ default: posthog }))

const CONFIG: AnalyticsConfig = {
  key: 'phc_test',
  appVersion: '1.2.3',
  platform: 'darwin',
  isProd: true
}

async function load(): Promise<typeof import('../src/renderer/src/lib/analytics')> {
  vi.resetModules()
  vi.clearAllMocks()
  return import('../src/renderer/src/lib/analytics')
}

describe('analytics', () => {
  it('is a no-op when no key is configured', async () => {
    const analytics = await load()
    analytics.init({ ...CONFIG, key: undefined })
    analytics.track('app_launched')
    analytics.trackSearch({ resultsCount: 1, queryLength: 5 })

    expect(posthog.init).not.toHaveBeenCalled()
    expect(posthog.capture).not.toHaveBeenCalled()
  })

  it('initializes once and tags client=melone-desktop', async () => {
    const analytics = await load()
    analytics.init(CONFIG)
    analytics.init(CONFIG) // idempotent

    expect(posthog.init).toHaveBeenCalledTimes(1)
    expect(posthog.init).toHaveBeenCalledWith(
      'phc_test',
      expect.objectContaining({
        api_host: 'https://us.i.posthog.com',
        // Autocapture on and unmasked (product decision) — DOM text included.
        autocapture: true,
        capture_pageview: false,
        person_profiles: 'identified_only'
      })
    )
    expect(posthog.register).toHaveBeenCalledWith(
      expect.objectContaining({ client: 'melone-desktop', environment: 'production' })
    )
  })

  it('captures domain events with structural metadata only (never query text)', async () => {
    const analytics = await load()
    analytics.init(CONFIG)

    analytics.trackSearch({ resultsCount: 3, queryLength: 12 })
    analytics.trackRecording('started')
    analytics.trackMcpToggle({ target: 'claude-code', enabled: true })
    analytics.trackMemoryOpened({ resultKind: 'url' })

    expect(posthog.capture).toHaveBeenCalledWith('memory_searched', {
      results_count: 3,
      query_length: 12,
      source: 'desktop'
    })
    expect(posthog.capture).toHaveBeenCalledWith('recording_started', { source: 'desktop' })
    expect(posthog.capture).toHaveBeenCalledWith('mcp_enabled', {
      target: 'claude-code',
      source: 'desktop'
    })
    expect(posthog.capture).toHaveBeenCalledWith('memory_opened', {
      result_kind: 'url',
      source: 'desktop'
    })
  })
})
