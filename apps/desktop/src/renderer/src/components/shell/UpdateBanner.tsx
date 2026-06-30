// Auto-update banner. Hidden by design — it appears ONLY when a new version is
// actually available (or while downloading/ready/failed). idle/checking/up-to-date
// render nothing, so the update button never lingers in normal use.
import { useEffect, useState } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { useI18n } from '../../lib/i18n'

const INITIAL: MeloneUpdateState = { phase: 'idle' }

function BannerShell({
  tone,
  progress,
  children
}: {
  tone?: 'error'
  // number → determinate fill (download %); 'indeterminate' → animated bar (install).
  progress?: number | 'indeterminate'
  children: ReactNode
}): ReactElement {
  return (
    <div
      className={tone === 'error' ? 'update-banner update-banner--error' : 'update-banner'}
      role="status"
    >
      {children}
      {progress !== undefined && (
        <div
          className={
            progress === 'indeterminate'
              ? 'update-progress update-progress--indeterminate'
              : 'update-progress'
          }
          aria-hidden="true"
        >
          <div
            className="update-progress-fill"
            style={progress === 'indeterminate' ? undefined : { width: `${progress}%` }}
          />
        </div>
      )}
    </div>
  )
}

export function UpdateBanner(): ReactElement | null {
  const { t } = useI18n()
  const [state, setState] = useState<MeloneUpdateState>(INITIAL)
  // Set the moment install is triggered (auto on download, or via the button).
  // quitAndInstall takes a few seconds to extract + relaunch; without this the
  // banner sits on "ready — restarting" and looks frozen.
  const [installing, setInstalling] = useState(false)

  useEffect(() => window.melone.onUpdateState(setState), [])

  // On completion the update installs automatically (the app relaunches). In dev
  // the install is a no-op, so the banner just rests on the installing spinner.
  useEffect(() => {
    if (state.phase === 'downloaded') {
      setInstalling(true)
      window.melone.installUpdate()
    }
  }, [state.phase])

  // Installing/restarting takes over the banner regardless of phase — the app is
  // about to relaunch, so show unambiguous progress instead of a stale message.
  if (installing) {
    return (
      <BannerShell progress="indeterminate">
        <span className="spinner" aria-hidden="true" />
        <span className="update-banner-text">{t('update.installing')}</span>
      </BannerShell>
    )
  }

  switch (state.phase) {
    case 'available':
      return (
        <BannerShell>
          <span className="dot dot--on" aria-hidden="true" />
          <span className="update-banner-text">
            {t('update.available', { version: state.version })}
          </span>
          <button
            type="button"
            className="button"
            onClick={() => {
              void window.melone.downloadUpdate()
            }}
          >
            {t('update.download')}
          </button>
        </BannerShell>
      )
    case 'downloading':
      return (
        <BannerShell progress={state.percent}>
          <span className="dot" aria-hidden="true" />
          <span className="update-banner-text">
            {t('update.downloading', { percent: state.percent })}
          </span>
          <button type="button" className="button" disabled>
            {t('update.download')}
          </button>
        </BannerShell>
      )
    case 'downloaded':
      return (
        <BannerShell>
          <span className="dot dot--on" aria-hidden="true" />
          <span className="update-banner-text">{t('update.readyRestart')}</span>
          <button
            type="button"
            className="button"
            onClick={() => {
              setInstalling(true)
              window.melone.installUpdate()
            }}
          >
            {t('update.restartNow')}
          </button>
        </BannerShell>
      )
    case 'error':
      return (
        <BannerShell tone="error">
          <span className="dot dot--error" aria-hidden="true" />
          <span className="update-banner-text caption--error">{t('update.error')}</span>
          <button
            type="button"
            className="button"
            onClick={() => {
              void window.melone.checkForUpdates()
            }}
          >
            {t('update.retry')}
          </button>
        </BannerShell>
      )
    default:
      // idle / checking / none → nothing to show.
      return null
  }
}
