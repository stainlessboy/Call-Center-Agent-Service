import { useEffect, useState } from 'react'

import { api, type CatalogGroup, type Lang, type RateItem } from '../api'
import { CATEGORY_ICONS, Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Skeleton } from '../components/ui'
import { formatDiff } from '../format'
import { LANGUAGE_NAMES } from '../i18n'
import { useApp, useNav, useT } from '../store'
import { haptic } from '../telegram'

/**
 * 2a — search-first Home.
 *
 * The chat entry is a search field rather than a banner, so the only red on
 * the screen is its send button and the product icons. MainButton is unused
 * here: there is no single action to promote.
 */
export function HomeScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const user = useApp((s) => s.data?.user)
  const operator = useApp((s) => s.data?.operator)
  const langs = useApp((s) => s.data?.langs ?? (['ru', 'uz', 'en'] as Lang[]))
  const lang = useApp((s) => s.lang)
  const setLang = useApp((s) => s.setLang)

  const [groups, setGroups] = useState<CatalogGroup[] | null>(null)
  const [rates, setRates] = useState<RateItem[] | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .catalog()
      .then((data) => !cancelled && setGroups(data.groups))
      .catch(() => !cancelled && setGroups([]))
    api
      .rates(3)
      .then((data) => !cancelled && setRates(data.items))
      .catch(() => !cancelled && setRates([]))
    return () => {
      cancelled = true
    }
  }, [lang])

  const tiles = (groups ?? []).flatMap((group) => group.categories).slice(0, 4)
  const openChat = () => {
    haptic.tap()
    push('chat')
  }

  return (
    <Screen title="Asaka AI">
      <div className="home-head">
        <span className="home-logo">A</span>
        <span className="t-row" style={{ flex: 1, fontWeight: 600, letterSpacing: '-0.01em' }}>
          Asaka AI
        </span>
        <div className="langswitch">
          {langs.map((code) => (
            <button
              key={code}
              className={`langswitch__item${code === lang ? ' langswitch__item--active' : ''}`}
              onClick={() => {
                haptic.select()
                void setLang(code)
              }}
              aria-label={LANGUAGE_NAMES[code]}
            >
              {code.toUpperCase()}
            </button>
          ))}
        </div>
        <button
          className="icon-btn"
          aria-label={t('settings.title')}
          onClick={() => {
            haptic.tap()
            push('settings')
          }}
        >
          <Icon name="settings" size={18} />
        </button>
      </div>

      <div style={{ paddingTop: 6 }}>
        <div className="t-h1">{t('home.greeting2', { name: user?.first_name || '' })}</div>
        <div className="t-cap" style={{ marginTop: 8 }}>
          {t('home.subtitle2')}
        </div>
      </div>

      <button className="home-search" onClick={openChat}>
        <Icon name="search" size={19} />
        <span className="home-search__placeholder">{t('home.searchPlaceholder')}</span>
        <span className="home-search__send">
          <Icon name="send" size={18} />
        </span>
      </button>

      <div style={{ display: 'flex', alignItems: 'baseline' }}>
        <span className="t-section" style={{ flex: 1 }}>
          {t('home.products')}
        </span>
        <button
          className="t-row t-hint"
          style={{ fontWeight: 500 }}
          onClick={() => {
            haptic.tap()
            push('catalog')
          }}
        >
          {t('home.allProducts', { n: 7 })} →
        </button>
      </div>

      {groups === null ? (
        <Skeleton lines={2} />
      ) : (
        <div className="tiles">
          {tiles.map((tile) => (
            <button
              key={tile.category}
              className="tile"
              onClick={() => {
                haptic.tap()
                push(tile.has_qualify ? 'qualify' : 'results', { category: tile.category })
              }}
            >
              <span className="tile__icon">
                <Icon name={CATEGORY_ICONS[tile.category] ?? 'card'} size={21} />
              </span>
              <span>
                <span className="tile__title" style={{ display: 'block' }}>
                  {tile.title}
                </span>
                <span className="tile__sub" style={{ display: 'block', marginTop: 4 }}>
                  {tile.subtitle}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="panel">
        <div style={{ display: 'flex', alignItems: 'baseline' }}>
          <span className="t-section" style={{ flex: 1 }}>
            {t('home.rates')}
          </span>
          <button
            className="t-row t-hint"
            style={{ fontWeight: 500 }}
            onClick={() => {
              haptic.tap()
              push('rates')
            }}
          >
            →
          </button>
        </div>
        {rates === null ? (
          <Skeleton lines={1} />
        ) : (
          <div className="rate-widget">
            {rates.map((rate) => (
              <div key={rate.code} className="rate-widget__cell">
                <span className="rate-widget__code">{rate.code}</span>
                <span className="rate-widget__value" style={{ fontSize: 19, letterSpacing: '-0.02em' }}>
                  {rate.rate_text}
                </span>
                <span
                  className="rate-widget__diff"
                  style={{
                    color:
                      rate.diff > 0 ? 'var(--ok)' : rate.diff < 0 ? 'var(--danger)' : 'var(--tg-hint)',
                  }}
                >
                  {formatDiff(rate.diff)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="duo" style={{ marginTop: 'auto', paddingTop: 4 }}>
        <button
          className="card card--tap card--flat"
          style={{ borderRadius: 18, flexDirection: 'row', alignItems: 'center', gap: 10 }}
          onClick={() => {
            haptic.tap()
            push('branches')
          }}
        >
          <Icon name="pin" size={18} />
          <span className="t-row" style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>
            {t('home.branches')}
          </span>
        </button>
        <button
          className="card card--tap card--flat"
          style={{ borderRadius: 18, flexDirection: 'row', alignItems: 'center', gap: 10 }}
          onClick={() => {
            haptic.tap()
            push('chat', { requestOperator: true })
          }}
        >
          <span className="dot" style={{ background: 'var(--ok)' }} />
          <span>
            <span className="t-row" style={{ display: 'block', fontWeight: 600, letterSpacing: '-0.01em' }}>
              {t('home.operator')}
            </span>
            <span className="t-cap num" style={{ display: 'block', marginTop: 2 }}>
              {t('home.operatorUntil', { to: operator?.end_hour ?? 23 })}
            </span>
          </span>
        </button>
      </div>
    </Screen>
  )
}
