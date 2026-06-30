import { describe, expect, it } from 'vitest'
import { nextZoomLevel, zoomDirectionForInput } from '../src/main/zoom'

function keyDown(key: string, modifiers: Partial<{ control: boolean; meta: boolean; alt: boolean }> = {}) {
  return { type: 'keyDown', key, control: false, meta: false, alt: false, ...modifiers }
}

describe('zoomDirectionForInput', () => {
  it('treats both Ctrl+= and Ctrl+Shift+= (+) as zoom-in', () => {
    expect(zoomDirectionForInput(keyDown('=', { control: true }))).toBe('in')
    expect(zoomDirectionForInput(keyDown('+', { control: true }))).toBe('in')
  })

  it('treats Ctrl+- as zoom-out and Ctrl+0 as reset', () => {
    expect(zoomDirectionForInput(keyDown('-', { control: true }))).toBe('out')
    expect(zoomDirectionForInput(keyDown('_', { control: true }))).toBe('out')
    expect(zoomDirectionForInput(keyDown('0', { control: true }))).toBe('reset')
  })

  it('accepts macOS Cmd combinations too', () => {
    expect(zoomDirectionForInput(keyDown('=', { meta: true }))).toBe('in')
  })

  it('ignores unmodified input, Alt combinations, and keyUp', () => {
    expect(zoomDirectionForInput(keyDown('='))).toBeNull()
    expect(zoomDirectionForInput(keyDown('=', { control: true, alt: true }))).toBeNull()
    expect(
      zoomDirectionForInput({ type: 'keyUp', key: '=', control: true, meta: false, alt: false })
    ).toBeNull()
  })
})

describe('nextZoomLevel', () => {
  it('moves in 0.5 steps and reset returns 0', () => {
    expect(nextZoomLevel(0, 'in')).toBe(0.5)
    expect(nextZoomLevel(-1, 'out')).toBe(-1.5)
    expect(nextZoomLevel(2.5, 'reset')).toBe(0)
  })

  it('stops at the bounds (-3..3) — shrinking must always be reversible', () => {
    expect(nextZoomLevel(3, 'in')).toBe(3)
    expect(nextZoomLevel(-3, 'out')).toBe(-3)
    expect(nextZoomLevel(-3, 'in')).toBe(-2.5)
  })
})
