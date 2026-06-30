/*
 * renderer <-> main IPC backing window.melone in the preload.
 *  - melone:request                 invoke: generic daemon JSON-RPC call, resolved
 *                                   as a BridgeRequestEnvelope (bridge-request.ts)
 *  - melone:bridge-state            main -> renderer push: bridge state changes
 *  - melone:bridge-state:subscribe  renderer -> main: request the current state snapshot
 *  - melone:open                    invoke: open a clicked search result (open-target.ts)
 * Channel literals live in channels.ts, shared with the preload.
 */
import { BrowserWindow, ipcMain } from 'electron'
import type { NativeImage } from 'electron'
import { handleBridgeRequest, type BridgeRequestEnvelope } from './bridge-request'
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
} from './channels'
import { parseOpenTarget, type OpenResult, type OpenTarget } from './open-target'
import {
  parseSettingsPane,
  type OpenSettingsResult,
  type SettingsPane
} from './system-settings'
import type { PythonBridge } from './python'
import type { UpdateManager } from './updater'

export type WindowControlAction = 'minimize' | 'maximize-toggle' | 'close'

/**
 * Wire a "subscribe → current snapshot, then stream every change to all
 * windows" channel pair. Sending the snapshot on subscribe guarantees the
 * renderer never misses the initial state. Shared by bridge / update state
 * (window state is per-window, so it stays separate).
 */
function registerStateBroadcast<T>(
  subscribeChannel: string,
  stateChannel: string,
  getState: () => T,
  onChange: (listener: (state: T) => void) => void
): void {
  ipcMain.on(subscribeChannel, (event) => {
    event.sender.send(stateChannel, getState())
  })
  onChange((state) => {
    for (const window of BrowserWindow.getAllWindows()) {
      window.webContents.send(stateChannel, state)
    }
  })
}

export function registerBridgeIpc(bridge: PythonBridge): void {
  ipcMain.handle(
    REQUEST_CHANNEL,
    (_event, method: unknown, params: unknown): Promise<BridgeRequestEnvelope> =>
      handleBridgeRequest(bridge, method, params)
  )

  registerStateBroadcast(
    BRIDGE_STATE_SUBSCRIBE_CHANNEL,
    BRIDGE_STATE_CHANNEL,
    () => bridge.getState(),
    (listener) => bridge.onStateChange(listener)
  )
}

/**
 * Daemon power toggle: renderer asks main to turn the RPC daemon on/off. The
 * applier (index.ts) persists the choice and calls bridge.enable()/disable();
 * the new state reaches the renderer through the existing bridge-state push.
 */
export function registerServicePowerIpc(setPower: (enabled: boolean) => void): void {
  ipcMain.handle(SERVICE_POWER_CHANNEL, (_event, enabled: unknown) => {
    setPower(enabled === true)
  })
}

/**
 * Open a clicked search result. The executor (open-target.ts openTarget with
 * shell bound) is injected by index.ts — this module only validates and wires
 * the channel.
 */
export function registerOpenIpc(open: (target: OpenTarget) => Promise<OpenResult>): void {
  ipcMain.handle(OPEN_CHANNEL, async (_event, rawTarget: unknown): Promise<OpenResult> => {
    // Re-validate in main — calls could bypass the preload.
    const target = parseOpenTarget(rawTarget)
    if (target === null) return { ok: false, reason: 'invalid' }
    return open(target)
  })
}

/**
 * Deep-link into a macOS System Settings privacy pane (onboarding's
 * "Open Settings →"). The executor (system-settings.ts with shell bound) is
 * injected by index.ts; this only validates the pane and wires the channel.
 */
export function registerOpenSystemSettingsIpc(
  open: (pane: SettingsPane) => Promise<OpenSettingsResult>
): void {
  ipcMain.handle(
    OPEN_SYSTEM_SETTINGS_CHANNEL,
    async (_event, rawPane: unknown): Promise<OpenSettingsResult> => {
      const pane = parseSettingsPane(rawPane)
      if (pane === null) return { ok: false, reason: 'invalid' }
      return open(pane)
    }
  )
}

/**
 * Begin a native drag of the app bundle so the user can drop it into a System
 * Settings privacy list (onboarding's "drag Melone into the list", mirroring a
 * DMG's drag-to-Applications). startDrag must run on the sender's webContents,
 * so this can't be an injected pure helper; index.ts supplies the bundle file +
 * drag icon via getDrag, which returns null when there is nothing to drag (non-
 * macOS or an unbundled run) and the drag is then a no-op.
 */
export function registerPermissionDragIpc(
  getDrag: () => { file: string; icon: NativeImage } | null
): void {
  ipcMain.on(START_PERMISSION_DRAG_CHANNEL, (event) => {
    const drag = getDrag()
    if (drag === null) return
    event.sender.startDrag(drag)
  })
}

/**
 * First-run onboarding flag. get/set both read/write service-prefs.json via the
 * appliers injected by index.ts (kept there so all prefs I/O lives in one place).
 */
export function registerOnboardingIpc(get: () => boolean, set: (complete: boolean) => void): void {
  ipcMain.handle(ONBOARDING_GET_CHANNEL, () => get())
  ipcMain.handle(ONBOARDING_SET_CHANNEL, (_event, complete: unknown) => {
    set(complete === true)
  })
}

// Window controls for the frameless custom title bar. Acts only on the requesting window.
export function registerWindowIpc(): void {
  ipcMain.on(WINDOW_CONTROL_CHANNEL, (event, action: WindowControlAction) => {
    const window = BrowserWindow.fromWebContents(event.sender)
    if (window === null) return
    switch (action) {
      case 'minimize':
        window.minimize()
        break
      case 'maximize-toggle':
        if (window.isMaximized()) {
          window.unmaximize()
        } else {
          window.maximize()
        }
        break
      case 'close':
        // Route through close() so index.ts keeps hide-on-close handling in one place.
        window.close()
        break
    }
  })

  ipcMain.on(WINDOW_STATE_SUBSCRIBE_CHANNEL, (event) => {
    const window = BrowserWindow.fromWebContents(event.sender)
    if (window === null) return
    event.sender.send(WINDOW_STATE_CHANNEL, { maximized: window.isMaximized() })
  })
}

// Push window state changes so the renderer can swap the maximize/restore icon.
export function wireWindowStateEvents(window: BrowserWindow): void {
  const send = (): void => {
    if (window.isDestroyed()) return
    window.webContents.send(WINDOW_STATE_CHANNEL, { maximized: window.isMaximized() })
  }
  window.on('maximize', send)
  window.on('unmaximize', send)
}

/**
 * Auto-update: push UpdateState (subscribe-then-stream, like bridge state) plus
 * user-triggered check / download / install-and-restart actions. The manager
 * itself decides what is visible — the renderer just renders the latest state.
 */
export function registerUpdateIpc(manager: UpdateManager): void {
  registerStateBroadcast(
    UPDATE_STATE_SUBSCRIBE_CHANNEL,
    UPDATE_STATE_CHANNEL,
    () => manager.getState(),
    (listener) => manager.onStateChange(listener)
  )

  // A check from the UI is always user-initiated, so its errors are allowed to surface.
  ipcMain.handle(UPDATE_CHECK_CHANNEL, () => {
    manager.check({ userInitiated: true })
  })
  ipcMain.handle(UPDATE_DOWNLOAD_CHANNEL, () => {
    manager.download()
  })
  ipcMain.on(UPDATE_INSTALL_CHANNEL, () => {
    manager.quitAndInstall()
  })
}
