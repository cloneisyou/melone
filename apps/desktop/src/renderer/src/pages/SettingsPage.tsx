// Settings page: grouped rows — Recording, Screen Text Search, Permissions,
// Storage (local footprint), Language, and About (version / build / schema / db).
import { Fragment, useEffect, useState } from 'react'
import type { ReactElement } from 'react'
import { RPC } from '../config/rpc'
import { useAppState } from '../context/app-state'
import * as analytics from '../lib/analytics'
import { requestDaemon } from '../lib/daemon'
import type { ScreenTextStatus, ServiceStatus, StorageStats } from '../lib/daemon'
import { formatBytes } from '../lib/format'
import { enumLabel, LOCALES, useI18n } from '../lib/i18n'
import { useDaemonAction } from '../lib/use-daemon-action'

type Translate = (key: string, vars?: Record<string, string | number>) => string

/** The recording-control RPCs the toggle invokes (start = collect, stop = kill). */
type ServiceAction = typeof RPC.service.start | typeof RPC.service.stop

/** Maps each recording RPC to its analytics action label. */
const RECORDING_ACTION = {
  [RPC.service.start]: 'started',
  [RPC.service.stop]: 'stopped'
} as const

function permissionClass(status: string): string {
  if (status === 'denied') return 'perm-status perm-status--denied'
  if (status === 'granted') return 'perm-status'
  return 'perm-status perm-status--muted'
}

export function SettingsPage(): ReactElement {
  const { t, locale, setLocale } = useI18n()
  const { bridge, serviceStatus, screenTextStatus, storage, loaded, errors, refresh } =
    useAppState()
  const action = useDaemonAction()
  const [desiredRecording, setDesiredRecording] = useState<boolean | null>(null)

  const status = serviceStatus
  const connected = bridge.status === 'connected'
  const busy = action.pending || !connected
  // Recording = the collector process is alive. The toggle starts/stops (kills)
  // that process rather than pausing it.
  const recording = status !== null && status.running
  const displayedRecording = desiredRecording ?? recording
  const screenTextEnabled = screenTextStatus?.settings.enabled ?? false
  // Daemon power is driven by bridge state, not RPC — it must work even when the
  // daemon is off (RPC unavailable). 'disabled' is the only off state.
  const daemonEnabled = bridge.status !== 'disabled'

  const handleDaemonPower = (): void => {
    void window.melone.setServicePower(!daemonEnabled)
  }

  const handleAction = (method: ServiceAction): Promise<void> =>
    action.run(async () => {
      await requestDaemon(method)
      analytics.trackRecording(RECORDING_ACTION[method])
      refresh()
    })

  useEffect(() => {
    if (desiredRecording === null) return
    if (recording === desiredRecording) setDesiredRecording(null)
  }, [desiredRecording, recording])

  useEffect(() => {
    if (!action.pending && action.error !== null) setDesiredRecording(null)
  }, [action.error, action.pending])

  const handleScreenTextToggle = (): Promise<void> =>
    action.run(async () => {
      await requestDaemon(RPC.screenText.updateSettings, { enabled: !screenTextEnabled })
      refresh()
    })

  const recordingSub = (s: ServiceStatus): string => {
    if (desiredRecording !== null) {
      return desiredRecording ? t('service.running') : t('service.stopped')
    }
    if (!s.running) return t('service.stopped')
    return s.pid === null ? t('service.running') : `${t('service.running')} · pid ${String(s.pid)}`
  }

  return (
    <div className="home-panel">
      <section className="panel panel--embedded settings" aria-label={t('settings.aria')}>
        {/* Daemon power — the local engine behind search/graph. Off stops it
            entirely (and stays off across restarts); independent of RPC. */}
        <h2 className="section-title">{t('settings.daemon')}</h2>
        <div className="setting-row">
          <div className="setting-copy">
            <span className="setting-label">{t('settings.daemonLabel')}</span>
            <span className="caption">{daemonCaption(bridge, t)}</span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={daemonEnabled}
            aria-label={t('settings.daemonLabel')}
            className={daemonEnabled ? 'switch switch--on' : 'switch'}
            onClick={handleDaemonPower}
          />
        </div>

        <h2 className="section-title">{t('settings.recording')}</h2>
        {status === null ? (
          <p className="caption">{loaded ? t('status.unavailable') : t('status.loading')}</p>
        ) : !status.collectorsSupported ? (
          <p className="caption">{t('service.noCollectors')}</p>
        ) : (
          <>
            {/* One control: on = actively collecting. Off stops the collector
                process (SIGTERM via service.stop); on starts a fresh one. The
                RPC daemon itself stays up either way. */}
            <div className="setting-row">
              <div className="setting-copy">
                <span className="setting-label">{t('settings.recordingLabel')}</span>
                <span className="caption">{recordingSub(status)}</span>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={displayedRecording}
                aria-label={t('settings.recordingLabel')}
                className={displayedRecording ? 'switch switch--on' : 'switch'}
                disabled={busy}
                onClick={() => {
                  setDesiredRecording(!recording)
                  // Off kills the collector process; on spawns a fresh one.
                  void handleAction(recording ? RPC.service.stop : RPC.service.start)
                }}
              />
            </div>

            <h2 className="section-title">{t('screenText.title')}</h2>
            <div className="setting-row">
              <div className="setting-copy">
                <span className="setting-label">{t('screenText.title')}</span>
                <span className="caption">{screenTextCaption(screenTextStatus, loaded, t)}</span>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={screenTextEnabled}
                aria-label={t('screenText.toggleAria')}
                className={screenTextEnabled ? 'switch switch--on' : 'switch'}
                disabled={busy || screenTextStatus === null}
                onClick={() => {
                  void handleScreenTextToggle()
                }}
              />
            </div>
            {errors.screenText !== null && (
              <p className="caption caption--error">{errors.screenText}</p>
            )}

            {Object.keys(status.permissions.permissions).length > 0 && (
              <>
                <h2 className="section-title">{t('settings.permissions')}</h2>
                <ul className="perm-list" aria-label={t('service.permissionsAria')}>
                  {Object.entries(status.permissions.permissions).map(([name, entry]) => (
                    <li key={name} className="perm-row">
                      <span className="perm-name">{name}</span>
                      <span className={permissionClass(entry.status)}>
                        {enumLabel(t, 'permission', entry.status)}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )}

        {/* Local storage footprint — independent of collection support, so it
            sits outside the recording block. */}
        <h2 className="section-title">{t('stats.breakdownTitle')}</h2>
        <StorageSection storage={storage} loaded={loaded} error={errors.storage} t={t} />

        <h2 className="section-title">{t('settings.language')}</h2>
        <div className="setting-row">
          <div className="setting-copy">
            <span className="setting-label">{t('settings.language')}</span>
          </div>
          <div className="setting-lang" role="group" aria-label={t('settings.language')}>
            {LOCALES.map((code, index) => (
              <Fragment key={code}>
                {index > 0 && (
                  <span className="locale-divider" aria-hidden="true">
                    /
                  </span>
                )}
                <button
                  type="button"
                  className={code === locale ? 'locale-button locale-button--active' : 'locale-button'}
                  aria-pressed={code === locale}
                  onClick={() => {
                    setLocale(code)
                  }}
                >
                  {code.toUpperCase()}
                </button>
              </Fragment>
            ))}
          </div>
        </div>

        <h2 className="section-title">{t('settings.about')}</h2>
        <dl className="data-list settings-about">
          <div className="data-row">
            <dt className="data-key">{t('settings.version')}</dt>
            <dd className="data-value">{__APP_VERSION__}</dd>
          </div>
          <div className="data-row">
            <dt className="data-key">{t('settings.built')}</dt>
            <dd className="data-value">{__BUILD_DATE__}</dd>
          </div>
          {status !== null && (
            <div className="data-row">
              <dt className="data-key">{t('settings.schema')}</dt>
              <dd className="data-value">v{status.migrationVersion}</dd>
            </div>
          )}
          {status !== null && (
            <div className="data-row">
              <dt className="data-key">{t('settings.db')}</dt>
              <dd className="data-value" title={status.dbPath}>
                {status.dbPath}
              </dd>
            </div>
          )}
        </dl>

        {action.error !== null && <p className="caption caption--error">{action.error}</p>}
        {errors.service !== null && <p className="caption caption--error">{errors.service}</p>}
      </section>
    </div>
  )
}

function storagePct(part: number, whole: number): number {
  if (whole <= 0) return 0
  return Math.round((part / whole) * 100)
}

/** Local storage footprint: total + a stacked screenshots/database/logs bar. */
function StorageSection({
  storage,
  loaded,
  error,
  t
}: {
  storage: StorageStats | null
  loaded: boolean
  error: string | null
  t: Translate
}): ReactElement {
  if (error !== null) return <p className="caption caption--error">{error}</p>
  if (storage === null) {
    return <p className="caption">{loaded ? t('stats.empty') : t('status.loading')}</p>
  }

  // Default each byte field — an older daemon may omit some, which would
  // otherwise surface as NaN in formatBytes / the percentages.
  const total = storage.totalBytes ?? 0
  const segments = [
    { key: 'screenshots', label: t('stats.screenshots'), bytes: storage.screenshotBytes ?? 0, color: 'var(--accent)' },
    { key: 'database', label: t('stats.database'), bytes: storage.databaseBytes ?? 0, color: 'var(--text-muted)' },
    { key: 'logs', label: t('stats.logs'), bytes: storage.logBytes ?? 0, color: 'var(--border-strong)' }
  ]

  return (
    <div className="stats" aria-label={t('stats.aria')}>
      <div className="stats-total">
        <span className="stats-total-value">{formatBytes(total)}</span>
        <span className="stats-total-label">{t('stats.totalLabel')}</span>
      </div>
      <div
        className="stats-bar"
        role="img"
        aria-label={segments.map((s) => `${s.label} ${formatBytes(s.bytes)}`).join(', ')}
      >
        {segments.map((segment) => {
          const width = total > 0 ? (segment.bytes / total) * 100 : 0
          if (width <= 0) return null
          return (
            <span
              key={segment.key}
              className="stats-bar-seg"
              style={{ width: `${String(width)}%`, background: segment.color }}
            />
          )
        })}
      </div>
      <dl className="stats-legend">
        {segments.map((segment) => (
          <div key={segment.key} className="stats-legend-row">
            <span className="stats-swatch" style={{ background: segment.color }} aria-hidden="true" />
            <dt className="stats-legend-label">{segment.label}</dt>
            <dd className="stats-legend-bytes">{formatBytes(segment.bytes)}</dd>
            <dd className="stats-legend-pct">{storagePct(segment.bytes, total)}%</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function daemonCaption(bridge: MeloneBridgeState, t: Translate): string {
  if (bridge.status === 'disabled') return t('settings.daemonHintOff')
  if (bridge.status === 'connecting') return t('statusline.connecting')
  if (bridge.status === 'down') {
    return bridge.detail === null ? t('statusline.down') : `${t('statusline.down')} · ${bridge.detail}`
  }
  return bridge.pid === null
    ? t('settings.daemonHintOn')
    : `${t('settings.daemonHintOn')} · pid ${String(bridge.pid)}`
}

function screenTextCaption(status: ScreenTextStatus | null, loaded: boolean, t: Translate): string {
  if (status === null) return loaded ? t('status.unavailable') : t('status.loading')
  if (status.state === 'off') return t('screenText.caption.off')
  if (status.state === 'blocked') {
    if (status.reason === 'screen_recording_permission_required') {
      return t('screenText.caption.permission')
    }
    return t('screenText.caption.provider')
  }
  if (status.state === 'indexing') {
    return t('screenText.caption.indexing', { count: status.backlogCount })
  }
  if (status.state === 'error') return t('screenText.caption.error')
  return status.latestIndexedAt === null
    ? t('screenText.caption.ready')
    : t('screenText.caption.readyLatest', { time: status.latestIndexedAt })
}
