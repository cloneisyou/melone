import { contextBridge, ipcRenderer } from 'electron'
import type { IpcRendererEvent } from 'electron'
import type { BridgeRequestEnvelope } from '../main/bridge-request'
import {
  BRIDGE_STATE_CHANNEL,
  BRIDGE_STATE_SUBSCRIBE_CHANNEL,
  ONBOARDING_GET_CHANNEL,
  ONBOARDING_SET_CHANNEL,
  OPEN_CHANNEL,
  OPEN_SYSTEM_SETTINGS_CHANNEL,
  REQUEST_CHANNEL,
  SERVICE_POWER_CHANNEL,
  START_PERMISSION_DRAG_CHANNEL,
  UPDATE_CHECK_CHANNEL,
  UPDATE_DOWNLOAD_CHANNEL,
  UPDATE_INSTALL_CHANNEL,
  UPDATE_STATE_CHANNEL,
  UPDATE_STATE_SUBSCRIBE_CHANNEL,
  WINDOW_CONTROL_CHANNEL,
  WINDOW_STATE_CHANNEL,
  WINDOW_STATE_SUBSCRIBE_CHANNEL
} from '../main/channels'
import type { OpenResult, OpenTarget } from '../main/open-target'
import type { BridgeState } from '../main/python'
import type { OpenSettingsResult, SettingsPane } from '../main/system-settings'
import type { UpdateState } from '../main/updater'

/**
 * Subscribe to a main → renderer state channel. The main process sends the
 * current snapshot once on `subscribeChannel`, then streams every change on
 * `stateChannel`. Returns an unsubscribe function.
 */
function subscribe<T>(
  stateChannel: string,
  subscribeChannel: string,
  callback: (state: T) => void
): () => void {
  const listener = (_event: IpcRendererEvent, state: T): void => {
    callback(state)
  }
  ipcRenderer.on(stateChannel, listener)
  ipcRenderer.send(subscribeChannel)
  return () => {
    ipcRenderer.removeListener(stateChannel, listener)
  }
}

// The renderer's only path to main-process functionality.
const meloneApi = {
  /**
   * Daemon JSON-RPC call. Always resolves with a BridgeRequestEnvelope —
   * contextBridge strips custom Error properties, so errors travel as data
   * ({code, message: symbol, data: human text}) and the renderer unwraps them
   * (lib/daemon.ts unwrapEnvelope).
   */
  request: async (
    method: string,
    params?: Record<string, unknown>
  ): Promise<BridgeRequestEnvelope> => {
    return (await ipcRenderer.invoke(REQUEST_CHANNEL, method, params)) as BridgeRequestEnvelope
  },
  /** Subscribe to bridge state. The current state arrives once immediately; the returned function unsubscribes. */
  onBridgeState: (callback: (state: BridgeState) => void): (() => void) =>
    subscribe(BRIDGE_STATE_CHANNEL, BRIDGE_STATE_SUBSCRIBE_CHANNEL, callback),
  /**
   * Turn the RPC daemon on/off. Persisted across restarts; the resulting state
   * (connecting/connected or disabled) streams back via onBridgeState.
   */
  setServicePower: async (enabled: boolean): Promise<void> => {
    await ipcRenderer.invoke(SERVICE_POWER_CHANNEL, enabled)
  },
  /**
   * Open a clicked search result. URLs open in the default browser; app
   * activation is macOS-only. Failures report via OpenResult instead of rejecting.
   */
  open: async (target: OpenTarget): Promise<OpenResult> => {
    return (await ipcRenderer.invoke(OPEN_CHANNEL, target)) as OpenResult
  },
  /** Deep-link into a macOS System Settings privacy pane (onboarding). macOS only. */
  openSystemSettings: async (pane: SettingsPane): Promise<OpenSettingsResult> => {
    return (await ipcRenderer.invoke(OPEN_SYSTEM_SETTINGS_CHANNEL, pane)) as OpenSettingsResult
  },
  /**
   * Begin a native drag of the Melone app bundle so the user can drop it into a
   * System Settings privacy list (onboarding). Call from a DOM dragstart handler
   * after preventDefault(); macOS-only and a no-op when there is no bundle.
   */
  startPermissionDrag: (): void => {
    ipcRenderer.send(START_PERMISSION_DRAG_CHANNEL)
  },
  /** Read the persisted first-run onboarding flag. */
  getOnboardingComplete: async (): Promise<boolean> => {
    return (await ipcRenderer.invoke(ONBOARDING_GET_CHANNEL)) as boolean
  },
  /** Persist that onboarding has been completed (or skipped). */
  setOnboardingComplete: async (complete: boolean): Promise<void> => {
    await ipcRenderer.invoke(ONBOARDING_SET_CHANNEL, complete)
  },
  /** Window controls for the custom title bar. macOS uses traffic lights, so the renderer skips these. */
  windowControls: {
    minimize: (): void => {
      ipcRenderer.send(WINDOW_CONTROL_CHANNEL, 'minimize')
    },
    toggleMaximize: (): void => {
      ipcRenderer.send(WINDOW_CONTROL_CHANNEL, 'maximize-toggle')
    },
    close: (): void => {
      ipcRenderer.send(WINDOW_CONTROL_CHANNEL, 'close')
    }
  },
  /** Subscribe to maximize state (for the maximize/restore icon). Current state arrives once on subscribe. */
  onWindowState: (callback: (state: { maximized: boolean }) => void): (() => void) =>
    subscribe(WINDOW_STATE_CHANNEL, WINDOW_STATE_SUBSCRIBE_CHANNEL, callback),
  /**
   * Subscribe to auto-update state. Current state arrives once on subscribe; the
   * returned function unsubscribes. The renderer shows the update banner only
   * when the phase is not idle/checking/none.
   */
  onUpdateState: (callback: (state: UpdateState) => void): (() => void) =>
    subscribe(UPDATE_STATE_CHANNEL, UPDATE_STATE_SUBSCRIBE_CHANNEL, callback),
  /** Manually check for updates (user action — failures may surface in the banner). */
  checkForUpdates: async (): Promise<void> => {
    await ipcRenderer.invoke(UPDATE_CHECK_CHANNEL)
  },
  /** Download the available update. Progress streams back via onUpdateState. */
  downloadUpdate: async (): Promise<void> => {
    await ipcRenderer.invoke(UPDATE_DOWNLOAD_CHANNEL)
  },
  /** Quit and install the downloaded update (restarts the app). */
  installUpdate: (): void => {
    ipcRenderer.send(UPDATE_INSTALL_CHANNEL)
  },
  /** For title bar layout branching (macOS traffic-light inset vs custom buttons). */
  platform: process.platform
}

export type MeloneApi = typeof meloneApi

contextBridge.exposeInMainWorld('melone', meloneApi)
