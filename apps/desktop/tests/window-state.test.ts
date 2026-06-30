import { describe, expect, it } from 'vitest'
import {
  defaultBounds,
  MIN_HEIGHT,
  MIN_WIDTH,
  sanitizeWindowState
} from '../src/main/window-state'

const WORK_AREA = { x: 0, y: 0, width: 1920, height: 1040 }
const DISPLAYS = [WORK_AREA]

describe('defaultBounds', () => {
  it('centers the window at 3/4 of the work area', () => {
    const bounds = defaultBounds(WORK_AREA)
    expect(bounds.width).toBe(1440)
    expect(bounds.height).toBe(780)
    expect(bounds.x).toBe(240)
    expect(bounds.y).toBe(130)
  })

  it('never goes below the minimum size on small screens', () => {
    const bounds = defaultBounds({ x: 0, y: 0, width: 700, height: 500 })
    expect(bounds.width).toBeGreaterThanOrEqual(MIN_WIDTH)
    expect(bounds.height).toBeGreaterThanOrEqual(MIN_HEIGHT)
  })

  it('respects the work area offset (secondary monitor)', () => {
    const bounds = defaultBounds({ x: 1920, y: 100, width: 1000, height: 800 })
    expect(bounds.x).toBeGreaterThanOrEqual(1920)
  })
})

describe('sanitizeWindowState', () => {
  const valid = { bounds: { x: 100, y: 80, width: 900, height: 700 }, maximized: false }

  it('passes a valid state through unchanged', () => {
    expect(sanitizeWindowState(valid, DISPLAYS)).toEqual(valid)
  })

  it('preserves the maximized flag', () => {
    expect(sanitizeWindowState({ ...valid, maximized: true }, DISPLAYS)?.maximized).toBe(true)
  })

  it('returns null for broken shapes — callers fall back to defaults', () => {
    expect(sanitizeWindowState(null, DISPLAYS)).toBeNull()
    expect(sanitizeWindowState('{}', DISPLAYS)).toBeNull()
    expect(sanitizeWindowState({ bounds: { x: 'a', y: 0, width: 800, height: 600 } }, DISPLAYS)).toBeNull()
    expect(sanitizeWindowState({ bounds: { x: 0, y: 0, width: NaN, height: 600 } }, DISPLAYS)).toBeNull()
  })

  it('rejects sizes below the minimum', () => {
    expect(
      sanitizeWindowState({ bounds: { x: 0, y: 0, width: 300, height: 700 } }, DISPLAYS)
    ).toBeNull()
  })

  it('rejects windows left off-screen by a removed monitor', () => {
    const offscreen = { bounds: { x: -5000, y: -5000, width: 900, height: 700 } }
    expect(sanitizeWindowState(offscreen, DISPLAYS)).toBeNull()
  })

  it('accepts a window sufficiently visible on any one of multiple displays', () => {
    const second = { x: 1920, y: 0, width: 1920, height: 1040 }
    const onSecond = { bounds: { x: 2200, y: 100, width: 900, height: 700 } }
    expect(sanitizeWindowState(onSecond, [WORK_AREA, second])).not.toBeNull()
    expect(sanitizeWindowState(onSecond, [WORK_AREA])).toBeNull()
  })
})
