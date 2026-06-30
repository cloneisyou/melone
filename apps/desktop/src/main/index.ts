import path from 'node:path'
import { app, BrowserWindow, Menu, nativeImage, screen, shell, Tray } from 'electron'
import type { NativeImage } from 'electron'
import { autoUpdater } from 'electron-updater'
import logoPngPath from '../../assets/melone_logo.png?asset'
import { appBundlePath } from './app-bundle'
import {
  registerBridgeIpc,
  registerOnboardingIpc,
  registerOpenIpc,
  registerOpenSystemSettingsIpc,
  registerPermissionDragIpc,
  registerServicePowerIpc,
  registerUpdateIpc,
  registerWindowIpc,
  wireWindowStateEvents
} from './ipc'
import { openTarget } from './open-target'
import { PythonBridge, resolveDaemonSpawn } from './python'
import { loadServicePrefs, saveServicePrefs } from './service-prefs'
import { openSystemSettings } from './system-settings'
import { UpdateManager, type AppUpdaterLike } from './updater'
import {
  defaultBounds,
  loadWindowState,
  MIN_HEIGHT,
  MIN_WIDTH,
  saveWindowState
} from './window-state'
import { wireZoomShortcuts } from './zoom'

// Pin the app name so the data dir is the same everywhere. getPath('userData')
// derives from app.getName(), which otherwise falls back to package.json "name"
// (melone-desktop) in dev and unsigned local builds — a *different* folder from
// the daemon's own default (config APP_NAME = "Melone", used by the MCP server
// and any non-Electron daemon). That split means the desktop app and the agent's
// MCP read two separate databases. Forcing "Melone" here (matching the daemon
// default) makes dev, packaged, and MCP all resolve to ~/Library/Application
// Support/Melone. Must run before the first getPath('userData') below.
app.setName('Melone')

let mainWindow: BrowserWindow | null = null
let bridge: PythonBridge | null = null
// Held in a module-level ref so the OS tray icon is not garbage-collected.
let tray: Tray | null = null

// macOS close hides the window; a real quit sets this so the hide handler does not run.
let quitting = false
// Guards the async before-quit cleanup so it runs exactly once (we re-quit after).
let shuttingDown = false

function appLogo(): NativeImage {
  // createFromPath only reliably loads raster formats (PNG/JPEG), not .icns.
  return nativeImage.createFromPath(logoPngPath)
}

function applyAppIcons(): void {
  const logo = appLogo()
  if (process.platform === 'darwin') {
    app.dock?.setIcon(logo)
  }
}

// System tray: reuse the same melone logo (downscaled) instead of a separate
// asset, so every surface — dock, taskbar, installer, tray — shows one logo.
function createTray(): void {
  if (tray !== null) return
  // The light squircle icon reads on both light and dark trays, so no template image.
  const icon = appLogo().resize({ width: 16, height: 16, quality: 'best' })
  tray = new Tray(icon)
  tray.setToolTip('Melone')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Melone 열기', click: () => showMainWindow() },
      { type: 'separator' },
      {
        label: '종료',
        click: () => {
          quitting = true
          app.quit()
        }
      }
    ])
  )
  // Left-click opens the window on Windows/Linux; on macOS it opens the menu.
  if (process.platform !== 'darwin') {
    tray.on('click', () => showMainWindow())
  }
}

function createBridge(): PythonBridge {
  // In dev, prefer the repo's apps/service venv; ../../../service is relative to the bundle (out/main).
  const serviceDir = app.isPackaged ? null : path.resolve(__dirname, '../../../service')
  // The packaged app bundle is read-only, so the daemon must persist to a
  // user-writable dir. MELONE_HOME is what the service resolves its data dir
  // from (config.resolve_app_data_dir); set it so the packaged daemon and the
  // collector it spawns agree on one location instead of the bundle path.
  process.env['MELONE_HOME'] = app.getPath('userData')
  // Packaged: the bundled standalone daemon under resources/. Dev: <python> -m melone_service.rpc.
  const { command, args } = resolveDaemonSpawn({
    env: process.env,
    platform: process.platform,
    isPackaged: app.isPackaged,
    serviceDir,
    resourcesPath: app.isPackaged ? process.resourcesPath : null
  })
  // With cwd at apps/service, even a PATH python can find -m melone_service.rpc.
  return new PythonBridge({ command, args, cwd: serviceDir ?? undefined })
}

function windowStatePath(): string {
  return path.join(app.getPath('userData'), 'window-state.json')
}

function servicePrefsPath(): string {
  return path.join(app.getPath('userData'), 'service-prefs.json')
}

function createMainWindow(): void {
  // Replace the system title bar with the renderer's custom one: hiddenInset
  // keeps the macOS traffic lights; other platforms drop the frame entirely.
  const chrome =
    process.platform === 'darwin'
      ? ({ titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 14, y: 13 } } as const)
      : ({ frame: false } as const)

  // First run: centered at 3/4 of the work area; afterwards restore the last-used bounds.
  const statePath = windowStatePath()
  const displayAreas = screen.getAllDisplays().map((display) => display.workArea)
  const saved = loadWindowState(statePath, displayAreas)
  const bounds = saved?.bounds ?? defaultBounds(screen.getPrimaryDisplay().workArea)

  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    icon: appLogo(),
    show: true,
    // Matches the default (light) theme --bg to avoid a flash before first paint.
    // Dark-theme users may see one bright frame — main cannot read localStorage.
    backgroundColor: '#F7F7F4',
    ...chrome,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // electron-vite bundles the preload with CJS require, so only sandbox is disabled.
      sandbox: false
    }
  })
  if (saved?.maximized === true) {
    mainWindow.maximize()
  }

  wireWindowStateEvents(mainWindow)
  wireZoomShortcuts(mainWindow.webContents)

  // Remember the last normal (unmaximized) bounds — the restore target even
  // when the window is closed while maximized.
  let normalBounds = bounds
  let saveTimer: NodeJS.Timeout | null = null
  const captureAndSave = (): void => {
    const window = mainWindow
    if (window === null || window.isDestroyed()) return
    if (!window.isMaximized()) {
      normalBounds = window.getBounds()
    }
    saveWindowState(statePath, { bounds: normalBounds, maximized: window.isMaximized() })
  }
  const scheduleSave = (): void => {
    if (saveTimer !== null) clearTimeout(saveTimer)
    // Resize/move events stream in bursts; debounce the disk writes.
    saveTimer = setTimeout(captureAndSave, 500)
  }
  mainWindow.on('resize', scheduleSave)
  mainWindow.on('move', scheduleSave)
  mainWindow.on('maximize', scheduleSave)
  mainWindow.on('unmaximize', scheduleSave)

  // Melone is a background collector, so closing hides to the tray on every
  // platform; the daemon keeps running and the tray reopens the window. Only a
  // real quit (tray "종료" / before-quit, which sets `quitting`) closes for good.
  mainWindow.on('close', (event) => {
    if (saveTimer !== null) clearTimeout(saveTimer)
    captureAndSave()
    if (!quitting) {
      event.preventDefault()
      mainWindow?.hide()
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  const rendererUrl = process.env['ELECTRON_RENDERER_URL']
  if (rendererUrl) {
    void mainWindow.loadURL(rendererUrl)
  } else {
    void mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

function showMainWindow(): void {
  if (mainWindow === null) {
    createMainWindow()
    return
  }
  mainWindow.show()
  mainWindow.focus()
}

// Single-instance: a second launch (or a different build run while this one is
// open) focuses the existing window instead of spawning another app — and
// another RPC daemon. Must run before whenReady; the loser quits.
const hasSingleInstanceLock = app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) {
  app.quit()
}

app.on('second-instance', () => {
  if (mainWindow !== null) {
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.show()
    mainWindow.focus()
  } else {
    showMainWindow()
  }
})

void app.whenReady().then(() => {
  // The second instance lost the lock and is quitting — do not spawn a daemon.
  if (!hasSingleInstanceLock) return
  // Windows: bind the taskbar button / notifications to our appId (matches
  // electron-builder appId) instead of the generic Electron identity. No-op elsewhere.
  app.setAppUserModelId('com.cloneisyou.melone')
  applyAppIcons()
  createTray()
  const prefsPath = servicePrefsPath()
  bridge = createBridge()
  registerBridgeIpc(bridge)
  // Daemon power toggle: persist the choice (merging so other prefs survive),
  // then apply it to the live bridge.
  registerServicePowerIpc((enabled) => {
    saveServicePrefs(prefsPath, { ...loadServicePrefs(prefsPath), daemonEnabled: enabled })
    if (enabled) bridge?.enable()
    else bridge?.disable()
  })
  // First-run onboarding flag: read/write the same prefs file.
  registerOnboardingIpc(
    () => loadServicePrefs(prefsPath).onboardingComplete,
    (complete) => {
      saveServicePrefs(prefsPath, { ...loadServicePrefs(prefsPath), onboardingComplete: complete })
    }
  )
  registerWindowIpc()
  // open-target.ts does not import electron, so shell.openExternal is injected here.
  registerOpenIpc((target) =>
    openTarget(target, {
      platform: process.platform,
      openExternal: (url) => shell.openExternal(url)
    })
  )
  // System Settings deep links (onboarding permission step) — same shell injection.
  registerOpenSystemSettingsIpc((pane) =>
    openSystemSettings(pane, {
      platform: process.platform,
      openExternal: (url) => shell.openExternal(url)
    })
  )
  // Drag the app bundle into a System Settings privacy list (onboarding). The
  // pure path math is testable; the bundle + a small drag image are bound here.
  registerPermissionDragIpc(() => {
    const file = appBundlePath(process.execPath, process.platform)
    if (file === null) return null
    return { file, icon: appLogo().resize({ width: 64, height: 64 }) }
  })

  // Auto-update. Release artifacts live on public GitHub Releases in
  // cloneisyou/melone. electron-builder writes the GitHub provider metadata into
  // app-update.yml during packaging, so the main process only attaches the real
  // updater when packaged on a supported platform. MELONE_FAKE_UPDATE=1 still
  // drives the renderer flow in dev.
  const AUTO_UPDATE_PLATFORMS: ReadonlySet<NodeJS.Platform> = new Set(['darwin', 'win32'])
  const fakeUpdate = process.env['MELONE_FAKE_UPDATE'] === '1'
  const canUseAutoUpdater = app.isPackaged && AUTO_UPDATE_PLATFORMS.has(process.platform)
  const updateManager = new UpdateManager({
    isPackaged: app.isPackaged,
    fakeUpdate,
    updater: canUseAutoUpdater ? (autoUpdater as unknown as AppUpdaterLike) : undefined
  })
  registerUpdateIpc(updateManager)
  // When enabled, check at launch and every 3h so a long-running session still
  // picks up a release without restart. Background failures stay silent.
  if (fakeUpdate || canUseAutoUpdater) {
    updateManager.check({ userInitiated: false })
    const RECHECK_INTERVAL_MS = 3 * 60 * 60 * 1000
    const recheck = setInterval(() => {
      // Don't interrupt an in-flight download or an update waiting to install.
      const phase = updateManager.getState().phase
      if (phase === 'downloading' || phase === 'downloaded') return
      updateManager.check({ userInitiated: false })
    }, RECHECK_INTERVAL_MS)
    recheck.unref?.()
  }

  // Honor the persisted power state: launch the daemon only when enabled,
  // otherwise sit in the 'disabled' state until the user turns it on.
  if (loadServicePrefs(prefsPath).daemonEnabled) bridge.start()
  else bridge.disable()
  createMainWindow()

  app.on('activate', () => {
    // macOS: clicking the dock icon restores the hidden window.
    showMainWindow()
  })
})

app.on('before-quit', (event) => {
  quitting = true
  // Delay the quit until the daemon AND the collector it spawned are stopped, so
  // the collector can't outlive us holding the SQLite lock (breaking the next
  // launch/update). Re-quit when done; this handler re-runs and falls through.
  if (shuttingDown || bridge === null) return
  shuttingDown = true
  event.preventDefault()
  void bridge.shutdown().finally(() => app.quit())
})

// The window hides (not closes) on the close button, so this normally never
// fires. Keeping a no-op listener means even if the window is destroyed the app
// stays resident in the tray — it quits only via tray "종료" / before-quit.
app.on('window-all-closed', () => {})
