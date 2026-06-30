import type { ScreenTextStatus } from './daemon'

/** Extracts the app name from a "Cursor | queries.py" label for macOS app activation. */
export function appNameFromLabel(label: string): string | null {
  const first = label.split(' | ')[0]?.trim()
  return first === undefined || first === '' ? null : first
}

export function searchEmptyNoticeKey({
  collectorsSupported,
  hasAnyData,
  screenTextStatus
}: {
  collectorsSupported: boolean | null
  hasAnyData: boolean
  screenTextStatus:
    | Pick<ScreenTextStatus, 'state' | 'enabled' | 'effectiveEnabled' | 'latestIndexedAt'>
    | null
}): string {
  if (screenTextStatus?.state === 'off') return 'search.screenTextOff'
  if (
    screenTextStatus !== null &&
    (screenTextStatus.enabled || screenTextStatus.effectiveEnabled) &&
    screenTextStatus.latestIndexedAt === null
  ) {
    return 'search.screenTextNoIndexedData'
  }

  // An empty DB on a non-collecting device is not a "no match" — say so instead.
  if (collectorsSupported === false && !hasAnyData) return 'search.noData'
  return 'search.noResults'
}
