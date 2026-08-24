import { Fragment, useEffect, useMemo, useState } from 'react'

import { api, ApiError, type SessionDetail, type SessionState, type SessionSummary } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { UiBlocksView } from '../components/UiBlocks'
import { Notice, Segments, Skeleton } from '../components/ui'
import { formatDay, formatTime } from '../format'
import { useApp, useChat, useNav, useT, type ScreenEntry } from '../store'
import { haptic } from '../telegram'
import { sanitizeUiBlocks } from '../uiBlocks'

type Filter = 'all' | 'active' | 'expired'

/** "истечёт через 21 ч" / "через 40 мин" — coarse on purpose. */
function untilLabel(iso: string | null): string {
  if (!iso) return ''
  const diff = Date.parse(iso) - Date.now()
  if (Number.isNaN(diff) || diff <= 0) return ''
  const hours = Math.floor(diff / 3_600_000)
  if (hours >= 1) return `${hours} ч`
  return `${Math.max(1, Math.round(diff / 60_000))} мин`
}

function dayLabel(iso: string | null, todayLabel: string, lang: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  const today = new Date()
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate()
  if (sameDay) return todayLabel
  return formatDay(date.getTime(), lang)
}

const STATE_TONE: Record<SessionState, string> = {
  active: 'var(--ok)',
  ended: 'var(--tg-hint)',
  expired: 'var(--tg-hint)',
}

/** 26 — session list grouped by day, with the three lifecycle states. */
export function SessionsScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const reset = useNav((s) => s.reset)
  const lang = useApp((s) => s.lang)
  const [data, setData] = useState<Awaited<ReturnType<typeof api.sessions>> | null>(null)
  const [filter, setFilter] = useState<Filter>('all')

  useEffect(() => {
    let cancelled = false
    api
      .sessions()
      .then((result) => !cancelled && setData(result))
      .catch(() => !cancelled && setData({ items: [], archive_days: 30, ttl_minutes: 1440, counts: { active: 0, ended: 0, expired: 0 } }))
    return () => {
      cancelled = true
    }
  }, [lang])

  const groups = useMemo(() => {
    if (!data) return []
    const visible = data.items.filter((item) =>
      filter === 'all' ? true : filter === 'active' ? item.state === 'active' : item.state === 'expired',
    )
    const byDay = new Map<string, SessionSummary[]>()
    for (const item of visible) {
      const key = dayLabel(item.started_at, t('sessions.today'), lang)
      byDay.set(key, [...(byDay.get(key) ?? []), item])
    }
    return [...byDay.entries()]
  }, [data, filter, lang, t])

  if (!data) {
    return (
      <Screen title={t('sessions.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  return (
    <Screen title={t('sessions.title')} secondaryBg>
      <Segments
        items={[
          { value: 'all' as Filter, label: t('sessions.all') },
          { value: 'active' as Filter, label: t('sessions.active') },
          { value: 'expired' as Filter, label: t('sessions.expired') },
        ]}
        value={filter}
        onChange={setFilter}
      />

      {groups.length === 0 ? <Notice tone="info">{t('sessions.empty')}</Notice> : null}

      {groups.map(([day, items]) => (
        <div key={day} className="stack stack--tight">
          <div className="t-section">{day}</div>
          {items.map((item) => (
            <SessionCard
              key={item.id}
              item={item}
              onOpen={() => {
                haptic.tap()
                if (item.state === 'active') reset('chat')
                else push('archive', { id: item.id })
              }}
            />
          ))}
        </div>
      ))}

      <div className="t-cap" style={{ paddingTop: 4 }}>
        {t('sessions.footer', {
          hours: Math.round(data.ttl_minutes / 60),
          days: data.archive_days,
        })}
      </div>
    </Screen>
  )
}

function SessionCard({ item, onOpen }: { item: SessionSummary; onOpen: () => void }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const expired = item.state === 'expired'
  const stateLabel =
    item.state === 'active'
      ? t('sessions.stateActive')
      : item.state === 'ended'
        ? t('sessions.stateEnded')
        : t('sessions.stateExpired')

  const meta = [
    t('sessions.messages', { n: item.message_count }),
    item.feedback_rating ? `★ ${item.feedback_rating}` : '',
  ]
    .filter(Boolean)
    .join(' · ')

  const until = untilLabel(item.expires_at)

  return (
    <div
      className={`session-card${item.state === 'active' ? ' session-card--active' : ''}${
        expired ? ' session-card--expired' : ''
      }`}
    >
      <button
        style={{ width: '100%', textAlign: 'left', display: 'block' }}
        onClick={onOpen}
        aria-label={item.title || t('chat.title')}
      >
      <div className="session-card__meta">
        {expired ? (
          <Icon name="close" size={13} />
        ) : (
          <span className="dot" style={{ background: STATE_TONE[item.state] }} />
        )}
        <span className="session-card__state" style={{ color: STATE_TONE[item.state] }}>
          {stateLabel}
        </span>
        {until ? (
          <span className="t-cap num">{t('sessions.expiresIn', { value: until })}</span>
        ) : item.ended_at ? (
          <span className="t-cap num">{formatTime(Date.parse(item.ended_at), lang)}</span>
        ) : null}
      </div>

      <div className="session-card__title">{item.title || t('chat.title')}</div>
      <div className="t-cap">{meta}</div>
      </button>

      <button
        className={`session-card__action${
          item.state === 'active' ? ' session-card__action--primary' : ''
        }${expired ? ' session-card__action--disabled' : ''}`}
        onClick={onOpen}
      >
        {item.state === 'active'
          ? t('sessions.continue')
          : expired
            ? t('sessions.locked')
            : t('sessions.open')}
      </button>
    </div>
  )
}

/** 26a — a closed conversation opened read-only. */
export function ArchiveScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const reset = useNav((s) => s.reset)
  const setChatLoaded = useChat((s) => s.setLoaded)
  const id = String(entry.params?.id ?? '')

  const [session, setSession] = useState<SessionDetail | null>(null)
  const [failed, setFailed] = useState('')

  useEffect(() => {
    let cancelled = false
    api
      .session(id)
      .then((data) => !cancelled && setSession(data))
      .catch((error) => {
        if (cancelled) return
        setFailed(error instanceof ApiError ? error.message : t('common.error'))
      })
    return () => {
      cancelled = true
    }
  }, [id, t])

  const startNew = () => {
    // The archive is not writable — drop the cached feed so the chat screen
    // reloads the (new) active session instead of showing this one.
    setChatLoaded(false)
    haptic.tap()
    reset('chat')
  }

  if (failed) {
    return (
      <Screen title={t('sessions.title')}>
        <Notice tone="danger">{failed}</Notice>
      </Screen>
    )
  }
  if (!session) {
    return (
      <Screen title={t('sessions.title')}>
        <Skeleton lines={6} />
      </Screen>
    )
  }

  const endedAt = session.ended_at ? new Date(session.ended_at) : null
  const endedLabel = endedAt
    ? `${formatDay(endedAt.getTime(), lang)}, ${formatTime(endedAt.getTime(), lang)}`
    : ''

  return (
    <Screen
      title={session.title || t('chat.title')}
      subtitle={t('sessions.readonly')}
      main={{ text: t('sessions.startNew'), onClick: startNew }}
      secondaryBg
    >
      <div className="card" style={{ flexDirection: 'row', gap: 11, alignItems: 'flex-start' }}>
        <span className="t-hint" style={{ display: 'flex', marginTop: 2 }}>
          <Icon name="close" size={17} />
        </span>
        <span>
          <span className="t-row" style={{ display: 'block', fontWeight: 600 }}>
            {t('sessions.expiredTitle', { date: endedLabel })}
          </span>
          <span className="t-cap" style={{ display: 'block', marginTop: 4 }}>
            {t('sessions.expiredBody')}
          </span>
        </span>
      </div>

      <div className="stack stack--tight archive-feed">
        {session.messages.map((message) => {
          // Archive is read-only: no `onSend` → product cards render as plain
          // rows, only external actions (map / phone) stay tappable.
          const blocks = message.role === 'agent' ? sanitizeUiBlocks(message.ui_blocks) : []
          return (
            <Fragment key={message.id}>
              {message.text ? (
                <div
                  className={
                    message.role === 'user'
                      ? 'bubble bubble--out'
                      : message.role === 'operator'
                        ? 'bubble bubble--operator'
                        : 'bubble bubble--in'
                  }
                  style={
                    message.role === 'user'
                      ? { alignSelf: 'flex-end', background: 'var(--tg-secondary-bg)', color: 'var(--tg-text)' }
                      : undefined
                  }
                >
                  {message.text}
                </div>
              ) : null}
              {blocks.length ? <UiBlocksView blocks={blocks} /> : null}
            </Fragment>
          )
        })}
        <div className="t-cap t-center" style={{ padding: '8px 0' }}>
          {t('sessions.endOfChat')}
        </div>
      </div>

      <div className="composer-locked">
        <Icon name="close" size={15} />
        {t('sessions.sendDisabled')}
      </div>
    </Screen>
  )
}
