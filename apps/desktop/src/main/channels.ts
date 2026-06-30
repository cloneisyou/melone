// IPC channel literals shared by main (ipc.ts) and the preload.
// Imports no Electron modules, so the preload bundle stays main-free.
export const REQUEST_CHANNEL = 'melone:request'
export const BRIDGE_STATE_CHANNEL = 'melone:bridge-state'
export const BRIDGE_STATE_SUBSCRIBE_CHANNEL = 'melone:bridge-state:subscribe'
// Renderer -> main: turn the RPC daemon on/off (persisted across restarts).
export const SERVICE_POWER_CHANNEL = 'melone:service-power'
export const WINDOW_CONTROL_CHANNEL = 'melone:window-control'
export const WINDOW_STATE_CHANNEL = 'melone:window-state'
export const WINDOW_STATE_SUBSCRIBE_CHANNEL = 'melone:window-state:subscribe'
export const OPEN_CHANNEL = 'melone:open'
// Renderer -> main: deep-link into a macOS System Settings privacy pane.
export const OPEN_SYSTEM_SETTINGS_CHANNEL = 'melone:open-system-settings'
// Renderer -> main: begin a native drag of the app bundle so the user can drop
// it into a System Settings privacy list (onboarding). macOS only.
export const START_PERMISSION_DRAG_CHANNEL = 'melone:start-permission-drag'
// First-run onboarding completion flag (persisted in service-prefs.json).
export const ONBOARDING_GET_CHANNEL = 'melone:onboarding-get'
export const ONBOARDING_SET_CHANNEL = 'melone:onboarding-set'
// Auto-update: state push + subscribe mirror the bridge-state pattern; the rest
// are user-triggered actions (check / download / install-and-restart).
export const UPDATE_STATE_CHANNEL = 'melone:update-state'
export const UPDATE_STATE_SUBSCRIBE_CHANNEL = 'melone:update-state:subscribe'
export const UPDATE_CHECK_CHANNEL = 'melone:update-check'
export const UPDATE_DOWNLOAD_CHANNEL = 'melone:update-download'
export const UPDATE_INSTALL_CHANNEL = 'melone:update-install'
