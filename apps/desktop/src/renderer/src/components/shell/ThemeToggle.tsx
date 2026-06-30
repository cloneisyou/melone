// Top-right theme toggle: a single icon button that flips light/dark. Replaces
// the old account menu (Settings moved to the nav bar, so the menu lost its
// purpose). Sits in the same corner/header slot the avatar used to occupy.
import type { ReactElement } from 'react'
import { useI18n } from '../../lib/i18n'
import type { Theme } from '../../lib/theme'

// Shown in light mode (click → dark).
function MoonGlyph(): ReactElement {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5 5 0 1 0 6.5 6.5Z"
        fill="currentColor"
      />
    </svg>
  )
}

// Shown in dark mode (click → light).
function SunGlyph(): ReactElement {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="8" cy="8" r="3" fill="currentColor" />
      <g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
        <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M13 3l-1.4 1.4M4.4 11.6 3 13" />
      </g>
    </svg>
  )
}

interface ThemeToggleProps {
  theme: Theme
  onThemeChange: (theme: Theme) => void
}

export function ThemeToggle({ theme, onThemeChange }: ThemeToggleProps): ReactElement {
  const { t } = useI18n()
  const nextTheme: Theme = theme === 'light' ? 'dark' : 'light'
  const themeLabel = nextTheme === 'dark' ? t('menu.themeDark') : t('menu.themeLight')

  return (
    <div className="menu">
      <button
        type="button"
        className="menu-avatar"
        aria-label={themeLabel}
        title={themeLabel}
        onClick={() => {
          onThemeChange(nextTheme)
        }}
      >
        {theme === 'light' ? <MoonGlyph /> : <SunGlyph />}
      </button>
    </div>
  )
}
