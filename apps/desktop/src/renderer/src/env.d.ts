/// <reference types="vite/client" />

// Injected at build time by electron.vite.config.ts (renderer `define`).
declare const __APP_VERSION__: string
declare const __BUILD_DATE__: string

// Renderer env vars (augments vite/client's ImportMetaEnv). Loaded from
// apps/desktop/.env via the renderer `envDir` in electron.vite.config.ts.
interface ImportMetaEnv {
  /** PostHog project token; analytics is a no-op when unset. */
  readonly VITE_POSTHOG_KEY?: string
}

// Types for the API exposed by the preload (contextBridge); implementation in
// src/preload/index.ts. MeloneBridgeState must match BridgeState in
// src/main/python.ts (the web tsconfig excludes main/preload, so sync manually).
interface MeloneBridgeState {
  // 'disabled' = the user turned the daemon off (distinct from a crash, 'down').
  status: 'connecting' | 'connected' | 'down' | 'disabled'
  pid: number | null
  detail: string | null
}

// Must match BridgeErrorPayload/BridgeRequestEnvelope in src/main/bridge-request.ts (manual sync).
interface MeloneBridgeErrorPayload {
  code: number
  /** Machine-readable symbol (e.g. "INVALID_PARAMS") for renderer branching. */
  message: string
  /** Human-readable (Korean) text from the daemon. */
  data?: unknown
}

type MeloneBridgeRequestEnvelope =
  | { ok: true; result: unknown }
  | { ok: false; error: MeloneBridgeErrorPayload }

// Must match OpenTarget/OpenResult in src/main/open-target.ts (manual sync).
interface MeloneOpenTarget {
  kind: 'url' | 'app_window' | 'app'
  url: string | null
  appName: string | null
}

interface MeloneOpenResult {
  ok: boolean
  reason: 'opened' | 'unsupported' | 'invalid'
}

// Must match SettingsPane/OpenSettingsResult in src/main/system-settings.ts (manual sync).
type MeloneSettingsPane = 'screen-recording' | 'accessibility'

interface MeloneOpenSettingsResult {
  ok: boolean
  reason: 'opened' | 'unsupported' | 'invalid'
}

// Must match UpdateState in src/main/updater.ts (manual sync). idle/checking/none
// render no banner — the update button appears only from 'available' onward.
type MeloneUpdateState =
  | { phase: 'idle' }
  | { phase: 'checking' }
  | { phase: 'none' }
  | { phase: 'available'; version: string }
  | { phase: 'downloading'; percent: number }
  | { phase: 'downloaded'; version: string }
  | { phase: 'error'; message: string }

interface Window {
  melone: {
    /** Daemon JSON-RPC call. Always resolves with an envelope — unwrap via lib/daemon.ts. */
    request: (
      method: string,
      params?: Record<string, unknown>
    ) => Promise<MeloneBridgeRequestEnvelope>
    /** Subscribe to bridge state. Current state arrives once on subscribe; the returned function unsubscribes. */
    onBridgeState: (callback: (state: MeloneBridgeState) => void) => () => void
    /** Turn the RPC daemon on/off (persisted). The new state streams back via onBridgeState. */
    setServicePower: (enabled: boolean) => Promise<void>
    /**
     * Open a clicked search result. URLs open in the default browser; app
     * activation is macOS-only. Failures report via OpenResult instead of rejecting.
     */
    open: (target: MeloneOpenTarget) => Promise<MeloneOpenResult>
    /** Deep-link into a macOS System Settings privacy pane (onboarding). macOS only. */
    openSystemSettings: (pane: MeloneSettingsPane) => Promise<MeloneOpenSettingsResult>
    /**
     * Begin a native drag of the Melone app bundle (onboarding: drop it into a
     * System Settings privacy list). Call from a dragstart handler after
     * preventDefault(); macOS-only and a no-op when there is no bundle.
     */
    startPermissionDrag: () => void
    /** Read the persisted first-run onboarding flag. */
    getOnboardingComplete: () => Promise<boolean>
    /** Persist that onboarding has been completed (or skipped). */
    setOnboardingComplete: (complete: boolean) => Promise<void>
    /** Window controls for the custom title bar (unused on macOS, which has traffic lights). */
    windowControls: {
      minimize: () => void
      toggleMaximize: () => void
      close: () => void
    }
    /** Subscribe to maximize state. Current state arrives once on subscribe; the returned function unsubscribes. */
    onWindowState: (callback: (state: { maximized: boolean }) => void) => () => void
    /** Subscribe to auto-update state. Current state arrives once on subscribe; the returned function unsubscribes. */
    onUpdateState: (callback: (state: MeloneUpdateState) => void) => () => void
    /** Manually check for updates (user action — failures may surface in the banner). */
    checkForUpdates: () => Promise<void>
    /** Download the available update. Progress streams back via onUpdateState. */
    downloadUpdate: () => Promise<void>
    /** Quit and install the downloaded update (restarts the app). */
    installUpdate: () => void
    /** process.platform value ('darwin' | 'win32' | ...). */
    platform: string
  }
}
