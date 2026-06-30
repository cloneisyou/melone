// JSON is read with readFileSync instead of import — tests/ compile under
// tsconfig.node, which does not enable resolveJsonModule.
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { enumLabel, isLocale, translate } from '../src/renderer/src/lib/i18n-core'
import type { MessageTable, Messages } from '../src/renderer/src/lib/i18n-core'

const localesDir = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../src/renderer/src/locales'
)

function loadLocale(file: string): Messages {
  return JSON.parse(readFileSync(resolve(localesDir, file), 'utf8')) as Messages
}

const en = loadLocale('en.json')
const kr = loadLocale('kr.json')
const table: MessageTable = { en, kr }

// Fixture isolated from the real copy so wording changes don't break rule tests.
const fixture: MessageTable = {
  en: {
    'statusline.updated': 'updated {time}',
    'only.en': 'english only',
    repeat: '{name} and {name} ({count})'
  },
  kr: {
    'statusline.updated': '{time} 갱신'
  }
}

describe('translate', () => {
  it('returns the locale dictionary string', () => {
    expect(translate(table, 'en', 'service.running')).toBe('Collecting')
    expect(translate(table, 'kr', 'service.running')).toBe('수집 중')
  })

  it('replaces {placeholder} with vars values', () => {
    expect(translate(fixture, 'en', 'statusline.updated', { time: '09:05:03' })).toBe(
      'updated 09:05:03'
    )
    expect(translate(fixture, 'kr', 'statusline.updated', { time: '09:05:03' })).toBe(
      '09:05:03 갱신'
    )
  })

  it('replaces repeated placeholders and numeric vars', () => {
    expect(translate(fixture, 'en', 'repeat', { name: 'melone', count: 3 })).toBe(
      'melone and melone (3)'
    )
  })

  it('leaves placeholders missing from vars verbatim', () => {
    expect(translate(fixture, 'en', 'repeat', { name: 'melone' })).toBe(
      'melone and melone ({count})'
    )
  })

  it('falls back to en when the locale lacks the key', () => {
    expect(translate(fixture, 'kr', 'only.en')).toBe('english only')
  })

  it('returns the key itself when en lacks it too', () => {
    expect(translate(fixture, 'kr', 'missing.key')).toBe('missing.key')
    expect(translate(fixture, 'en', 'missing.key', { time: 'x' })).toBe('missing.key')
  })
})

describe('enumLabel — daemon enum label mapping', () => {
  const t = (key: string): string => translate(table, 'kr', key)

  it('maps known enum values to locale labels', () => {
    expect(enumLabel(t, 'permission', 'granted')).toBe('허용')
    expect(enumLabel(t, 'permission', 'denied')).toBe('거부')
  })

  it('returns unknown enum values verbatim', () => {
    expect(enumLabel(t, 'permission', 'mystery_status')).toBe('mystery_status')
  })
})

describe('isLocale', () => {
  it('accepts only en/kr as locales', () => {
    expect(isLocale('en')).toBe(true)
    expect(isLocale('kr')).toBe(true)
    expect(isLocale('ko')).toBe(false)
    expect(isLocale(null)).toBe(false)
  })
})

describe('locale dictionaries (en.json / kr.json)', () => {
  it('has identical key sets in both dictionaries', () => {
    expect(Object.keys(kr).sort()).toEqual(Object.keys(en).sort())
  })

  it('has a non-empty string for every value', () => {
    for (const [key, value] of [...Object.entries(en), ...Object.entries(kr)]) {
      expect(typeof value, key).toBe('string')
      expect(value.trim().length, key).toBeGreaterThan(0)
    }
  })

  it('contains no Korean characters in en copy', () => {
    for (const [key, value] of Object.entries(en)) {
      expect(value, key).not.toMatch(/[ㄱ-ㅎㅏ-ㅣ가-힣]/)
    }
  })

  it('has matching placeholder sets for each key across both dictionaries', () => {
    const placeholders = (template: string): string[] =>
      [...template.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort()
    for (const key of Object.keys(en)) {
      expect(placeholders(kr[key]), key).toEqual(placeholders(en[key]))
    }
  })
})
