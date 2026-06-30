import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'

const desktopRoot = path.dirname(fileURLToPath(import.meta.url))

// App version (package.json) + build date, injected into the renderer so
// Settings → About can show them without an IPC round-trip.
const pkg = JSON.parse(readFileSync(path.join(desktopRoot, 'package.json'), 'utf8')) as {
  version: string
}
const buildDate = new Date().toISOString().slice(0, 10)

// main/preload는 Node 런타임에서 돌므로 의존성을 번들하지 않고 외부화한다.
export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()]
  },
  preload: {
    plugins: [externalizeDepsPlugin()]
  },
  renderer: {
    // Serve assets/ (incl. melone_logo.png) for the window favicon.
    publicDir: path.join(desktopRoot, 'assets'),
    // Load .env (e.g. VITE_POSTHOG_KEY) from the desktop root, not src/renderer.
    envDir: desktopRoot,
    plugins: [react()],
    define: {
      __APP_VERSION__: JSON.stringify(pkg.version),
      __BUILD_DATE__: JSON.stringify(buildDate)
    }
  }
})
