/*
 * App-wide state in one context so pages read what they need via useAppState()
 * instead of being handed a long prop list. Owns the 5s dashboard poll (visible
 * + connected only), the bridge subscription, theme, and the active page.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { POLL, PREVIEWS, RANK } from '../config/app'
import { RPC } from '../config/rpc'
import type { PageId } from '../config/pages'
import { humanErrorMessage, requestDaemon } from '../lib/daemon'
import type {
  McpStatus,
  RankedContext,
  ScenePreview,
  ScreenTextStatus,
  ServiceStatus,
  StorageStats
} from '../lib/daemon'
import { createPoller } from '../lib/poller'
import { applyTheme, initialTheme, THEME_STORAGE_KEY } from '../lib/theme'
import type { Theme } from '../lib/theme'

/** Per-section poll error; null when the last fetch for that section succeeded. */
export interface DashboardErrors {
  service: string | null
  screenText: string | null
  mcp: string | null
  rank: string | null
  previews: string | null
  storage: string | null
}

export interface AppState {
  bridge: MeloneBridgeState
  connected: boolean
  rank: RankedContext[] | null
  previews: ScenePreview[] | null
  storage: StorageStats | null
  serviceStatus: ServiceStatus | null
  screenTextStatus: ScreenTextStatus | null
  mcpStatus: McpStatus | null
  loaded: boolean
  errors: DashboardErrors
  collectorsSupported: boolean | null
  hasAnyData: boolean
  refresh: () => void
  theme: Theme
  setTheme: (theme: Theme) => void
  tab: PageId
  setTab: (tab: PageId) => void
  /** Whether the first-run onboarding overlay is showing. */
  onboardingOpen: boolean
  /** Open the onboarding overlay (e.g. from the home "Set it up" link). */
  openOnboarding: () => void
  /** Mark onboarding done (persisted) and close the overlay. */
  completeOnboarding: () => void
}

const INITIAL_BRIDGE: MeloneBridgeState = { status: 'connecting', pid: null, detail: null }

const AppStateContext = createContext<AppState | null>(null)

export function useAppState(): AppState {
  const value = useContext(AppStateContext)
  if (value === null) throw new Error('useAppState must be used within <AppStateProvider>')
  return value
}

export function AppStateProvider({ children }: { children: ReactNode }): ReactElement {
  const [tab, setTab] = useState<PageId>('home')
  const [theme, setThemeState] = useState<Theme>(() =>
    initialTheme(localStorage.getItem(THEME_STORAGE_KEY))
  )
  const [bridge, setBridge] = useState<MeloneBridgeState>(INITIAL_BRIDGE)
  const [serviceStatus, setServiceStatus] = useState<ServiceStatus | null>(null)
  const [screenTextStatus, setScreenTextStatus] = useState<ScreenTextStatus | null>(null)
  const [mcpStatus, setMcpStatus] = useState<McpStatus | null>(null)
  const [rank, setRank] = useState<RankedContext[] | null>(null)
  const [previews, setPreviews] = useState<ScenePreview[] | null>(null)
  const [storage, setStorage] = useState<StorageStats | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [errors, setErrors] = useState<DashboardErrors>({
    service: null,
    screenText: null,
    mcp: null,
    rank: null,
    previews: null,
    storage: null
  })

  // Onboarding overlay. It auto-opens while EITHER is true:
  //   - a required macOS permission (accessibility / screen recording) is
  //     missing — every launch, so the user is driven back to grant it; or
  //   - the user has not yet been through the flow (onboardingComplete=false).
  // The second clause matters because granting screen recording needs an app
  // restart: on that relaunch the permissions are satisfied, but the user has
  // not yet seen the Connect-agent / Try-it steps — without it the wizard would
  // never reappear and those steps would be silently skipped. Once both
  // permissions are granted AND the flow is completed, it stops auto-opening.
  // onboardingDismissed stops it reopening on the next 5s poll after Skip/Done;
  // manual "Set it up" (openOnboarding) clears it so it always opens on demand.
  // onboardingComplete is null until the persisted flag is read.
  const [onboardingOpen, setOnboardingOpen] = useState(false)
  const [onboardingComplete, setOnboardingComplete] = useState<boolean | null>(null)
  const onboardingDismissed = useRef(false)
  // Auto-start recording at most once per session (see the effect below).
  const autoStartAttempted = useRef(false)

  // Skip a tick while the previous poll is still in flight.
  const inFlight = useRef(false)
  const statusInFlight = useRef(false)

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    applyTheme(next)
    localStorage.setItem(THEME_STORAGE_KEY, next)
  }, [])

  const openOnboarding = useCallback(() => {
    onboardingDismissed.current = false
    setOnboardingOpen(true)
  }, [])

  const completeOnboarding = useCallback(() => {
    onboardingDismissed.current = true
    setOnboardingOpen(false)
    setOnboardingComplete(true)
    void window.melone.setOnboardingComplete(true)
  }, [])

  const refresh = useCallback(async (): Promise<void> => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [svc, screenText, mcp, ranked, previewsResult, storageResult] =
        await Promise.allSettled([
          requestDaemon(RPC.service.status),
          requestDaemon(RPC.screenText.status),
          requestDaemon(RPC.mcp.status),
          requestDaemon(RPC.context.rank, { sinceMinutes: RANK.sinceMinutes, limit: RANK.fetchLimit }),
          requestDaemon(RPC.screen.previews, { limit: PREVIEWS.limit }),
          requestDaemon(RPC.storage.stats)
        ])

      if (svc.status === 'fulfilled') setServiceStatus(svc.value)
      if (screenText.status === 'fulfilled') setScreenTextStatus(screenText.value)
      if (mcp.status === 'fulfilled') setMcpStatus(mcp.value)
      if (ranked.status === 'fulfilled') setRank(ranked.value)
      if (previewsResult.status === 'fulfilled') setPreviews(previewsResult.value.previews)
      if (storageResult.status === 'fulfilled') setStorage(storageResult.value)

      setErrors({
        service: svc.status === 'fulfilled' ? null : humanErrorMessage(svc.reason),
        screenText: screenText.status === 'fulfilled' ? null : humanErrorMessage(screenText.reason),
        mcp: mcp.status === 'fulfilled' ? null : humanErrorMessage(mcp.reason),
        rank: ranked.status === 'fulfilled' ? null : humanErrorMessage(ranked.reason),
        previews:
          previewsResult.status === 'fulfilled' ? null : humanErrorMessage(previewsResult.reason),
        storage:
          storageResult.status === 'fulfilled' ? null : humanErrorMessage(storageResult.reason)
      })

      setLoaded(true)
    } finally {
      inFlight.current = false
    }
  }, [])

  const refreshStatus = useCallback(async (): Promise<void> => {
    if (statusInFlight.current) return
    statusInFlight.current = true
    try {
      const [svc, screenText] = await Promise.allSettled([
        requestDaemon(RPC.service.status),
        requestDaemon(RPC.screenText.status)
      ])

      if (svc.status === 'fulfilled') setServiceStatus(svc.value)
      if (screenText.status === 'fulfilled') setScreenTextStatus(screenText.value)

      setErrors((current) => ({
        ...current,
        service: svc.status === 'fulfilled' ? null : humanErrorMessage(svc.reason),
        screenText: screenText.status === 'fulfilled' ? null : humanErrorMessage(screenText.reason)
      }))

      setLoaded(true)
    } finally {
      statusInFlight.current = false
    }
  }, [])

  useEffect(() => {
    const poller = createPoller({
      intervalMs: POLL.intervalMs,
      tick: () => {
        void refresh()
      }
    })
    const syncVisibility = (): void => {
      poller.setVisible(document.visibilityState === 'visible')
    }
    document.addEventListener('visibilitychange', syncVisibility)
    syncVisibility()
    const offBridge = window.melone.onBridgeState((state) => {
      setBridge(state)
      poller.setConnected(state.status === 'connected')
    })
    return () => {
      offBridge()
      document.removeEventListener('visibilitychange', syncVisibility)
      poller.dispose()
    }
  }, [refresh])

  // Read the persisted "completed onboarding once" flag at startup.
  useEffect(() => {
    let active = true
    void window.melone.getOnboardingComplete().then((complete) => {
      if (active) setOnboardingComplete(complete)
    })
    return () => {
      active = false
    }
  }, [])

  // Auto-open onboarding while a required permission is missing OR the flow has
  // never been completed (see the state comment). Runs on every status update
  // (5s poll), so it also catches a permission revoked mid-session. Never
  // force-closes — when nothing is outstanding it just stops auto-opening,
  // leaving a manually opened wizard alone.
  useEffect(() => {
    if (onboardingComplete === null) return // wait until the flag is known
    const permsMissing =
      serviceStatus !== null && serviceStatus.permissions.missingRequiredPermissions.length > 0
    if ((permsMissing || !onboardingComplete) && !onboardingDismissed.current) {
      setOnboardingOpen(true)
    }
  }, [serviceStatus, onboardingComplete])

  // macOS privacy grants do not emit an app event back to Electron. While the
  // user is in the permission path, poll only the cheap status endpoints so the
  // UI catches Accessibility / Screen Recording changes without waiting for the
  // normal dashboard poll.
  //
  // shouldPoll is a stable boolean so the effect re-runs only when the polling
  // condition flips — NOT on every serviceStatus update. Depending on
  // serviceStatus directly would loop: the effect calls refreshStatus(), which
  // setServiceStatus(new object) → effect re-runs → refreshStatus() again,
  // unthrottled. (refreshStatus is memoized, so it is a stable dep.)
  const permsMissing =
    serviceStatus !== null && serviceStatus.permissions.missingRequiredPermissions.length > 0
  const waitingForInitialPermissionStatus = onboardingOpen && serviceStatus === null
  const shouldPollPermissions =
    bridge.status === 'connected' && (permsMissing || waitingForInitialPermissionStatus)

  useEffect(() => {
    if (!shouldPollPermissions) return
    const id = window.setInterval(() => {
      void refreshStatus()
    }, POLL.permissionIntervalMs)
    void refreshStatus()
    return () => {
      window.clearInterval(id)
    }
  }, [shouldPollPermissions, refreshStatus])

  // Auto-start recording once permissions are in place. The collector never
  // starts itself, so without this the user would have to find the Settings
  // toggle after granting. Fires at most once per session (autoStartAttempted)
  // so a later manual stop is not immediately undone. Only when connected,
  // collectors are supported, both permissions are granted, and nothing is
  // already running or paused.
  useEffect(() => {
    if (autoStartAttempted.current || serviceStatus === null) return
    if (bridge.status !== 'connected' || !serviceStatus.collectorsSupported) return
    if (serviceStatus.permissions.missingRequiredPermissions.length > 0) return
    if (serviceStatus.running || serviceStatus.paused) return
    autoStartAttempted.current = true
    void requestDaemon(RPC.service.start)
      .then(() => {
        refresh()
      })
      .catch(() => {
        // A failed start surfaces via the Recording section; don't retry-spam.
      })
  }, [serviceStatus, bridge.status, refresh])

  const value: AppState = {
    bridge,
    connected: bridge.status === 'connected',
    rank,
    previews,
    storage,
    serviceStatus,
    screenTextStatus,
    mcpStatus,
    loaded,
    errors,
    collectorsSupported: serviceStatus?.collectorsSupported ?? null,
    hasAnyData: rank !== null && rank.length > 0,
    refresh: () => {
      void refresh()
    },
    theme,
    setTheme,
    tab,
    setTab,
    onboardingOpen,
    openOnboarding,
    completeOnboarding
  }

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>
}
