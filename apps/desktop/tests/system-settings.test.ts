import { describe, expect, it, vi } from 'vitest'
import {
  openSystemSettings,
  parseSettingsPane,
  type SettingsPane
} from '../src/main/system-settings'

describe('parseSettingsPane', () => {
  it('accepts the known panes', () => {
    expect(parseSettingsPane('screen-recording')).toBe('screen-recording')
    expect(parseSettingsPane('accessibility')).toBe('accessibility')
  })

  it('rejects unknown or non-string values', () => {
    expect(parseSettingsPane('microphone')).toBeNull()
    expect(parseSettingsPane('')).toBeNull()
    expect(parseSettingsPane(null)).toBeNull()
    expect(parseSettingsPane(42)).toBeNull()
    expect(parseSettingsPane({ pane: 'accessibility' })).toBeNull()
  })
})

describe('openSystemSettings', () => {
  it('opens the screen-recording deep link on macOS', async () => {
    const openExternal = vi.fn(async () => {})
    const result = await openSystemSettings('screen-recording', {
      platform: 'darwin',
      openExternal
    })
    expect(result).toEqual({ ok: true, reason: 'opened' })
    expect(openExternal).toHaveBeenCalledWith(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'
    )
  })

  it('opens the accessibility deep link on macOS', async () => {
    const openExternal = vi.fn(async () => {})
    await openSystemSettings('accessibility', { platform: 'darwin', openExternal })
    expect(openExternal).toHaveBeenCalledWith(
      'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
    )
  })

  it('reports unsupported on non-macOS without calling openExternal', async () => {
    const openExternal = vi.fn(async () => {})
    for (const platform of ['win32', 'linux'] as NodeJS.Platform[]) {
      const result = await openSystemSettings('screen-recording', { platform, openExternal })
      expect(result).toEqual({ ok: false, reason: 'unsupported' })
    }
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('reports invalid when the OS refuses the deep link', async () => {
    const openExternal = vi.fn(async () => {
      throw new Error('no handler')
    })
    const pane: SettingsPane = 'accessibility'
    const result = await openSystemSettings(pane, { platform: 'darwin', openExternal })
    expect(result).toEqual({ ok: false, reason: 'invalid' })
  })
})
