// First-run onboarding overlay. Three steps:
//   1. Permissions — rows derived from the daemon's permission report; known
//      macOS panes get an "Open Settings →" deep link (window.melone.openSystemSettings).
//   2. Connect agent — the shared MCP toggles plus copyable config snippets.
//   3. Try it — copyable example prompts.
// Visibility + the persisted completion flag live in app-state (onboardingOpen /
// completeOnboarding). Mounted once in Shell, above the page content.
import { useEffect, useState } from 'react'
import type { ReactElement } from 'react'
import { BRAND } from '../../config/app'
import { useAppState } from '../../context/app-state'
import { enumLabel, useI18n } from '../../lib/i18n'
import { CopyButton } from './CopyButton'
import { ExamplePrompts } from './ExamplePrompts'
import { useMcpToggle } from './useMcpToggle'

const IS_MAC = window.melone.platform === 'darwin'

const STEP_KEYS = ['permissions', 'connect', 'try'] as const
type StepKey = (typeof STEP_KEYS)[number]

// Illustrative manual-setup snippets (the toggle does the real registration).
const SNIPPETS: Record<'claudeCode' | 'codex', string> = {
  claudeCode: `"melone": {
  "type": "stdio",
  "command": "python3",
  "args": ["-m", "melone_service.mcp"]
}`,
  codex: `[mcp_servers.melone]
command = "python3"
args = ["-m", "melone_service.mcp"]`
}

function permissionClass(status: string): string {
  if (status === 'denied') return 'perm-status perm-status--denied'
  if (status === 'granted') return 'perm-status'
  return 'perm-status perm-status--muted'
}

// Map a daemon permission name to a deep-linkable macOS pane, if we recognize it.
function paneForPermission(name: string): MeloneSettingsPane | null {
  const lower = name.toLowerCase()
  if (lower.includes('screen')) return 'screen-recording'
  if (lower.includes('accessibility')) return 'accessibility'
  return null
}

// Where to land when the wizard opens. After the user grants permissions and
// relaunches, the permissions step is already done — start on "connect" so the
// previously-skipped Connect/Try steps are what they see, not step 1 again.
function initialStepIndex(
  serviceStatus: ReturnType<typeof useAppState>['serviceStatus']
): number {
  const done = serviceStatus !== null && serviceStatus.permissions.missingRequiredPermissions.length === 0
  return done ? STEP_KEYS.indexOf('connect') : 0
}

export function OnboardingWizard(): ReactElement | null {
  const { t } = useI18n()
  const { onboardingOpen, completeOnboarding, serviceStatus } = useAppState()
  const [stepIndex, setStepIndex] = useState(() => initialStepIndex(serviceStatus))

  // Pick the landing step each time the wizard opens (the component stays mounted
  // in Shell, so a one-time initializer can't see post-grant status). Reading
  // serviceStatus here rather than in deps is deliberate: we set the step only on
  // open, not on every 5s status poll — that would yank the user between steps.
  useEffect(() => {
    if (onboardingOpen) setStepIndex(initialStepIndex(serviceStatus))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onboardingOpen])

  if (!onboardingOpen) return null

  const step: StepKey = STEP_KEYS[stepIndex]
  const isFirst = stepIndex === 0
  const isLast = stepIndex === STEP_KEYS.length - 1

  const back = (): void => {
    setStepIndex((index) => Math.max(0, index - 1))
  }
  const next = (): void => {
    if (isLast) completeOnboarding()
    else setStepIndex((index) => Math.min(STEP_KEYS.length - 1, index + 1))
  }

  return (
    <div className="onboarding-overlay" role="dialog" aria-modal="true" aria-label={t('onboarding.title')}>
      <div className="onboarding-modal">
        <div className="onboarding-header">
          <span className="onboarding-eyebrow">{t('onboarding.title')}</span>
          <button type="button" className="onboarding-skip" onClick={completeOnboarding}>
            {t('onboarding.skip')}
          </button>
        </div>

        <div className="onboarding-steps" aria-hidden="true">
          {STEP_KEYS.map((key, index) => (
            <span
              key={key}
              className={index === stepIndex ? 'onboarding-dot onboarding-dot--active' : 'onboarding-dot'}
            />
          ))}
        </div>

        <div className="onboarding-body">
          {step === 'permissions' && <PermissionsStep serviceStatus={serviceStatus} />}
          {step === 'connect' && <ConnectStep />}
          {step === 'try' && <TryStep />}
        </div>

        <div className="onboarding-footer">
          <button
            type="button"
            className="button button--ghost"
            onClick={back}
            disabled={isFirst}
          >
            {t('onboarding.back')}
          </button>
          <button type="button" className="button" onClick={next}>
            {isLast ? t('onboarding.done') : t('onboarding.next')}
          </button>
        </div>
      </div>
    </div>
  )
}

function PermissionsStep({
  serviceStatus
}: {
  serviceStatus: ReturnType<typeof useAppState>['serviceStatus']
}): ReactElement {
  const { t } = useI18n()
  const entries = serviceStatus ? Object.entries(serviceStatus.permissions.permissions) : []
  // The same app gets added to whichever privacy list, so the drag source is
  // shown once (not per row) while any recognized pane permission is ungranted.
  const showDrag =
    IS_MAC &&
    entries.some(([name, entry]) => paneForPermission(name) !== null && entry.status !== 'granted')

  return (
    <div className="onboarding-step">
      <h2 className="onboarding-title">{t('onboarding.permissionsTitle')}</h2>
      <p className="onboarding-lead">{t('onboarding.permissionsLead')}</p>

      {showDrag && (
        <div className="onboarding-drag-card">
          <button
            type="button"
            className="onboarding-drag-app"
            draggable
            onDragStart={(event) => {
              // Hand the drag to the OS so it carries the real app bundle, not
              // the DOM node — System Settings then accepts the drop.
              event.preventDefault()
              window.melone.startPermissionDrag()
            }}
            aria-label={t('onboarding.permissionsDragAria')}
          >
            <img className="onboarding-drag-icon" src="./melone_logo.png" alt="" draggable={false} />
            <span className="onboarding-drag-name">{BRAND.wordmark}</span>
          </button>
          <span className="onboarding-drag-arrow" aria-hidden="true">
            →
          </span>
          <p className="onboarding-drag-hint">{t('onboarding.permissionsDragHint')}</p>
        </div>
      )}

      {entries.length === 0 ? (
        <p className="caption">{t('onboarding.permissionsNone')}</p>
      ) : (
        <ul className="onboarding-perm-list">
          {entries.map(([name, entry]) => {
            const pane = paneForPermission(name)
            return (
              <li key={name} className="onboarding-perm-row">
                <div className="onboarding-perm-info">
                  <span className="perm-name">{name}</span>
                  <span className={permissionClass(entry.status)}>
                    {enumLabel(t, 'permission', entry.status)}
                  </span>
                </div>
                {IS_MAC && pane !== null && entry.status !== 'granted' && (
                  <button
                    type="button"
                    className="button button--ghost"
                    onClick={() => {
                      void window.melone.openSystemSettings(pane)
                    }}
                  >
                    {t('onboarding.openSettings')}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function ConnectStep(): ReactElement {
  const { t } = useI18n()
  const { rows, loaded, connected, pendingTarget, rowErrors, toggle } = useMcpToggle()

  return (
    <div className="onboarding-step">
      <h2 className="onboarding-title">{t('onboarding.connectTitle')}</h2>
      <p className="onboarding-lead">{t('onboarding.connectLead')}</p>
      <p className="onboarding-lead">{t('onboarding.connectSkillNote')}</p>

      {rows === null ? (
        <p className="caption">{loaded ? t('status.unavailable') : t('status.loading')}</p>
      ) : (
        rows.map((row) => {
          const parseError = row.enabled === null
          const checked = row.enabled === true
          const disabled = parseError || pendingTarget !== null || !connected
          const rowError = rowErrors[row.key]
          return (
            <div key={row.key} className="onboarding-connect-item">
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
              <div className="onboarding-snippet">
                <pre className="onboarding-snippet-code">
                  <code>{SNIPPETS[row.key]}</code>
                </pre>
                <CopyButton text={SNIPPETS[row.key]} label={t('onboarding.copySnippet')} />
              </div>
            </div>
          )
        })
      )}
      <p className="caption">{t('onboarding.connectManual')}</p>
    </div>
  )
}

function TryStep(): ReactElement {
  const { t } = useI18n()
  return (
    <div className="onboarding-step">
      <h2 className="onboarding-title">{t('onboarding.tryTitle')}</h2>
      <p className="onboarding-lead">{t('onboarding.tryLead')}</p>
      <ExamplePrompts />
    </div>
  )
}
