// Search-results grid: debounced context.search → cards of screenshot thumbnail
// + app/url label + "Last Visited" stamp. Clicking a card opens the source via
// window.melone.open (URLs in the browser, apps activated on macOS).
import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import type { ScreenTextStatus, SearchResult } from '../../lib/daemon'
import * as analytics from '../../lib/analytics'
import { humanErrorMessage, requestDaemon } from '../../lib/daemon'
import { formatVisited } from '../../lib/format'
import { useI18n } from '../../lib/i18n'
import { appNameFromLabel, searchEmptyNoticeKey } from '../../lib/search-ux'
import { GLYPH, SEARCH } from '../../config/app'
import { RPC } from '../../config/rpc'
import { GlobeGlyph, WindowGlyph } from '../../components/ui/glyphs'

interface SearchResultsProps {
  query: string
  sinceMinutes: number
  limit: number
  onSinceMinutesChange: (value: number) => void
  onLimitChange: (value: number) => void
  connected: boolean
  collectorsSupported: boolean | null
  hasAnyData: boolean
  screenTextStatus: ScreenTextStatus | null
}

export function SearchResults({
  query,
  sinceMinutes,
  limit,
  onSinceMinutesChange,
  onLimitChange,
  connected,
  collectorsSupported,
  hasAnyData,
  screenTextStatus
}: SearchResultsProps): ReactElement {
  const { t } = useI18n()
  const [results, setResults] = useState<SearchResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)
  const generationRef = useRef(0)

  const trimmedQuery = query.trim()

  useEffect(() => {
    if (trimmedQuery === '' || !connected) {
      return undefined
    }
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const response = await requestDaemon(RPC.context.search, {
            query: trimmedQuery,
            sinceMinutes,
            limit
          })
          generationRef.current += 1
          setResults(response.results)
          setError(null)
          analytics.trackSearch({
            resultsCount: response.results.length,
            queryLength: trimmedQuery.length
          })
        } catch (requestError) {
          setError(humanErrorMessage(requestError))
        }
      })()
    }, SEARCH.debounceMs)
    return () => {
      window.clearTimeout(timer)
    }
  }, [trimmedQuery, connected, sinceMinutes, limit])

  const handleOpen = async (result: SearchResult): Promise<void> => {
    setOpenError(null)
    const outcome = await window.melone.open({
      kind: result.kind as MeloneOpenTarget['kind'],
      url: result.uri,
      appName: result.uri === null ? appNameFromLabel(result.label) : null
    })
    analytics.trackMemoryOpened({ resultKind: result.kind })
    if (!outcome.ok) {
      setOpenError(
        outcome.reason === 'unsupported' ? t('search.openUnsupported') : t('search.openInvalid')
      )
    }
  }

  const filterControl = (
    <div className="search-filters" aria-label={t('search.filterAria')}>
      <label className="search-filter-field">
        <span className="search-filter-label">{t('search.scopeLabel')}</span>
        <select
          className="search-filter-select"
          value={String(sinceMinutes)}
          onChange={(event) => {
            onSinceMinutesChange(Number(event.target.value))
          }}
        >
          {SEARCH.timeScopes.map((option) => (
            <option key={String(option.minutes)} value={String(option.minutes)}>
              {t(option.labelKey)}
            </option>
          ))}
        </select>
      </label>
      <label className="search-filter-field">
        <span className="search-filter-label">{t('search.limitLabel')}</span>
        <select
          className="search-filter-select"
          value={String(limit)}
          onChange={(event) => {
            onLimitChange(Number(event.target.value))
          }}
        >
          {SEARCH.resultLimits.map((option) => (
            <option key={String(option)} value={String(option)}>
              {t('search.limitOption', { count: option })}
            </option>
          ))}
        </select>
      </label>
    </div>
  )

  if (!connected) {
    return <p className="caption search-results-note">{t('service.bridge.connecting')}</p>
  }
  if (error !== null) {
    return (
      <div className="search-results">
        {filterControl}
        <p className="caption caption--error search-results-note">{error}</p>
      </div>
    )
  }
  if (results === null) {
    return (
      <div className="search-results">
        {filterControl}
        <p className="caption search-results-note">{t('context.loading')}</p>
      </div>
    )
  }
  if (results.length === 0) {
    const noticeKey = searchEmptyNoticeKey({ collectorsSupported, hasAnyData, screenTextStatus })
    return (
      <div className="search-results">
        {filterControl}
        <p className="caption search-results-note">{t(noticeKey)}</p>
      </div>
    )
  }

  return (
    <div className="search-results">
      {filterControl}
      <div className="result-grid" role="list" aria-label={t('search.resultsAria')}>
        {results.map((result) => {
          const visited = formatVisited(result.lastSeenAt)
          return (
            <button
              key={`${String(generationRef.current)}:${result.key}`}
              type="button"
              role="listitem"
              className="result-card"
              title={
                result.snippet === undefined || result.snippet === null
                  ? result.label
                  : `${result.label}\n${result.snippet}`
              }
              onClick={() => {
                void handleOpen(result)
              }}
            >
              {result.image != null ? (
                <img
                  className="result-card-thumb"
                  src={result.image}
                  alt={result.label}
                  draggable={false}
                />
              ) : (
                <span className="result-card-thumb result-card-thumb--empty" aria-hidden="true" />
              )}
              <span className="result-card-meta">
                <span className="result-card-icon">
                  {result.kind === 'url' ? (
                    <GlobeGlyph size={GLYPH.resultSize} />
                  ) : (
                    <WindowGlyph size={GLYPH.resultSize} />
                  )}
                </span>
                <span className="result-card-label">{result.label}</span>
              </span>
              {visited !== '' && (
                <span className="result-card-visited">{t('search.lastVisited', { date: visited })}</span>
              )}
            </button>
          )
        })}
      </div>
      {openError !== null && <p className="caption caption--error search-results-note">{openError}</p>}
    </div>
  )
}
