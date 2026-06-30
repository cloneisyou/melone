/*
 * Resolve the macOS .app bundle root from the running executable so onboarding
 * can offer the app itself as a Finder-style drag source — the user drops it
 * into a System Settings privacy list, the same gesture as dragging an app onto
 * Applications in a DMG. Pure + electron-free so the path math stays unit-
 * testable; the actual webContents.startDrag lives in ipc.ts.
 */

/**
 * The `.app` bundle root for `execPath`, or null when there is nothing to drag.
 * macOS only — a privacy-list drag has no meaning elsewhere. A bundled execPath
 * looks like /Applications/Melone.app/Contents/MacOS/Melone, so we slice back to
 * the outermost `.app` segment. (In dev, Electron runs from .../Electron.app,
 * which is itself draggable and matches the dev privacy entry, so we keep it.)
 * An unbundled path has no `.app/`, so there is nothing draggable → null.
 */
export function appBundlePath(execPath: string, platform: NodeJS.Platform): string | null {
  if (platform !== 'darwin') return null
  const marker = '.app/'
  const index = execPath.indexOf(marker)
  if (index === -1) return null
  return execPath.slice(0, index + marker.length - 1)
}
