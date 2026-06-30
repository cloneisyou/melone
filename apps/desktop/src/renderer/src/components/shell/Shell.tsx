// App frame + home chrome. Two layouts:
//  - idle home: centered wordmark → search → nav → scene carousel, account menu
//    floating in the top-right corner.
//  - a sub-page tab or an active search: a compact header row
//    [Melone | search | account] → nav → page body.
// The wordmark always returns to the idle home (and clears the query). Search is
// global — a non-empty query shows results over any page. All page-specific data
// comes from useAppState(), so the shell drills no props into pages.
import type { ReactElement } from 'react'
import { BRAND, LINKS } from '../../config/app'
import { NAV_PAGES, pageById } from '../../config/pages'
import { useAppState } from '../../context/app-state'
import { OnboardingWizard } from '../../features/onboarding/OnboardingWizard'
import { SearchBar, SearchResults, useSearch } from '../../features/search'
import { useI18n } from '../../lib/i18n'
import { ThemeToggle } from './ThemeToggle'
import { UpdateBanner } from './UpdateBanner'
import { PageOutlet } from './PageOutlet'

// macOS keeps OS traffic lights inset; other platforms get a plain drag strip.
const isMac = window.melone.platform === 'darwin'

export function Shell(): ReactElement {
  const { t } = useI18n()
  const {
    tab,
    setTab,
    connected,
    theme,
    setTheme,
    collectorsSupported,
    hasAnyData,
    screenTextStatus,
    openOnboarding
  } = useAppState()
  const search = useSearch()

  const onHome = tab === 'home'
  // The big centered hero is only the idle home; a sub-page or active search uses
  // the compact header row.
  const compact = search.active || !onHome
  const bodyVariant = search.active ? 'results' : pageById(tab).layout

  const goHome = (): void => {
    search.clear()
    setTab('home')
  }

  const openFeedback = (): void => {
    void window.melone.open({ kind: 'url', url: LINKS.feedback, appName: null })
  }

  const themeToggle = <ThemeToggle theme={theme} onThemeChange={setTheme} />

  return (
    <div className="shell">
      {/* No visible header: a thin draggable strip keeps the window movable and,
          on macOS, reserves the inset where the OS traffic lights sit. */}
      <div className={isMac ? 'window-drag window-drag--mac' : 'window-drag'} aria-hidden="true" />
      <UpdateBanner />
      {/* Revisit onboarding — Home tab only (not during search), pinned just
          above the feedback link. */}
      {onHome && !search.active && (
        <button
          type="button"
          className="shell-revisit"
          aria-label={t('onboarding.revisitAria')}
          onClick={openOnboarding}
        >
          {t('onboarding.revisit')}
        </button>
      )}
      {/* Persistent feedback link — pinned to the bottom-right corner on every tab
          and search state, clear of the top-right account menu and OS chrome. */}
      <button
        type="button"
        className="shell-feedback"
        aria-label={t('feedback.aria')}
        onClick={openFeedback}
      >
        {t('feedback.prompt')}
      </button>
      <main className="shell-content">
        <div className={compact ? 'home home--compact' : 'home'}>
          {/* One header element across both layouts so the search input is never
              remounted (which would drop focus mid-typing) — only classes change.
              The idle corner account is rendered last (it's absolute) so toggling
              it never shifts the header/search position. */}
          <div className={compact ? 'home-topbar' : 'home-hero'}>
            <button
              type="button"
              className="home-wordmark"
              onClick={goHome}
              aria-label={t('home.toHome')}
            >
              {BRAND.wordmark}
            </button>
            <SearchBar value={search.query} onChange={search.setQuery} connected={connected} />
            {compact && <div className="home-account">{themeToggle}</div>}
          </div>

          <nav className="home-tabs" role="tablist" aria-label={t('home.tabsAria')}>
            {NAV_PAGES.map((page) => (
              <button
                key={page.id}
                type="button"
                role="tab"
                aria-selected={tab === page.id}
                className={tab === page.id ? 'home-tab home-tab--active' : 'home-tab'}
                onClick={() => {
                  // Search is global and overlays any page, so navigating to a
                  // tab must exit search — otherwise results stay on top.
                  search.clear()
                  setTab(page.id)
                }}
              >
                {t(page.labelKey)}
              </button>
            ))}
          </nav>

          <div
            className={`home-body home-body--${bodyVariant}`}
            role="tabpanel"
            aria-label={t(pageById(tab).labelKey)}
          >
            {search.active ? (
              <SearchResults
                query={search.query}
                sinceMinutes={search.sinceMinutes}
                limit={search.limit}
                onSinceMinutesChange={search.setSinceMinutes}
                onLimitChange={search.setLimit}
                connected={connected}
                collectorsSupported={collectorsSupported}
                hasAnyData={hasAnyData}
                screenTextStatus={screenTextStatus}
              />
            ) : (
              <PageOutlet page={tab} />
            )}
          </div>

          {!compact && <div className="home-account home-account--corner">{themeToggle}</div>}
        </div>
      </main>
      {/* First-run onboarding overlay — fixed, above all page content. */}
      <OnboardingWizard />
    </div>
  )
}
