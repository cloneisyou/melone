import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactElement, ReactNode } from 'react'
import en from '../locales/en.json'
import kr from '../locales/kr.json'
import { isLocale, LOCALE_STORAGE_KEY, translate } from './i18n-core'
import type { Locale, MessageTable, TranslateVars } from './i18n-core'

export { enumLabel, LOCALES } from './i18n-core'
export type { Locale, TranslateVars } from './i18n-core'

const MESSAGES: MessageTable = { en, kr }

export interface I18n {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: string, vars?: TranslateVars) => string
}

const I18nContext = createContext<I18n | null>(null)

function readStoredLocale(): Locale {
  // Storage access can be blocked by the environment — fall back to en silently.
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY)
    if (isLocale(stored)) return stored
  } catch {
    /* use default */
  }
  return 'en'
}

export function I18nProvider({ children }: { children: ReactNode }): ReactElement {
  const [locale, setLocaleState] = useState<Locale>(readStoredLocale)

  const setLocale = useCallback((next: Locale): void => {
    setLocaleState(next)
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next)
    } catch {
      /* ignore — state keeps the locale for the session */
    }
  }, [])

  // Sync document lang for screen readers (locale file 'kr' maps to HTML lang 'ko').
  useEffect(() => {
    document.documentElement.lang = locale === 'kr' ? 'ko' : 'en'
  }, [locale])

  const value = useMemo<I18n>(
    () => ({ locale, setLocale, t: (key, vars) => translate(MESSAGES, locale, key, vars) }),
    [locale, setLocale]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18n {
  const value = useContext(I18nContext)
  if (value === null) throw new Error('useI18n must be used inside I18nProvider')
  return value
}
