// Root: provide app-wide state, then render the shell. All data/polling lives in
// AppStateProvider; all layout/pages live under Shell.
import type { ReactElement } from 'react'
import { Shell } from './components/shell/Shell'
import { AppStateProvider } from './context/app-state'

export function App(): ReactElement {
  return (
    <AppStateProvider>
      <Shell />
    </AppStateProvider>
  )
}
