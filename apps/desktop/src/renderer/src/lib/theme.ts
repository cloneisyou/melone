export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'melone.theme'

export function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark'
}

export function initialTheme(stored: unknown): Theme {
  return isTheme(stored) ? stored : 'light'
}

// Light is the :root default, so only dark sets the attribute — keeps the cascade simple.
export function applyTheme(theme: Theme): void {
  if (theme === 'dark') {
    document.documentElement.dataset['theme'] = 'dark'
  } else {
    delete document.documentElement.dataset['theme']
  }
}
