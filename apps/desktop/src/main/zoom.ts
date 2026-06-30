/*
 * Zoom shortcuts — Electron's default menu binds Zoom In to Ctrl+Shift+=, so
 * text shrunk with Ctrl+- cannot be restored with plain Ctrl+=. Handle zoom
 * in before-input-event and preventDefault to suppress double firing with the
 * menu accelerator (preventDefault also blocks menu shortcuts — Electron docs).
 */
import type { Input, WebContents } from 'electron'

const ZOOM_MIN = -3
const ZOOM_MAX = 3
const ZOOM_STEP = 0.5

export type ZoomDirection = 'in' | 'out' | 'reset'

// Only Ctrl (or macOS Cmd) combinations count as zoom input.
export function zoomDirectionForInput(input: {
  type: string
  key: string
  control: boolean
  meta: boolean
  alt: boolean
}): ZoomDirection | null {
  if (input.type !== 'keyDown') return null
  if (!(input.control || input.meta) || input.alt) return null

  // Accept both '=' (Ctrl+=) and '+' (Ctrl+Shift+=, numpad +) as zoom-in.
  if (input.key === '=' || input.key === '+') return 'in'
  if (input.key === '-' || input.key === '_') return 'out'
  if (input.key === '0') return 'reset'
  return null
}

export function nextZoomLevel(current: number, direction: ZoomDirection): number {
  if (direction === 'reset') return 0
  const next = direction === 'in' ? current + ZOOM_STEP : current - ZOOM_STEP
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next))
}

export function wireZoomShortcuts(webContents: WebContents): void {
  webContents.on('before-input-event', (event, input: Input) => {
    const direction = zoomDirectionForInput(input)
    if (direction === null) return
    event.preventDefault()
    webContents.setZoomLevel(nextZoomLevel(webContents.getZoomLevel(), direction))
  })
}
