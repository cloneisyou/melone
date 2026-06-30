// Home body shown until the user has connected an agent: "Use it in Claude Code
// / Codex" — compact integration toggles, copyable example prompts, and a button
// to open the full first-run wizard. Once an integration is enabled, HomePage
// swaps this for the scene carousel.
import type { ReactElement } from 'react'
import { useAppState } from '../context/app-state'
import { ExamplePrompts } from '../features/onboarding/ExamplePrompts'
import { useMcpToggle } from '../features/onboarding/useMcpToggle'
import { useI18n } from '../lib/i18n'

export function HomeSetup(): ReactElement {
  const { t } = useI18n()
  const { openOnboarding } = useAppState()
  const { rows, loaded, connected, pendingTarget, rowErrors, toggle } = useMcpToggle()

  return (
    <div className="home-setup">
      <div className="home-setup-head">
        <h2 className="home-setup-title">{t('onboarding.homeTitle')}</h2>
        <p className="home-setup-intro">{t('onboarding.homeIntro')}</p>
      </div>

      <section className="onboarding-toggles" aria-label={t('mcp.title')}>
        {rows === null ? (
          <p className="caption">{loaded ? t('status.unavailable') : t('status.loading')}</p>
        ) : (
          rows.map((row) => {
            const parseError = row.enabled === null
            const checked = row.enabled === true
            const disabled = parseError || pendingTarget !== null || !connected
            const rowError = rowErrors[row.key]
            return (
              <div key={row.key} className="onboarding-toggle">
                <div className="onboarding-toggle-row">
                  <span className="mcp-name">{row.name}</span>
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
                {parseError && <p className="caption caption--error">{t('mcp.parseError')}</p>}
                {rowError !== undefined && <p className="caption caption--error">{rowError}</p>}
              </div>
            )
          })
        )}
      </section>

      <div className="home-setup-examples">
        <h3 className="home-setup-subtitle">{t('onboarding.examplesTitle')}</h3>
        <ExamplePrompts />
      </div>

      <button type="button" className="button" onClick={openOnboarding}>
        {t('onboarding.openWizard')}
      </button>
    </div>
  )
}
