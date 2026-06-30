/*
 * Renderer tunables — the single place to change timing, page sizes, fetch
 * limits, and other knobs. Components must read from here, never inline a
 * literal (코드에 특정값 박아넣지 말기). RPC method names live in ./rpc;
 * user-facing copy lives in ../locales.
 */

/** Brand constants (proper nouns — not translated, so they live here, not in locales). */
export const BRAND = {
  wordmark: 'Melone'
} as const

/** External links opened in the default browser (http/https only — see open-target.ts). */
export const LINKS = {
  /** User feedback form, reachable from the persistent corner link on every page. */
  feedback:
    'https://docs.google.com/forms/d/e/1FAIpQLSdrG4OR2UBJ5v9BVjlye0J3to0hqJ8zAJlmx7Bt9NLOL2JHtw/viewform?usp=dialog'
} as const

/** Background poll for the dashboard sections (visible + connected only). */
export const POLL = {
  intervalMs: 5_000,
  /** Faster lightweight status refresh while a permission grant is pending. */
  permissionIntervalMs: 750
} as const

/** Global search box → context.search. */
export const SEARCH = {
  /** Debounce before firing a query while typing. */
  debounceMs: 250,
  /** Max results per query. */
  limit: 60,
  /** Look-back window for matches. */
  sinceMinutes: 7 * 24 * 60,
  /** User-selectable look-back windows for desktop search. */
  timeScopes: [
    { minutes: 60, labelKey: 'search.scope1h' },
    { minutes: 24 * 60, labelKey: 'search.scope24h' },
    { minutes: 7 * 24 * 60, labelKey: 'search.scope7d' },
    { minutes: 30 * 24 * 60, labelKey: 'search.scope30d' }
  ],
  /** User-selectable result caps for desktop search. */
  resultLimits: [24, 60, 100, 200]
} as const

/** Rank cards (Top URI / Top App). */
export const RANK = {
  /** Look-back window — "Last 24 hours". */
  sinceMinutes: 24 * 60,
  /** Fetch with headroom; the view trims to tableLimit per kind. */
  fetchLimit: 100,
  /** Rows shown per card. */
  tableLimit: 12
} as const

/** Recent-scene carousel on the idle home. */
export const PREVIEWS = {
  limit: 12
} as const

/** Scene timeline strip + keyset paging. */
export const TIMELINE = {
  /** Scenes fetched per page. */
  pageSize: 30,
  /** Cap short sticks per scene so a busy scene can't blow out the strip. */
  maxShortSticks: 16,
  /** Distance from the left edge that triggers loading older scenes. */
  loadEdgePx: 300,
  /** Slack when testing whether the strip overflows. */
  overflowEpsilonPx: 4
} as const

/** Kind-glyph render sizes (px on a 16px viewBox). */
export const GLYPH = {
  /** Default for most glyph call sites. */
  size: 15,
  /** Compact glyph in search-result cards. */
  resultSize: 14
} as const
