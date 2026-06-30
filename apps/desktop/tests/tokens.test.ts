import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const tokensPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../src/renderer/src/styles/tokens.css'
)
const css = readFileSync(tokensPath, 'utf8')

// Pins tokens.css to the palette spec in docs/prod/desktop-plan.md "design system"
// (Cursor-derived warm palette — getdesign.md cursor DESIGN.md).
const lightTokens: Record<string, string> = {
  '--bg': '#F7F7F4',
  '--surface': '#FFFFFF',
  '--border': '#E6E5E0',
  '--border-strong': '#CFCDC4',
  '--text': '#26251E',
  '--text-muted': '#807D72',
  '--accent': '#a3c585',
  '--warn': '#C08532',
  '--error': '#CF2D56',
  '--surface-hover': '#EFEEE8'
}

const darkTokens: Record<string, string> = {
  '--bg': '#1B1A16',
  '--surface': '#26251E',
  '--border': '#3A382F',
  '--border-strong': '#4C4A40',
  '--text': '#F7F7F4',
  '--text-muted': '#A09C92',
  '--accent': '#b3cf99',
  '--warn': '#D9A050',
  '--error': '#E25D75',
  '--surface-hover': '#2F2E27'
}

// Strict five-level hierarchy (largest → smallest); each role is a self-contained
// `font` shorthand. Pins the size (and that mono is NOT baked into a level).
const typographyRoles: Record<string, RegExp> = {
  '--type-display': /--type-display:\s*400 30px\/1\.2 var\(--font-sans\)/,
  '--type-title': /--type-title:\s*600 18px\/1\.3 var\(--font-sans\)/,
  '--type-body': /--type-body:\s*400 13px\/1\.5 var\(--font-sans\)/,
  '--type-label': /--type-label:\s*500 12px\/1\.4 var\(--font-sans\)/,
  '--type-caption': /--type-caption:\s*400 11px\/1\.4 var\(--font-sans\)/
}

describe('tokens.css', () => {
  it('defines the light (default) palette exactly as in PLAN', () => {
    for (const [token, value] of Object.entries(lightTokens)) {
      expect(css).toContain(`${token}: ${value}`)
    }
  })

  it('defines the dark palette in a data-theme block', () => {
    expect(css).toContain("[data-theme='dark']")
    for (const [token, value] of Object.entries(darkTokens)) {
      expect(css).toContain(`${token}: ${value}`)
    }
  })

  it('has the pill radius token reserved for the search bar', () => {
    expect(css).toContain('--radius-pill')
  })

  it('defines the five typography roles at the PLAN sizes (sans, no baked-in mono)', () => {
    for (const pattern of Object.values(typographyRoles)) {
      expect(css).toMatch(pattern)
    }
  })

  it('uses the system font stack as the base (SF Pro on macOS)', () => {
    expect(css).toContain('--font-sans:')
    expect(css).toContain('-apple-system')
    // body sets the base voice via the body role, which resolves to the sans stack.
    expect(css).toMatch(/body\s*\{[^}]*font:\s*var\(--type-body\)/)
    expect(css).toMatch(/--type-body:[^;]*var\(--font-sans\)/)
  })

  it('uses a bundled display face for the wordmark, with a sans fallback', () => {
    expect(css).toMatch(/@font-face\s*\{[^}]*'Poiret One'/)
    expect(css).toMatch(/--font-wordmark:\s*'Poiret One',\s*var\(--font-sans\)/)
  })

  it('avoids forbidden patterns (gradients, box shadows)', () => {
    expect(css).not.toMatch(/gradient/i)
    expect(css).not.toMatch(/box-shadow/i)
  })
})
