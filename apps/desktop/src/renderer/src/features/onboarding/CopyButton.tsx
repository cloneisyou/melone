// Small copy-to-clipboard button used for example prompts and config snippets.
// Shows a transient "Copied" label, then reverts. Self-contained so any
// onboarding surface can drop it next to copyable text.
import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import { useI18n } from '../../lib/i18n'

interface CopyButtonProps {
  /** The text written to the clipboard. */
  text: string
  /** Accessible label override (defaults to the generic "Copy"). */
  label?: string
}

export function CopyButton({ text, label }: CopyButtonProps): ReactElement {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (timer.current !== null) clearTimeout(timer.current)
    }
  }, [])

  const handleCopy = (): void => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      if (timer.current !== null) clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        setCopied(false)
      }, 1500)
    })
  }

  return (
    <button
      type="button"
      className="copy-button"
      aria-label={label ?? t('onboarding.copy')}
      onClick={handleCopy}
    >
      {copied ? t('onboarding.copied') : t('onboarding.copy')}
    </button>
  )
}
