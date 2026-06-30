// Pure i18n logic — no React/DOM/JSON imports, so both vitest (node) and
// tsconfig.node can import it. Dictionary loading lives in lib/i18n.tsx.

export type Locale = 'en' | 'kr'

/** Render order of the EN / KR locale toggle (now in Settings). */
export const LOCALES: readonly Locale[] = ['en', 'kr']

export const LOCALE_STORAGE_KEY = 'melone.locale'

export type Messages = Record<string, string>
export type MessageTable = Record<Locale, Messages>
export type TranslateVars = Record<string, string | number>

export function isLocale(value: unknown): value is Locale {
  return value === 'en' || value === 'kr'
}

/**
 * Resolves key via locale dict → en dict → the key itself, then fills {placeholders}.
 * Placeholders missing from vars stay verbatim so misses show up on screen.
 */
export function translate(
  table: MessageTable,
  locale: Locale,
  key: string,
  vars?: TranslateVars
): string {
  const template = table[locale][key] ?? table.en[key] ?? key
  if (vars === undefined) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match
  )
}

/**
 * Labels a daemon enum value via the "prefix.value" locale key; unknown values
 * pass through verbatim so they never block debugging.
 */
export function enumLabel(t: (key: string) => string, prefix: string, value: string): string {
  const key = `${prefix}.${value}`
  const label = t(key)
  return label === key ? value : label
}
