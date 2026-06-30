import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
import * as analytics from './lib/analytics'
import { I18nProvider } from './lib/i18n'
import { applyTheme, initialTheme, THEME_STORAGE_KEY } from './lib/theme'
import './styles/tokens.css'
import './styles/app.css'

// Apply the stored theme before first paint to avoid a light/dark flash.
applyTheme(initialTheme(localStorage.getItem(THEME_STORAGE_KEY)))

// Start analytics (a no-op without VITE_POSTHOG_KEY) and mark the launch once,
// here outside React so StrictMode's double-invoke doesn't double-count it.
analytics.init({
  key: import.meta.env.VITE_POSTHOG_KEY,
  appVersion: __APP_VERSION__,
  platform: window.melone.platform,
  isProd: import.meta.env.PROD
})
analytics.track('app_launched')

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </StrictMode>
)
