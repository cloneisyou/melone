import { describe, expect, it } from 'vitest'
import { searchEmptyNoticeKey } from '../src/renderer/src/lib/search-ux'

describe('search empty state selection', () => {
  it('shows Screen Text Search off separately from no matches', () => {
    expect(
      searchEmptyNoticeKey({
        collectorsSupported: true,
        hasAnyData: true,
        screenTextStatus: {
          state: 'off',
          enabled: false,
          effectiveEnabled: false,
          latestIndexedAt: null
        }
      })
    ).toBe('search.screenTextOff')
  })

  it('shows missing indexed screen text data separately from no matches', () => {
    expect(
      searchEmptyNoticeKey({
        collectorsSupported: true,
        hasAnyData: true,
        screenTextStatus: {
          state: 'ready',
          enabled: true,
          effectiveEnabled: true,
          latestIndexedAt: null
        }
      })
    ).toBe('search.screenTextNoIndexedData')
  })

  it('keeps the non-collecting device empty state when screen text status is unknown', () => {
    expect(
      searchEmptyNoticeKey({
        collectorsSupported: false,
        hasAnyData: false,
        screenTextStatus: null
      })
    ).toBe('search.noData')
  })

  it('uses the ordinary no match state when screen text data exists', () => {
    expect(
      searchEmptyNoticeKey({
        collectorsSupported: true,
        hasAnyData: true,
        screenTextStatus: {
          state: 'ready',
          enabled: true,
          effectiveEnabled: true,
          latestIndexedAt: '2026-06-19T10:00:00.000Z'
        }
      })
    ).toBe('search.noResults')
  })
})
