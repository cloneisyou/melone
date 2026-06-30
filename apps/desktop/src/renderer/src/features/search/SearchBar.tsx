// Controlled search input — a self-contained, reusable box. State lives in the
// caller (see useSearch); this component just renders the input and a
// not-connected hint. Drop it anywhere with a value/onChange pair.
import type { ReactElement } from 'react'
import { useI18n } from '../../lib/i18n'

interface SearchBarProps {
  value: string
  onChange: (value: string) => void
  /** When false, typing shows a "connecting" hint instead of silently no-op'ing. */
  connected: boolean
}

export function SearchBar({ value, onChange, connected }: SearchBarProps): ReactElement {
  const { t } = useI18n()

  return (
    <div className="search">
      <div className="search-field">
        <svg
          className="search-icon"
          viewBox="0 0 24 24"
          width="18"
          height="18"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <line x1="16.5" y1="16.5" x2="21" y2="21" />
        </svg>
        <input
          className="search-input"
          type="search"
          value={value}
          placeholder={t('search.placeholder')}
          aria-label={t('search.aria')}
          spellCheck={false}
          onChange={(event) => {
            onChange(event.target.value)
          }}
        />
      </div>

      {!connected && value.trim() !== '' && (
        <p className="caption search-waiting">{t('service.bridge.connecting')}</p>
      )}
    </div>
  )
}
