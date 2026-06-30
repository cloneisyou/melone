// Renders the active page's component from the registry — the one place that
// maps a PageId to UI, so there is no per-page conditional anywhere.
import type { ReactElement } from 'react'
import { pageById } from '../../config/pages'
import type { PageId } from '../../config/pages'

export function PageOutlet({ page }: { page: PageId }): ReactElement {
  const Component = pageById(page).Component
  return <Component />
}
