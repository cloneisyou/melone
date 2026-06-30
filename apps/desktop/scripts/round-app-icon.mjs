// Generate the packaging icon set from one source: assets/melone_logo.png
// (the finished app icon — squircle + logo already baked in). The same PNG is the
// runtime icon (dock, window, tray@16px, favicon, onboarding drag), so every
// surface shows one logo; this script only produces the platform packaging icons.
//
// Only needed when the logo changes — the outputs below are committed, and CI
// builds from them directly, so the generator's deps are NOT committed (sharp is
// native; the repo otherwise avoids npm image libs). Install them ad-hoc:
//   pnpm add -D sharp png-to-ico @fiahfy/icns
//   node scripts/round-app-icon.mjs
//   pnpm remove sharp png-to-ico @fiahfy/icns
//
// Outputs:
//   build/icon.ico     — Windows packaging (installer / .exe / taskbar)
//   build/icon.icns    — macOS packaging (.app / dock)
//
// The .ico/.icns are pre-built here (png-to-ico + @fiahfy/icns) instead of
// letting electron-builder convert the PNG at pack time — its WASM icon
// converter is unreliable ("WebAssembly.Memory(): could not allocate memory").
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'
import pngToIco from 'png-to-ico'
import * as icns from '@fiahfy/icns'

const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../assets/melone_logo.png')
const buildIco = resolve(here, '../build/icon.ico')
const buildIcns = resolve(here, '../build/icon.icns')

const ICO_SIZES = [16, 24, 32, 48, 64, 128, 256] // ICO format tops out at 256

async function buildIcoFrom(master) {
  const pngs = await Promise.all(ICO_SIZES.map((s) => sharp(master).resize(s, s).png().toBuffer()))
  return pngToIco(pngs)
}

async function buildIcnsFrom(master) {
  const file = new icns.Icns()
  // PNG-based osTypes only (skip legacy raw formats); dedupe is handled by the table.
  for (const { osType, size } of icns.Icns.supportedIconTypes.filter((t) => t.format === 'PNG')) {
    const png = await sharp(master).resize(size, size).png().toBuffer()
    file.append(icns.IcnsImage.fromPNG(png, osType))
  }
  return file.data
}

const master = readFileSync(source)
writeFileSync(buildIco, await buildIcoFrom(master))
writeFileSync(buildIcns, await buildIcnsFrom(master))
console.log('written: build/icon.ico, build/icon.icns')
