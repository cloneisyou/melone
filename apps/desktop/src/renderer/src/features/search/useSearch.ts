// Search query state — the reusable core of the search feature. Owns the query
// string and derives whether a search is "active" (non-empty). Pair with
// <SearchBar> for input and <SearchResults> for the grid.
import { useState } from 'react'
import { SEARCH } from '../../config/app'

export interface SearchState {
  query: string
  setQuery: (value: string) => void
  sinceMinutes: number
  setSinceMinutes: (value: number) => void
  limit: number
  setLimit: (value: number) => void
  /** True when the trimmed query is non-empty (results should show). */
  active: boolean
  /** Reset to the idle (empty) query. */
  clear: () => void
}

export function useSearch(): SearchState {
  const [query, setQuery] = useState('')
  const [sinceMinutes, setSinceMinutes] = useState<number>(SEARCH.sinceMinutes)
  const [limit, setLimit] = useState<number>(SEARCH.limit)
  return {
    query,
    setQuery,
    sinceMinutes,
    setSinceMinutes,
    limit,
    setLimit,
    active: query.trim() !== '',
    clear: () => {
      setQuery('')
      setSinceMinutes(SEARCH.sinceMinutes)
      setLimit(SEARCH.limit)
    }
  }
}
