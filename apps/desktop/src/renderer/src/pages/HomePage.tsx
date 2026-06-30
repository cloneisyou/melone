// Idle home body: until the user connects an agent, show the "Use it in Claude
// Code / Codex" setup section; once an integration is enabled, show the
// recent-scene carousel. Plus a seed-demo button on a non-collecting device
// with an empty DB.
import type { ReactElement } from 'react'
import { HomePreviews } from './HomePreviews'
import { HomeSetup } from './HomeSetup'
import { RPC } from '../config/rpc'
import { useAppState } from '../context/app-state'
import { requestDaemon } from '../lib/daemon'
import { useI18n } from '../lib/i18n'
import { useDaemonAction } from '../lib/use-daemon-action'

export function HomePage(): ReactElement {
  const { t } = useI18n()
  const { previews, loaded, errors, connected, collectorsSupported, hasAnyData, mcpStatus, refresh } =
    useAppState()
  const seed = useDaemonAction()
  const canSeed = loaded && collectorsSupported === false && !hasAnyData

  // Setup section while no integration is connected; carousel once one is.
  // mcpStatus null = not loaded yet → default to the carousel (avoids a flash).
  const anyEnabled =
    mcpStatus !== null &&
    (mcpStatus.claudeCode.enabled === true || mcpStatus.codex.enabled === true)
  const showSetup = mcpStatus !== null && !anyEnabled

  const handleSeed = (): Promise<void> =>
    seed.run(async () => {
      await requestDaemon(RPC.events.seedDemo)
      refresh()
    })

  return (
    <>
      {showSetup ? (
        <HomeSetup />
      ) : (
        <HomePreviews previews={previews} loaded={loaded} error={errors.previews} />
      )}
      {canSeed && (
        <div className="home-grow">
          <button
            type="button"
            className="button"
            disabled={seed.pending || !connected}
            onClick={() => {
              void handleSeed()
            }}
          >
            {t('home.seedDemo')}
          </button>
          {seed.error !== null && <p className="caption caption--error">{seed.error}</p>}
        </div>
      )}
    </>
  )
}
