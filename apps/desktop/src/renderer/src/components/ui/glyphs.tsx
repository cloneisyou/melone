// Shared monochrome line glyphs (stand-ins for real app icons/favicons — a later
// backend task). All draw on a 16×16 viewBox and inherit stroke from the parent
// `color`, so callers size them with `size` and tint via CSS color.
import type { ReactElement, ReactNode } from 'react'
import { GLYPH } from '../../config/app'

function Glyph({ size = GLYPH.size, children }: { size?: number; children: ReactNode }): ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.1}
    >
      {children}
    </svg>
  )
}

/** Globe — used for URL/link contexts. */
export function GlobeGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M1.8 8h12.4M8 1.8c2 2 2 10.4 0 12.4M8 1.8c-2 2-2 10.4 0 12.4" />
    </Glyph>
  )
}

/** App window — title-barred rectangle. */
export function WindowGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <rect x="2" y="3" width="12" height="10" rx="1.6" />
      <path d="M2 6h12" />
    </Glyph>
  )
}

/** Rounded square — neutral fallback for an unknown kind. */
export function SquareGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <rect x="2.5" y="2.5" width="11" height="11" rx="2.4" />
    </Glyph>
  )
}

/** Clock — timestamps/ranges. */
export function ClockGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M8 4.6V8l2.4 1.6" />
    </Glyph>
  )
}

/** Chain link — file paths / sources. */
export function LinkGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <path d="M6.5 9.5l3-3M7 4.5l1-1a2.4 2.4 0 0 1 3.4 3.4l-1 1M9 11.5l-1 1A2.4 2.4 0 0 1 4.6 9.1l1-1" />
    </Glyph>
  )
}

/** Text page — OCR/screen-text counts. */
export function TextGlyph({ size }: { size?: number }): ReactElement {
  return (
    <Glyph size={size}>
      <rect x="2.5" y="2.5" width="11" height="11" rx="2" />
      <path d="M5.5 6h5M8 6v4.5" />
    </Glyph>
  )
}

/** Pick a glyph from a context `kind`: url → globe, app_window → window, else square. */
export function KindGlyph({ kind, size }: { kind: string; size?: number }): ReactElement {
  if (kind === 'url') return <GlobeGlyph size={size} />
  if (kind === 'app_window') return <WindowGlyph size={size} />
  return <SquareGlyph size={size} />
}
