import { useEffect, useRef, type ReactNode } from 'react'

import { haptic, isTelegram, webApp } from '../telegram'
import { Icon } from './Icon'

export interface ButtonSpec {
  text: string
  onClick: () => void
  disabled?: boolean
  loading?: boolean
  /** Shown under the button when disabled — e.g. "Заполните сумму". */
  hint?: string
}

interface ScreenProps {
  children: ReactNode
  /** Primary action — native MainButton inside Telegram, sticky bar outside. */
  main?: ButtonSpec
  /** Optional second action rendered to the left of the primary one. */
  secondary?: ButtonSpec
  /** Screen title used by the browser fallback header. */
  title?: string
  subtitle?: string
  /** Secondary background (Home and other grouped-list screens). */
  secondaryBg?: boolean
  /** Removes body padding (chat). */
  flush?: boolean
  /** Wider padding for text-heavy screens. */
  text?: boolean
  bodyRef?: React.Ref<HTMLDivElement>
}

/**
 * MainButton wiring.
 *
 * Inside Telegram the native button is the single source of truth; the DOM bar
 * is not rendered at all. Outside Telegram (local browser testing) the DOM bar
 * takes over so every flow stays clickable.
 */
function useNativeMainButton(spec: ButtonSpec | undefined) {
  const handlerRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    const button = webApp?.MainButton
    if (!isTelegram || !button) return

    if (!spec) {
      button.hide()
      return
    }

    button.setParams({ text: spec.text, is_visible: true })
    if (spec.disabled) button.disable()
    else button.enable()
    if (spec.loading) button.showProgress(false)
    else button.hideProgress()

    const handler = () => {
      if (spec.disabled || spec.loading) return
      haptic.tap()
      spec.onClick()
    }
    handlerRef.current = handler
    button.onClick(handler)
    return () => {
      button.offClick(handler)
      handlerRef.current = null
    }
  }, [spec?.text, spec?.disabled, spec?.loading, spec?.onClick, spec])

  useEffect(() => {
    return () => {
      if (isTelegram) webApp?.MainButton.hide()
    }
  }, [])
}

export function Screen({
  children,
  main,
  secondary,
  title,
  subtitle,
  secondaryBg,
  flush,
  text,
  bodyRef,
}: ScreenProps) {
  useNativeMainButton(main)

  const showFallbackBar = !isTelegram && (main || secondary)

  return (
    <div className={`screen${secondaryBg ? ' screen--secondary' : ''}`}>
      {!isTelegram && title ? <FallbackHeader title={title} subtitle={subtitle} /> : null}
      <div
        ref={bodyRef}
        className={`screen__body${flush ? ' screen__body--flush' : ''}${
          text ? ' screen__body--text' : ''
        }`}
      >
        {children}
      </div>
      {showFallbackBar ? (
        <div>
          <div className="mainbtn-bar">
            {secondary ? (
              <button
                className="mainbtn mainbtn--secondary"
                onClick={secondary.onClick}
                disabled={secondary.disabled || secondary.loading}
              >
                {secondary.loading ? <span className="spinner" /> : null}
                {secondary.text}
              </button>
            ) : null}
            {main ? (
              <button
                className="mainbtn"
                onClick={() => {
                  haptic.tap()
                  main.onClick()
                }}
                disabled={main.disabled || main.loading}
              >
                {main.loading ? <span className="spinner" /> : null}
                {main.text}
              </button>
            ) : null}
          </div>
          {main?.disabled && main.hint ? <span className="mainbtn__hint">{main.hint}</span> : null}
        </div>
      ) : null}
    </div>
  )
}

function FallbackHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="fallback-header">
      <BackArrow />
      <div>
        <div className="fallback-header__title">{title}</div>
        {subtitle ? <div className="fallback-header__sub">{subtitle}</div> : null}
      </div>
      <div className="fallback-header__spacer" />
    </div>
  )
}

/** Browser-only back affordance; inside Telegram the native BackButton is used. */
function BackArrow() {
  return (
    <button
      aria-label="back"
      onClick={() => window.dispatchEvent(new CustomEvent('miniapp:back'))}
      style={{ color: 'var(--tg-hint)', display: 'flex' }}
    >
      <Icon name="back" size={20} />
    </button>
  )
}
