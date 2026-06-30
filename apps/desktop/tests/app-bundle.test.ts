import { describe, expect, it } from 'vitest'
import { appBundlePath } from '../src/main/app-bundle'

describe('appBundlePath', () => {
  it('slices a macOS execPath back to the .app bundle root', () => {
    expect(appBundlePath('/Applications/Melone.app/Contents/MacOS/Melone', 'darwin')).toBe(
      '/Applications/Melone.app'
    )
  })

  it('returns the bundle for a dev Electron run (still draggable)', () => {
    expect(
      appBundlePath(
        '/repo/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron',
        'darwin'
      )
    ).toBe('/repo/node_modules/electron/dist/Electron.app')
  })

  it('takes the outermost .app when the path nests more than one', () => {
    expect(appBundlePath('/x/Outer.app/Contents/Inner.app/MacOS/bin', 'darwin')).toBe(
      '/x/Outer.app'
    )
  })

  it('returns null when the path has no bundle', () => {
    expect(appBundlePath('/usr/local/bin/melone', 'darwin')).toBeNull()
  })

  it('returns null off macOS', () => {
    expect(appBundlePath('C:/Program Files/Melone/Melone.exe', 'win32')).toBeNull()
    expect(appBundlePath('/opt/Melone.app/Contents/MacOS/Melone', 'linux')).toBeNull()
  })
})
