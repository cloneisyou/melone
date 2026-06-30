// Scene timeline: a horizontal strip of sticks grouped by scene. Each scene is
// one tall stick (its keyframe screenshot) plus a short stick per extra log.
// The newest scene sits at the right edge; the user scrolls left for older
// scenes (which page in on the left). Selecting a scene highlights its sticks
// grey-green and shows its details + logs; hovering a non-selected scene shows
// a summary tooltip.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import type { Scene } from '../lib/daemon'
import { humanErrorMessage, requestDaemon } from '../lib/daemon'
import { formatDate, formatTime } from '../lib/format'
import { useI18n } from '../lib/i18n'
import { TIMELINE } from '../config/app'
import { RPC } from '../config/rpc'
import { useAppState } from '../context/app-state'
import { ClockGlyph, LinkGlyph, TextGlyph, WindowGlyph } from '../components/ui/glyphs'

function rangeLabel(scene: Scene): string {
  const start = `${formatDate(scene.startedAt)} ${formatTime(scene.startedAt)}`
  if (scene.endedAt === null) return start
  return `${start} - ${formatTime(scene.endedAt)}`
}

export function TimelinePage(): ReactElement {
  const { t } = useI18n()
  const { connected } = useAppState()
  const [scenes, setScenes] = useState<Scene[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [hovered, setHovered] = useState<{ scene: Scene; left: number } | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const stripRef = useRef<HTMLDivElement | null>(null)
  const scenesRef = useRef<Scene[] | null>(null)
  const loadingRef = useRef(false)
  const exhaustedRef = useRef(false)
  // Newest-on-the-right means older pages prepend on the LEFT. Before such a
  // load we snapshot the strip width so the layout effect can keep the viewport
  // visually still (otherwise inserting left content jumps the scroll).
  const anchorRef = useRef<{ scrollWidth: number; scrollLeft: number } | null>(null)
  // Pin the view to the newest scene (right edge) until the first user scroll.
  const pinnedToNewestRef = useRef(true)

  // Load the next page of older scenes. The keyset cursor is the oldest loaded
  // started_at, read from a ref so this stays a stable callback.
  const loadPage = useCallback(async (): Promise<void> => {
    if (loadingRef.current || exhaustedRef.current) return
    loadingRef.current = true
    const prev = scenesRef.current
    const before = prev !== null && prev.length > 0 ? prev[prev.length - 1].startedAt : null
    try {
      const response = await requestDaemon(RPC.scene.timeline, {
        limit: TIMELINE.pageSize,
        ...(before !== null ? { before } : {})
      })
      if (response.scenes.length < TIMELINE.pageSize) exhaustedRef.current = true
      setError(null)
      setScenes((current) => {
        const base = before === null || current === null ? [] : current
        const seen = new Set(base.map((scene) => scene.id))
        const next = [...base, ...response.scenes.filter((scene) => !seen.has(scene.id))]
        scenesRef.current = next
        return next
      })
    } catch (requestError) {
      setError(humanErrorMessage(requestError))
    } finally {
      loadingRef.current = false
    }
  }, [])

  useEffect(() => {
    if (!connected) return
    scenesRef.current = null
    exhaustedRef.current = false
    anchorRef.current = null
    pinnedToNewestRef.current = true
    void loadPage()
  }, [connected, loadPage])

  // Keep loading until the strip overflows (becomes scrollable) or runs dry, so
  // a short first page doesn't leave the user with nothing to scroll.
  useEffect(() => {
    const strip = stripRef.current
    if (strip === null || scenes === null) return
    if (
      !exhaustedRef.current &&
      !loadingRef.current &&
      strip.scrollWidth <= strip.clientWidth + TIMELINE.overflowEpsilonPx
    ) {
      void loadPage()
    }
  }, [scenes, loadPage])

  // After scenes change, keep the scroll position sensible: while pinned, hold
  // the newest scene at the right edge; once the user has scrolled, an older
  // page prepended on the left is offset out so the viewport stays put.
  useLayoutEffect(() => {
    const strip = stripRef.current
    if (strip === null || scenes === null) return
    const anchor = anchorRef.current
    if (anchor !== null) {
      strip.scrollLeft = anchor.scrollLeft + (strip.scrollWidth - anchor.scrollWidth)
      anchorRef.current = null
    } else if (pinnedToNewestRef.current) {
      strip.scrollLeft = strip.scrollWidth
    }
  }, [scenes])

  const handleScroll = useCallback((): void => {
    const strip = stripRef.current
    if (strip === null) return
    // Any user scroll releases the newest-pin so loads stop yanking to the right.
    if (strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 4) {
      pinnedToNewestRef.current = false
    }
    // Older scenes are to the left; load more (anchored) when near the left edge.
    if (strip.scrollLeft <= TIMELINE.loadEdgePx && !loadingRef.current && !exhaustedRef.current) {
      anchorRef.current = { scrollWidth: strip.scrollWidth, scrollLeft: strip.scrollLeft }
      void loadPage()
    }
  }, [loadPage])

  const selected = useMemo(() => {
    if (scenes === null || scenes.length === 0) return null
    // Scenes arrive most-recent first, so default to the newest one.
    return scenes.find((scene) => scene.id === selectedId) ?? scenes[0]
  }, [scenes, selectedId])

  // Render oldest → newest so the newest scene sits at the right edge; the data
  // stays newest-first (the load cursor and the default selection rely on it).
  const ordered = useMemo(() => (scenes === null ? [] : [...scenes].reverse()), [scenes])

  if (error !== null) {
    return (
      <div className="home-timeline">
        <p className="caption caption--error timeline-note">{error}</p>
      </div>
    )
  }
  if (scenes === null) {
    return (
      <div className="home-timeline">
        <p className="caption timeline-note">{t('context.loading')}</p>
      </div>
    )
  }
  if (scenes.length === 0) {
    return (
      <div className="home-timeline">
        <p className="caption timeline-note">{t('timeline.empty')}</p>
      </div>
    )
  }

  const showTip = hovered !== null && (selected === null || hovered.scene.id !== selected.id)

  return (
    <div className="home-timeline">
      <div className="timeline" aria-label={t('timeline.aria')}>
        {selected !== null && <SceneDetail scene={selected} t={t} />}

      <div
        className="tl-strip-wrap"
        ref={wrapRef}
        onMouseLeave={() => {
          setHovered(null)
        }}
      >
        <div className="timeline-strip" role="list" ref={stripRef} onScroll={handleScroll}>
          {ordered.map((scene) => {
            const shortCount = Math.min(Math.max(scene.recordCount - 1, 0), TIMELINE.maxShortSticks)
            const isSelected = selected !== null && scene.id === selected.id
            return (
              <button
                key={scene.id}
                type="button"
                role="listitem"
                className={isSelected ? 'tl-scene tl-scene--selected' : 'tl-scene'}
                aria-pressed={isSelected}
                aria-label={scene.label}
                onClick={() => {
                  setSelectedId(scene.id)
                }}
                onMouseEnter={(event) => {
                  const wrap = wrapRef.current
                  if (wrap === null) return
                  const wrapRect = wrap.getBoundingClientRect()
                  const rect = event.currentTarget.getBoundingClientRect()
                  setHovered({ scene, left: rect.left - wrapRect.left + rect.width / 2 })
                }}
              >
                <span className="tl-stick tl-stick--tall" />
                {Array.from({ length: shortCount }, (_, index) => (
                  <span key={index} className="tl-stick tl-stick--short" />
                ))}
              </button>
            )
          })}
        </div>

        {showTip && hovered !== null && (
          <div className="tl-tip" role="tooltip" style={{ left: `${String(hovered.left)}px` }}>
            <span className="tl-tip-time">{rangeLabel(hovered.scene)}</span>
            <span className="tl-tip-label">{hovered.scene.label}</span>
            <span className="tl-tip-shots">
              {t('timeline.shots', { count: hovered.scene.ocrShots })}
            </span>
          </div>
        )}
      </div>
      </div>
    </div>
  )
}

function SceneDetail({
  scene,
  t
}: {
  scene: Scene
  t: (key: string, vars?: Record<string, string | number>) => string
}): ReactElement {
  return (
    <div className="timeline-detail">
      <div className="timeline-shot">
        {scene.image !== null ? (
          <img src={scene.image} alt={scene.label} draggable={false} />
        ) : (
          <span className="timeline-shot--empty">{t('timeline.noShot')}</span>
        )}
      </div>

      <div className="timeline-info">
        <div className="timeline-card">
          <div className="timeline-fact">
            <ClockGlyph />
            <span>{rangeLabel(scene)}</span>
          </div>
          <div className="timeline-fact">
            <WindowGlyph />
            <span>{scene.label}</span>
          </div>
          {scene.file !== null && (
            <div className="timeline-fact">
              <LinkGlyph />
              <span className="timeline-file" title={scene.file}>
                {scene.file}
              </span>
            </div>
          )}
          <div className="timeline-fact">
            <TextGlyph />
            <span>{t('timeline.shots', { count: scene.ocrShots })}</span>
          </div>
        </div>

        <div className="timeline-logs">
          <span className="timeline-logs-title">{t('timeline.logs')}</span>
          <div className="timeline-logs-list">
            {scene.logs.length === 0 ? (
              <p className="caption">{t('timeline.noLogs')}</p>
            ) : (
              scene.logs.map((log, index) => (
                <p key={`${log.timestamp}:${String(index)}`} className="timeline-log">
                  <span className="timeline-log-ts">{log.timestamp}</span>{' '}
                  <span className="timeline-log-type">{log.type}</span>
                  {(log.app !== null || log.window !== null) && (
                    <span className="timeline-log-ctx">
                      {' | '}
                      {[log.app, log.window].filter((v) => v !== null && v !== '').join(' | ')}
                    </span>
                  )}
                  {log.url !== null && log.url !== '' && (
                    <span className="timeline-log-url"> {log.url}</span>
                  )}
                </p>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
