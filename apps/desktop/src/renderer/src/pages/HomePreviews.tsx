// Home-tab body: a horizontal row of recent scene previews (the first retained
// screenshot of each scene). Clicking a card opens its source through
// window.melone.open — URLs in the browser, apps activated on macOS — mirroring
// the search result behavior.
import type { ReactElement } from 'react'
import type { ScenePreview } from '../lib/daemon'
import { useI18n } from '../lib/i18n'

interface HomePreviewsProps {
  previews: ScenePreview[] | null
  loaded: boolean
  error: string | null
}

export function HomePreviews({ previews, loaded, error }: HomePreviewsProps): ReactElement {
  const { t } = useI18n()

  const handleOpen = async (preview: ScenePreview): Promise<void> => {
    await window.melone.open({
      kind: preview.kind as MeloneOpenTarget['kind'],
      url: preview.url,
      appName: preview.url === null ? preview.appName : null
    })
  }

  if (error !== null) {
    return <p className="caption caption--error home-previews-empty">{error}</p>
  }

  if (previews === null) {
    return (
      <p className="caption home-previews-empty">{loaded ? '' : t('context.loading')}</p>
    )
  }

  if (previews.length === 0) {
    return <p className="caption home-previews-empty">{t('home.previewsEmpty')}</p>
  }

  return (
    <div className="home-previews" role="list" aria-label={t('home.previewsAria')}>
      {previews.map((preview) => (
        <button
          key={preview.key}
          type="button"
          role="listitem"
          className="home-preview"
          title={preview.label}
          onClick={() => {
            void handleOpen(preview)
          }}
        >
          <img
            className="home-preview-thumb"
            src={preview.image}
            alt={preview.label}
            draggable={false}
          />
          <span className="home-preview-caption">{preview.label}</span>
        </button>
      ))}
    </div>
  )
}
