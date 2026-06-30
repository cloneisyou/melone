import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_SERVICE_PREFS,
  loadServicePrefs,
  sanitizeServicePrefs,
  saveServicePrefs
} from '../src/main/service-prefs'

describe('sanitizeServicePrefs', () => {
  it('defaults a non-object to the default prefs', () => {
    expect(sanitizeServicePrefs(null)).toEqual(DEFAULT_SERVICE_PREFS)
    expect(sanitizeServicePrefs(42)).toEqual(DEFAULT_SERVICE_PREFS)
  })

  it('defaults a missing or non-boolean daemonEnabled', () => {
    expect(sanitizeServicePrefs({})).toEqual(DEFAULT_SERVICE_PREFS)
    expect(sanitizeServicePrefs({ daemonEnabled: 'no' })).toEqual(DEFAULT_SERVICE_PREFS)
  })

  it('preserves a boolean daemonEnabled', () => {
    expect(sanitizeServicePrefs({ daemonEnabled: false })).toEqual({
      daemonEnabled: false,
      onboardingComplete: false
    })
    expect(sanitizeServicePrefs({ daemonEnabled: true })).toEqual({
      daemonEnabled: true,
      onboardingComplete: false
    })
  })

  it('defaults a missing or non-boolean onboardingComplete to false', () => {
    expect(sanitizeServicePrefs({ daemonEnabled: true }).onboardingComplete).toBe(false)
    expect(sanitizeServicePrefs({ onboardingComplete: 'yes' }).onboardingComplete).toBe(false)
  })

  it('preserves a boolean onboardingComplete independently of daemonEnabled', () => {
    expect(sanitizeServicePrefs({ daemonEnabled: false, onboardingComplete: true })).toEqual({
      daemonEnabled: false,
      onboardingComplete: true
    })
  })
})

describe('loadServicePrefs / saveServicePrefs', () => {
  let dir: string

  beforeEach(() => {
    dir = mkdtempSync(path.join(tmpdir(), 'melone-prefs-'))
  })

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true })
  })

  it('returns defaults when the file is missing', () => {
    expect(loadServicePrefs(path.join(dir, 'nope.json'))).toEqual(DEFAULT_SERVICE_PREFS)
  })

  it('round-trips a saved value', () => {
    const file = path.join(dir, 'service-prefs.json')
    saveServicePrefs(file, { daemonEnabled: false, onboardingComplete: true })
    expect(loadServicePrefs(file)).toEqual({ daemonEnabled: false, onboardingComplete: true })
  })

  it('returns defaults for a corrupt file', () => {
    const file = path.join(dir, 'service-prefs.json')
    writeFileSync(file, '{ not json', 'utf8')
    expect(loadServicePrefs(file)).toEqual(DEFAULT_SERVICE_PREFS)
  })
})
