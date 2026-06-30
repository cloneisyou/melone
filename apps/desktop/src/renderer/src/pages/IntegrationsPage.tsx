// Integrations page: toggles the melone MCP server entry in Claude Code / Codex
// config files. The enable/disable behavior lives in useMcpToggle (shared with
// the home setup section and onboarding wizard); this page renders the full
// detail view (config path, detection, parse errors). Target names are proper
// nouns and never translated; failure messages show the daemon's error.data
// verbatim (error contract).
import type { ReactElement } from 'react'
import { useMcpToggle } from '../features/onboarding/useMcpToggle'
import { useI18n } from '../lib/i18n'

export function IntegrationsPage(): ReactElement {
  const { t } = useI18n()
  const { rows, loaded, connected, pendingTarget, rowErrors, statusError, toggle } = useMcpToggle()

  return (
    <div className="home-panel">
      <section className="panel panel--embedded" aria-label={t('mcp.title')}>
        <p className="mcp-intro">{t('mcp.connect')}</p>
        <p className="caption">{t('mcp.skillNote')}</p>

        {rows === null ? (
          <p className="caption">{loaded ? t('status.unavailable') : t('status.loading')}</p>
        ) : (
          rows.map((row) => {
            const parseError = row.enabled === null
            const checked = row.enabled === true
            const disabled = parseError || pendingTarget !== null || !connected
            const rowError = rowErrors[row.key]
            return (
              <div key={row.key} className="mcp-item">
                <div className="mcp-row">
                  <div className="mcp-info">
                    <span className="mcp-name">{row.name}</span>
                    {!row.detected && <span className="caption">{t('mcp.notDetected')}</span>}
                    {parseError && (
                      <span className="caption caption--error">{t('mcp.parseError')}</span>
                    )}
                    <span className="mcp-path" title={row.configPath}>
                      {row.configPath}
                    </span>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={checked}
                    aria-label={t('mcp.toggleAria', { name: row.name })}
                    className={checked ? 'switch switch--on' : 'switch'}
                    disabled={disabled}
                    onClick={() => {
                      toggle(row.key, !checked)
                    }}
                  />
                </div>
                {rowError !== undefined && <p className="caption caption--error">{rowError}</p>}
              </div>
            )
          })
        )}

        {statusError !== null && <p className="caption caption--error">{statusError}</p>}
      </section>
    </div>
  )
}
