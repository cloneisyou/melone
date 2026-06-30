/*
 * Window bounds persistence — first run centers at 3/4 of the work area;
 * later runs restore the last bounds (+maximized). When a monitor change
 * leaves the saved position off-screen, fall back to defaults.
 * Decision logic stays pure for vitest (open-target.ts pattern).
 */
import { readFileSync, writeFileSync } from 'node:fs'

export interface Rect {
  x: number
  y: number
  width: number
  height: number
}

export interface WindowState {
  bounds: Rect
  maximized: boolean
}

/** Must equal the BrowserWindow minWidth/minHeight options (manually synced with index.ts). */
export const MIN_WIDTH = 600
export const MIN_HEIGHT = 420

/** First-run size ratio — 3/4 of the work area. */
const INITIAL_RATIO = 0.75
/** Minimum visible area to count a saved window as grabbable — enough title bar to drag. */
const MIN_VISIBLE_WIDTH = 100
const MIN_VISIBLE_HEIGHT = 40

/** Default bounds: 3/4 of the work area, centered. */
export function defaultBounds(workArea: Rect): Rect {
  const width = Math.max(Math.round(workArea.width * INITIAL_RATIO), MIN_WIDTH)
  const height = Math.max(Math.round(workArea.height * INITIAL_RATIO), MIN_HEIGHT)
  return {
    width,
    height,
    x: workArea.x + Math.round((workArea.width - width) / 2),
    y: workArea.y + Math.round((workArea.height - height) / 2)
  }
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function intersects(rect: Rect, area: Rect): boolean {
  const overlapWidth = Math.min(rect.x + rect.width, area.x + area.width) - Math.max(rect.x, area.x)
  const overlapHeight =
    Math.min(rect.y + rect.height, area.y + area.height) - Math.max(rect.y, area.y)
  return overlapWidth >= MIN_VISIBLE_WIDTH && overlapHeight >= MIN_VISIBLE_HEIGHT
}

/**
 * Validate a value read from the state file. Returns null for broken shapes,
 * sizes below the minimum, or windows not sufficiently visible on any
 * display — callers fall back to the defaults.
 */
export function sanitizeWindowState(raw: unknown, displayAreas: readonly Rect[]): WindowState | null {
  if (typeof raw !== 'object' || raw === null) return null
  const candidate = raw as { bounds?: unknown; maximized?: unknown }
  if (typeof candidate.bounds !== 'object' || candidate.bounds === null) return null
  const bounds = candidate.bounds as Partial<Rect>
  if (
    !isFiniteNumber(bounds.x) ||
    !isFiniteNumber(bounds.y) ||
    !isFiniteNumber(bounds.width) ||
    !isFiniteNumber(bounds.height)
  ) {
    return null
  }
  if (bounds.width < MIN_WIDTH || bounds.height < MIN_HEIGHT) return null
  const rect: Rect = {
    x: Math.round(bounds.x),
    y: Math.round(bounds.y),
    width: Math.round(bounds.width),
    height: Math.round(bounds.height)
  }
  if (!displayAreas.some((area) => intersects(rect, area))) return null
  return { bounds: rect, maximized: candidate.maximized === true }
}

/** Read the saved state; null when missing or corrupt (treated as a first run). */
export function loadWindowState(filePath: string, displayAreas: readonly Rect[]): WindowState | null {
  try {
    const raw: unknown = JSON.parse(readFileSync(filePath, 'utf8'))
    return sanitizeWindowState(raw, displayAreas)
  } catch {
    return null
  }
}

/** Synchronous write is fine for one small JSON and stays safe right before quit. */
export function saveWindowState(filePath: string, state: WindowState): void {
  try {
    writeFileSync(filePath, JSON.stringify(state, null, 2), 'utf8')
  } catch {
    // A failed save only means the next launch opens with default bounds.
  }
}
