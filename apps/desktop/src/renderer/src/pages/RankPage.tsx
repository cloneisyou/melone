// Rank page: two bordered cards (Top URI / Top App) with numbered rows showing
// a kind glyph, label, visit count and PageRank score. Icons are kind glyphs
// for now (real app icons/favicons are a later backend task).
import type { ReactElement } from 'react'
import { RANK } from '../config/app'
import { useAppState } from '../context/app-state'
import type { RankedContext } from '../lib/daemon'
import { useI18n } from '../lib/i18n'
import { KindGlyph } from '../components/ui/glyphs'

interface RankCardProps {
  title: string
  rows: RankedContext[]
  loaded: boolean
}

function RankCard({ title, rows, loaded }: RankCardProps): ReactElement {
  const { t } = useI18n()
  return (
    <section className="rank-card">
      <header className="rank-card-head">
        <span className="rank-card-title">{title}</span>
        <span className="rank-card-window">▾ {t('rank.window')}</span>
      </header>
      {rows.length === 0 ? (
        <p className="caption">{loaded ? t('memory.rankEmpty') : t('context.loading')}</p>
      ) : (
        <ol className="rank-rows">
          {rows.map((entry, index) => (
            <li key={`${entry.kind}:${entry.label}:${String(index)}`} className="rank-row">
              <span className="rank-num">{index + 1}</span>
              <span className="rank-icon">
                <KindGlyph kind={entry.kind} />
              </span>
              <span className="rank-label" title={entry.label}>
                {entry.label}
              </span>
              <span className="rank-visits">{t('context.visits', { count: entry.visits })}</span>
              <span className="rank-score">{entry.score.toFixed(5)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export function RankPage(): ReactElement {
  const { t } = useI18n()
  const { rank, loaded, errors } = useAppState()

  const rows = rank ?? []
  const uriRows = rows.filter((entry) => entry.kind === 'url').slice(0, RANK.tableLimit)
  const appRows = rows
    .filter((entry) => entry.kind === 'app' || entry.kind === 'app_window')
    .slice(0, RANK.tableLimit)

  return (
    <div className="home-rank">
      <div className="rank" aria-label={t('memory.rankAria')}>
        <RankCard title={t('memory.uriTitle')} rows={uriRows} loaded={loaded} />
        <RankCard title={t('memory.appTitle')} rows={appRows} loaded={loaded} />
        {errors.rank !== null && <p className="caption caption--error">{errors.rank}</p>}
      </div>
    </div>
  )
}
