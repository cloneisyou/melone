// Shared MCP enable/disable logic for the Integrations page, the home setup
// section, and the onboarding wizard. Reads mcpStatus from app-state and exposes
// per-target rows plus a toggle that mirrors the daemon call + analytics + an
// immediate refresh. Markup stays with each surface; only the behavior is shared.
import { useState } from 'react'
import { RPC } from '../../config/rpc'
import { useAppState } from '../../context/app-state'
import * as analytics from '../../lib/analytics'
import { humanErrorMessage, requestDaemon } from '../../lib/daemon'

export type McpTargetKey = 'claudeCode' | 'codex'

interface TargetDef {
  key: McpTargetKey
  /** target param value for mcp.enable / mcp.disable. */
  target: 'claude-code' | 'codex'
  /** Proper noun — never translated. */
  name: string
}

// Exported so surfaces can render a stable list before mcpStatus has loaded.
export const MCP_TARGETS: readonly TargetDef[] = [
  { key: 'claudeCode', target: 'claude-code', name: 'Claude Code MCP' },
  { key: 'codex', target: 'codex', name: 'Codex MCP' }
]

export interface McpRow extends TargetDef {
  detected: boolean
  /** null = the config file could not be parsed. */
  enabled: boolean | null
  configPath: string
  error?: string
}

export interface McpToggle {
  /** Per-target rows, or null until mcpStatus first loads. */
  rows: McpRow[] | null
  loaded: boolean
  connected: boolean
  /** The target currently being toggled (disable controls), or null. */
  pendingTarget: McpTargetKey | null
  /** Last per-row failure message, keyed by target. */
  rowErrors: Partial<Record<McpTargetKey, string>>
  /** mcp.status fetch error (whole-section), or null. */
  statusError: string | null
  /** True when at least one integration is enabled. */
  anyEnabled: boolean
  toggle: (key: McpTargetKey, next: boolean) => void
}

export function useMcpToggle(): McpToggle {
  const { bridge, mcpStatus, loaded, errors, refresh } = useAppState()
  const [pendingTarget, setPendingTarget] = useState<McpTargetKey | null>(null)
  const [rowErrors, setRowErrors] = useState<Partial<Record<McpTargetKey, string>>>({})

  const connected = bridge.status === 'connected'
  const rows: McpRow[] | null =
    mcpStatus === null ? null : MCP_TARGETS.map((def) => ({ ...def, ...mcpStatus[def.key] }))
  const anyEnabled = rows !== null && rows.some((row) => row.enabled === true)

  const toggle = (key: McpTargetKey, next: boolean): void => {
    const def = MCP_TARGETS.find((target) => target.key === key)
    if (def === undefined) return
    setPendingTarget(key)
    setRowErrors((prev) => ({ ...prev, [key]: undefined }))
    void (async () => {
      try {
        await requestDaemon(next ? RPC.mcp.enable : RPC.mcp.disable, { target: def.target })
        analytics.trackMcpToggle({ target: def.target, enabled: next })
        refresh()
      } catch (error) {
        setRowErrors((prev) => ({ ...prev, [key]: humanErrorMessage(error) }))
      } finally {
        setPendingTarget(null)
      }
    })()
  }

  return {
    rows,
    loaded,
    connected,
    pendingTarget,
    rowErrors,
    statusError: errors.mcp,
    anyEnabled,
    toggle
  }
}
