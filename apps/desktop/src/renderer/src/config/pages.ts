/*
 * Page registry — the single source for which pages exist, how the body is laid
 * out, and which appear in the nav. Adding a page = one entry here (plus the
 * component); the shell and nav read from this list, so there is no per-page
 * conditional to edit. labelKeys resolve in ../locales.
 */
import type { ComponentType } from 'react'
import { HomePage } from '../pages/HomePage'
import { RankPage } from '../pages/RankPage'
import { TimelinePage } from '../pages/TimelinePage'
import { SettingsPage } from '../pages/SettingsPage'
import { IntegrationsPage } from '../pages/IntegrationsPage'

export type PageId = 'home' | 'rank' | 'timeline' | 'mcp' | 'settings'

/** Body layout variant → `home-body--{layout}` class. */
export type PageLayout = 'home' | 'panel' | 'timeline'

export interface PageDef {
  id: PageId
  /** i18n key for the nav label (only pages with `nav: true` show it). */
  labelKey: string
  layout: PageLayout
  /** Shown as a nav tab. Only 'home' (the wordmark returns here) is not a tab. */
  nav: boolean
  Component: ComponentType
}

export const PAGES: readonly PageDef[] = [
  { id: 'home', labelKey: 'menu.home', layout: 'home', nav: false, Component: HomePage },
  { id: 'rank', labelKey: 'menu.rank', layout: 'panel', nav: true, Component: RankPage },
  { id: 'timeline', labelKey: 'menu.timeline', layout: 'timeline', nav: true, Component: TimelinePage },
  { id: 'mcp', labelKey: 'menu.integrations', layout: 'panel', nav: true, Component: IntegrationsPage },
  { id: 'settings', labelKey: 'menu.settings', layout: 'panel', nav: true, Component: SettingsPage }
]

/** Tabs shown in the nav row, in order. */
export const NAV_PAGES: readonly PageDef[] = PAGES.filter((p) => p.nav)

export function pageById(id: PageId): PageDef {
  return PAGES.find((p) => p.id === id) ?? PAGES[0]
}
