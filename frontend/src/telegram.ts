/**
 * Thin typed wrapper over `window.Telegram.WebApp`.
 *
 * Everything degrades gracefully: opened in a plain browser (local development)
 * `isTelegram` is false, native buttons are absent and the UI renders its own
 * fallback chrome instead. No behaviour is gated on the SDK being present.
 */

type ThemeParams = Record<string, string | undefined>

interface Inset {
  top: number
  bottom: number
  left: number
  right: number
}

interface TelegramWebApp {
  initData: string
  initDataUnsafe: { user?: { id: number; first_name?: string; language_code?: string } }
  version: string
  colorScheme: 'light' | 'dark'
  themeParams: ThemeParams
  viewportStableHeight: number
  viewportHeight: number
  isExpanded: boolean
  /** Device notch / home indicator (Bot API 8.0+). */
  safeAreaInset?: Inset
  /** Space taken by the client's own header and buttons (Bot API 8.0+). */
  contentSafeAreaInset?: Inset
  ready(): void
  expand(): void
  close(): void
  onEvent(event: string, handler: () => void): void
  offEvent(event: string, handler: () => void): void
  openLink(url: string, options?: { try_instant_view?: boolean }): void
  openTelegramLink(url: string): void
  showConfirm(message: string, callback: (ok: boolean) => void): void
  requestContact?(callback: (shared: boolean, result?: unknown) => void): void
  setHeaderColor?(color: string): void
  setBackgroundColor?(color: string): void
  disableVerticalSwipes?(): void
  MainButton: {
    text: string
    isVisible: boolean
    isActive: boolean
    show(): void
    hide(): void
    enable(): void
    disable(): void
    showProgress(leaveActive?: boolean): void
    hideProgress(): void
    setParams(params: { text?: string; color?: string; text_color?: string; is_active?: boolean; is_visible?: boolean }): void
    onClick(cb: () => void): void
    offClick(cb: () => void): void
  }
  SecondaryButton?: TelegramWebApp['MainButton'] & {
    setParams(params: { text?: string; position?: string; is_active?: boolean; is_visible?: boolean }): void
  }
  BackButton: {
    isVisible: boolean
    show(): void
    hide(): void
    onClick(cb: () => void): void
    offClick(cb: () => void): void
  }
  HapticFeedback: {
    impactOccurred(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft'): void
    notificationOccurred(type: 'error' | 'success' | 'warning'): void
    selectionChanged(): void
  }
  CloudStorage?: {
    setItem(key: string, value: string, cb?: (err: string | null, ok?: boolean) => void): void
    getItem(key: string, cb: (err: string | null, value?: string) => void): void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export const webApp: TelegramWebApp | null = window.Telegram?.WebApp ?? null

/** True only when actually launched from a Telegram client. */
export const isTelegram = Boolean(webApp && webApp.initData)

export const rawInitData = webApp?.initData ?? ''

/**
 * User's appearance choice: 'auto' follows the Telegram client (or the OS in a
 * browser), 'light' / 'dark' pin it. Stored server-side, applied here.
 */
let storedTheme: 'auto' | 'light' | 'dark' = 'auto'

/** themeParams key → CSS variable. Brand tokens are ours and never overridden. */
const THEME_PARAM_VARS: Record<string, string> = {
  bg_color: '--tg-bg',
  secondary_bg_color: '--tg-secondary-bg',
  section_bg_color: '--tg-section-bg',
  text_color: '--tg-text',
  hint_color: '--tg-hint',
  subtitle_text_color: '--tg-sub',
  link_color: '--tg-link',
  section_separator_color: '--tg-sep',
  header_bg_color: '--tg-header',
}

export function applyStoredTheme(theme: 'auto' | 'light' | 'dark'): void {
  storedTheme = theme
  applyTheme()
}

/** Telegram theme params → CSS variables, plus the light/dark switch. */
function applyTheme(): void {
  const root = document.documentElement
  const clientScheme =
    webApp?.colorScheme ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  const scheme = storedTheme === 'auto' ? clientScheme : storedTheme
  root.dataset.theme = scheme

  // A pinned theme keeps the design tokens as-is; only 'auto' adopts the
  // client palette, otherwise a dark Telegram would bleed into a light app.
  if (storedTheme !== 'auto') {
    for (const cssVar of Object.values(THEME_PARAM_VARS)) root.style.removeProperty(cssVar)
    return
  }
  const params = webApp?.themeParams
  if (!params) return
  for (const [key, cssVar] of Object.entries(THEME_PARAM_VARS)) {
    const value = params[key]
    if (value) root.style.setProperty(cssVar, value)
  }
}

/**
 * Recent clients draw the close button, the title and the "…" menu *over* the
 * web view, so an unpadded page starts underneath them. `safeAreaInset` is the
 * device notch, `contentSafeAreaInset` the client chrome — the padding to keep
 * clear is their sum. Older clients expose neither and keep their header
 * outside the view, so the `env()` floor in tokens.css stays in charge.
 */
function applyInsets(): void {
  const device = webApp?.safeAreaInset
  const content = webApp?.contentSafeAreaInset
  if (!device && !content) return
  const root = document.documentElement
  root.style.setProperty('--tg-inset-top', `${(device?.top ?? 0) + (content?.top ?? 0)}px`)
  root.style.setProperty('--tg-inset-bottom', `${(device?.bottom ?? 0) + (content?.bottom ?? 0)}px`)
}

/** Keeps `--viewport-height` in sync so the chat survives the keyboard. */
function applyViewport(): void {
  const height = webApp?.viewportStableHeight
  document.documentElement.style.setProperty(
    '--viewport-height',
    height ? `${height}px` : '100dvh',
  )
}

export function initTelegram(): void {
  applyTheme()
  applyViewport()
  applyInsets()
  if (!webApp) {
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', applyTheme)
    return
  }
  webApp.ready()
  webApp.expand()
  webApp.disableVerticalSwipes?.()
  webApp.onEvent('themeChanged', applyTheme)
  webApp.onEvent('viewportChanged', () => {
    applyViewport()
    applyInsets()
  })
  webApp.onEvent('safeAreaChanged', applyInsets)
  webApp.onEvent('contentSafeAreaChanged', applyInsets)
}

/* ── Native controls ─────────────────────────────────────────────────── */

export const haptic = {
  select(): void {
    webApp?.HapticFeedback.selectionChanged()
  },
  tap(): void {
    webApp?.HapticFeedback.impactOccurred('light')
  },
  success(): void {
    webApp?.HapticFeedback.notificationOccurred('success')
  },
  error(): void {
    webApp?.HapticFeedback.notificationOccurred('error')
  },
}

export function openExternal(url: string): void {
  if (webApp) {
    if (url.startsWith('https://t.me/')) webApp.openTelegramLink(url)
    else webApp.openLink(url)
    return
  }
  window.open(url, '_blank', 'noopener')
}

export function confirmAction(message: string): Promise<boolean> {
  if (!webApp) return Promise.resolve(window.confirm(message))
  return new Promise((resolve) => webApp.showConfirm(message, resolve))
}

/** Native contact request (Bot API 6.9+). Resolves false when unavailable. */
export function requestContact(): Promise<boolean> {
  if (!webApp?.requestContact) return Promise.resolve(false)
  return new Promise((resolve) => webApp.requestContact!((shared) => resolve(shared)))
}

export function closeApp(): void {
  webApp?.close()
}
