// The example prompts shown in the home setup section and the wizard's "Try it"
// step. One list (by i18n key) so both surfaces stay in sync. Each prompt is
// copyable so the user can paste it straight into Claude Code / Codex.
import type { ReactElement } from 'react'
import { useI18n } from '../../lib/i18n'
import { CopyButton } from './CopyButton'

// Prompts live in the locale files (translatable, en/kr parity enforced).
export const EXAMPLE_PROMPT_KEYS = [
  'onboarding.example1',
  'onboarding.example2',
  'onboarding.example3',
  'onboarding.example4'
] as const

export function ExamplePrompts(): ReactElement {
  const { t } = useI18n()
  return (
    <div className="onboarding-examples-wrap">
      <p className="onboarding-skill-hint">{t('onboarding.skillHint')}</p>
      <ul className="onboarding-examples" aria-label={t('onboarding.examplesTitle')}>
        {EXAMPLE_PROMPT_KEYS.map((key) => {
          const prompt = t(key)
          return (
            <li key={key} className="onboarding-example">
              <span className="onboarding-example-text">“{prompt}”</span>
              <CopyButton text={prompt} label={t('onboarding.copyPrompt')} />
            </li>
          )
        })}
      </ul>
    </div>
  )
}
