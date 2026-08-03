import { useEffect, useState } from 'react'

import { api, type ChatMessage, type Lang, type RateItem, type ThemeName } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Notice, Option, Row, Rows, Section, Skeleton, Toggle } from '../components/ui'
import { formatDiff, formatTime } from '../format'
import { LANGUAGE_NAMES } from '../i18n'
import { useApp, useNav, useT } from '../store'
import { confirmAction, haptic, openExternal } from '../telegram'

const APP_VERSION = '0.1.0'

/** 23 — exchange rates. */
export function RatesScreen() {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const [data, setData] = useState<Awaited<ReturnType<typeof api.rates>> | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .rates()
      .then((result) => !cancelled && setData(result))
      .catch(() => !cancelled && setData({ items: [], updated_at: '', coming_soon: [] }))
    return () => {
      cancelled = true
    }
  }, [lang])

  if (!data) {
    return (
      <Screen title={t('rates.title')}>
        <Skeleton lines={6} />
      </Screen>
    )
  }

  const visible = expanded ? data.items : data.items.slice(0, 6)
  const hidden = data.items.length - visible.length

  return (
    <Screen title={t('rates.title')} secondaryBg>
      <Rows>
        {visible.map((item) => (
          <RateRow key={item.code} item={item} />
        ))}
      </Rows>

      {hidden > 0 ? (
        <button className="link t-center" onClick={() => setExpanded(true)}>
          {t('rates.showAll', { n: hidden })}
        </button>
      ) : null}

      {data.coming_soon.length ? (
        <Section title={t('rates.comingSoon')}>
          <div style={{ opacity: 0.75 }}>
            <Rows>
              {data.coming_soon.map((item) => (
                <Row
                  key={item.key}
                  title={item.label}
                  right={<span className="badge">{t('common.soon')}</span>}
                />
              ))}
            </Rows>
          </div>
        </Section>
      ) : null}

      {data.updated_at ? (
        <div className="t-cap t-center">{t('rates.updated', { date: data.updated_at })}</div>
      ) : null}
    </Screen>
  )
}

function RateRow({ item }: { item: RateItem }) {
  const tone = item.diff > 0 ? 'var(--ok)' : item.diff < 0 ? 'var(--danger)' : 'var(--tg-hint)'
  return (
    <div className="row">
      <span
        style={{
          width: 34,
          height: 24,
          borderRadius: 4,
          background: 'var(--tg-secondary-bg)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 11,
          fontWeight: 600,
          flex: 'none',
        }}
      >
        {item.code.slice(0, 2)}
      </span>
      <span className="row__main">
        <span className="row__title" style={{ fontSize: 15, fontWeight: 600 }}>
          {item.code}
        </span>
        <span className="row__sub">{item.name}</span>
      </span>
      <span style={{ textAlign: 'right' }}>
        <span className="num" style={{ display: 'block', fontSize: 16, fontWeight: 600 }}>
          {item.rate_text}
        </span>
        <span className="num" style={{ fontSize: 12, fontWeight: 500, color: tone }}>
          {item.diff > 0 ? '▲' : item.diff < 0 ? '▼' : '='} {formatDiff(item.diff)}
        </span>
      </span>
    </div>
  )
}

/** 24 — useful links and contacts. */
export function LinksScreen() {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const [data, setData] = useState<Awaited<ReturnType<typeof api.links>> | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .links()
      .then((result) => !cancelled && setData(result))
      .catch(() => !cancelled && setData(null))
    return () => {
      cancelled = true
    }
  }, [lang])

  if (!data) {
    return (
      <Screen title={t('links.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  return (
    <Screen title={t('links.title')} secondaryBg>
      <Section title={t('links.app')}>
        <div className="duo">
          {data.apps.map((app) => (
            <button key={app.key} className="card card--tap" onClick={() => openExternal(app.url)}>
              <span className="t-row" style={{ fontWeight: 600 }}>
                {app.label}
              </span>
            </button>
          ))}
        </div>
      </Section>

      <Section title={t('links.social')}>
        <Rows>
          {data.socials.map((social) => (
            <Row
              key={social.key}
              title={social.label}
              onClick={() => openExternal(social.url)}
              right={
                <span className="t-hint" style={{ display: 'flex' }}>
                  <Icon name="external" size={18} />
                </span>
              }
            />
          ))}
        </Rows>
      </Section>

      <Section title={t('links.contacts')}>
        <div className="card">
          <button
            style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', width: '100%' }}
            onClick={() => openExternal(`tel:${data.contacts.call_center}`)}
          >
            <span className="t-sub">{t('links.callCenter')}</span>
            <span className="num t-brand" style={{ fontSize: 18, fontWeight: 700 }}>
              {data.contacts.call_center}
            </span>
          </button>
          <div className="divider" />
          <button
            style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', width: '100%' }}
            onClick={() => openExternal(`tel:${data.contacts.trust_line.replace(/\s/g, '')}`)}
          >
            <span className="t-sub">{t('links.trustLine')}</span>
            <span className="num" style={{ fontSize: 15, fontWeight: 600 }}>
              {data.contacts.trust_line}
            </span>
          </button>
        </div>
      </Section>
    </Screen>
  )
}

/** 25 — settings, with the appearance block from iteration 2. */
export function SettingsScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const reset = useNav((s) => s.reset)
  const { data, lang, setLang, setTheme } = useApp()
  const [notifications, setNotifications] = useState(true)
  const [pickingLang, setPickingLang] = useState(false)
  const [sessions, setSessions] = useState<{ active: number; archive: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .sessions()
      .then((result) => {
        if (cancelled) return
        setSessions({
          active: result.counts.active,
          archive: result.counts.ended + result.counts.expired,
        })
      })
      .catch(() => !cancelled && setSessions(null))
    return () => {
      cancelled = true
    }
  }, [])

  const endSession = async () => {
    const confirmed = await confirmAction(t('settings.endConfirm'))
    if (!confirmed) return
    await api.endSession()
    haptic.success()
    reset('home')
  }

  const theme = data?.user.theme ?? 'auto'

  return (
    <Screen title={t('settings.title')} secondaryBg>
      <Section title={t('settings.appearance')}>
        <div className="card">
          <div className="t-title" style={{ fontSize: 15 }}>
            {t('settings.theme')}
          </div>
          <div className="theme-grid">
            {(['light', 'dark', 'auto'] as ThemeName[]).map((option) => (
              <button
                key={option}
                className={`theme-option${theme === option ? ' theme-option--active' : ''}`}
                onClick={() => {
                  haptic.select()
                  void setTheme(option)
                }}
              >
                <ThemePreview variant={option} />
                <span className="theme-option__label">
                  {t(
                    option === 'light'
                      ? 'settings.themeLight'
                      : option === 'dark'
                        ? 'settings.themeDark'
                        : 'settings.themeAuto',
                  )}
                </span>
              </button>
            ))}
          </div>
          <div className="t-cap">{t('settings.themeNote')}</div>
        </div>
      </Section>

      <Section title={t('settings.profile')}>
        <Rows>
          <Row
            title={t('settings.language')}
            value={LANGUAGE_NAMES[lang]}
            chevron
            onClick={() => setPickingLang((value) => !value)}
          />
          <Row
            title={t('settings.phone')}
            value={data?.user.phone_masked || t('settings.phoneEmpty')}
            chevron
            onClick={() => push('phone')}
          />
          <Row
            title={t('settings.notifications')}
            right={<Toggle on={notifications} onChange={setNotifications} />}
          />
          <Row
            title={t('sessions.title')}
            value={
              sessions
                ? t('settings.sessionsValue', { active: sessions.active, archive: sessions.archive })
                : ''
            }
            chevron
            onClick={() => push('sessions')}
          />
        </Rows>
      </Section>

      {pickingLang ? (
        <div className="stack stack--tight">
          {(data?.langs ?? (['ru', 'uz', 'en'] as Lang[])).map((code) => (
            <Option
              key={code}
              label={LANGUAGE_NAMES[code] ?? code}
              selected={lang === code}
              onClick={() => {
                void setLang(code)
                setPickingLang(false)
              }}
            />
          ))}
        </div>
      ) : null}

      <Rows>
        <Row danger title={t('settings.endSession')} onClick={() => void endSession()} />
      </Rows>

      {data?.dev_mode ? <Notice tone="warn">MINIAPP_DEV_MODE</Notice> : null}

      <div className="t-cap t-center">
        {t('settings.version', { version: APP_VERSION })}
        <br />
        {t('settings.footer', { hours: 24, days: 30 })}
      </div>
    </Screen>
  )
}

/** Miniature of what the theme does to a screen — light, dark, or split. */
function ThemePreview({ variant }: { variant: ThemeName }) {
  if (variant === 'auto') {
    return (
      <span className="theme-preview" style={{ padding: 0, flexDirection: 'row' }}>
        <span style={{ flex: 1, background: '#fff', borderRight: '1px solid #e6e8eb' }} />
        <span style={{ flex: 1, background: '#17212b' }} />
      </span>
    )
  }
  const dark = variant === 'dark'
  return (
    <span
      className="theme-preview"
      style={{
        background: dark ? '#17212b' : '#fff',
        border: dark ? 'none' : '1px solid #e6e8eb',
      }}
    >
      <span className="theme-preview__line" style={{ width: '70%', background: dark ? '#4a5866' : '#c9ced4' }} />
      <span className="theme-preview__line" style={{ width: '45%', background: dark ? '#2c3a47' : '#e2e5e9' }} />
      <span className="theme-preview__btn" style={{ background: dark ? '#F2323D' : '#D70C17' }} />
    </span>
  )
}

/** 25b — session history timeline. */
export function HistoryScreen() {
  const t = useT()
  const [items, setItems] = useState<ChatMessage[] | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .sessionHistory()
      .then((data) => !cancelled && setItems(data.items))
      .catch(() => !cancelled && setItems([]))
    return () => {
      cancelled = true
    }
  }, [])

  if (items === null) {
    return (
      <Screen title={t('history.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  if (!items.length) {
    return (
      <Screen title={t('history.title')}>
        <Notice tone="info">{t('history.empty')}</Notice>
      </Screen>
    )
  }

  return (
    <Screen title={t('history.title')} secondaryBg>
      <div className="card">
        {items.map((item, index) => (
          <div key={item.id} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '6px 0' }}>
            <span
              className={`timeline-dot${index === items.length - 1 ? ' timeline-dot--current' : ''}`}
              style={{ marginTop: 6 }}
            />
            <span style={{ flex: 1 }}>
              <span className="t-row" style={{ fontWeight: 500, display: 'block' }}>
                {item.text.slice(0, 120)}
              </span>
              <span className="t-cap num">
                {item.created_at ? formatTime(Date.parse(item.created_at)) : ''}
              </span>
            </span>
          </div>
        ))}
      </div>
    </Screen>
  )
}
