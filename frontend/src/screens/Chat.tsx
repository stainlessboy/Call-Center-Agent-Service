import { useEffect, useRef, useState } from 'react'

import { api, eventsSocketUrl } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Chips } from '../components/ui'
import { UiBlocksView } from '../components/UiBlocks'
import { formatTime } from '../format'
import { useApp, useChat, useNav, useT, type ChatEntry, type ScreenEntry } from '../store'
import { haptic } from '../telegram'
import { sanitizeUiBlocks } from '../uiBlocks'

let entrySeq = 0
const nextId = () => `m${++entrySeq}`

/** 05–08 — chat feed, rich states, operator handoff and rating. */
export function ChatScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const push = useNav((s) => s.push)
  const operatorHours = useApp((s) => s.data?.operator)

  const {
    messages,
    quickReplies,
    typing,
    mode,
    operatorName,
    showOperatorButton,
    askRating,
    loaded,
    stream,
    setAll,
    add,
    update,
    setQuickReplies,
    setTyping,
    setMode,
    setShowOperatorButton,
    setAskRating,
    setLoaded,
    streamBegin,
    streamToken,
    streamDone,
    streamFinalize,
  } = useChat()

  const [draft, setDraft] = useState(String(entry.params?.draft ?? ''))
  const [blocked, setBlocked] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [offline, setOffline] = useState(!navigator.onLine)
  const [rating, setRating] = useState(0)
  const [ratingSent, setRatingSent] = useState(false)
  const feedRef = useRef<HTMLDivElement>(null)
  const requestedOperator = useRef(false)

  /* Load history once per mount. */
  useEffect(() => {
    if (loaded) return
    let cancelled = false
    api
      .chatHistory()
      .then((data) => {
        if (cancelled) return
        setAll(
          data.messages.map((message) => ({
            id: `db${message.id}`,
            role: (message.role === 'agent'
              ? 'agent'
              : message.role === 'operator'
                ? 'operator'
                : message.role === 'user'
                  ? 'user'
                  : 'system') as ChatEntry['role'],
            text: message.text,
            blocks: sanitizeUiBlocks(message.ui_blocks),
            at: message.created_at ? Date.parse(message.created_at) : Date.now(),
          })),
        )
        if (data.human_mode) setMode('operator')
        setLoaded(true)
      })
      .catch(() => setLoaded(true))
    return () => {
      cancelled = true
    }
  }, [loaded, setAll, setLoaded, setMode])

  /* Live operator events. */
  useEffect(() => {
    let socket: WebSocket | null = null
    let closed = false
    try {
      socket = new WebSocket(eventsSocketUrl())
    } catch {
      return
    }
    socket.onmessage = (event) => {
      let payload: Record<string, unknown>
      try {
        payload = JSON.parse(event.data as string)
      } catch {
        return
      }
      switch (payload.event) {
        case 'operator_joined':
          setMode('operator', String(payload.operator_name ?? ''))
          setNotice(t('chat.operatorJoined'))
          break
        case 'operator_message':
          add({ id: nextId(), role: 'operator', text: String(payload.text ?? ''), at: Date.now() })
          break
        case 'operator_file':
          add({
            id: nextId(),
            role: 'operator',
            text: String(payload.url ?? ''),
            at: Date.now(),
          })
          break
        case 'operator_left':
          setMode('bot')
          setAskRating(true)
          break
        case 'chat_ended':
        case 'operator_error':
          setMode('bot')
          add({ id: nextId(), role: 'system', text: String(payload.message ?? ''), at: Date.now() })
          break
        case 'inactivity_warning':
          add({ id: nextId(), role: 'system', text: String(payload.message ?? ''), at: Date.now() })
          break
        // Live token stream of the current bot turn (Phase 3). The store
        // drops tokens outside an active turn, so late/duplicate events
        // after the POST response are harmless.
        case 'assistant_token':
          streamToken(String(payload.text ?? ''))
          break
        case 'assistant_done':
          streamDone()
          break
        default:
          break
      }
    }
    socket.onclose = () => {
      if (!closed) socket = null
    }
    return () => {
      closed = true
      socket?.close()
    }
  }, [add, setAskRating, setMode, streamDone, streamToken, t])

  /* Network status → the offline plate in the feed. */
  useEffect(() => {
    const goOnline = () => setOffline(false)
    const goOffline = () => setOffline(true)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
  }, [])

  /* Keep the feed pinned to the newest message. */
  useEffect(() => {
    const node = feedRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [messages, typing, quickReplies, stream.text])

  const send = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return
    const localId = nextId()
    add({ id: localId, role: 'user', text: trimmed, pending: true, at: Date.now() })
    setDraft('')
    setQuickReplies([])
    setTyping(true)
    // Open the streaming slot for this turn — assistant_token events may
    // start arriving over the WS long before the POST response lands.
    const turn = streamBegin()
    try {
      const reply = await api.sendMessage(trimmed)
      update(localId, { pending: false })
      if (reply.blocked === 'daily_limit') {
        streamFinalize(undefined, turn)
        setBlocked(reply.text)
        setShowOperatorButton(true)
        return
      }
      // Replace the live bubble with the final message in one store update;
      // tokens that straggle in after this are dropped by the store.
      streamFinalize(
        {
          id: nextId(),
          role: reply.human_mode ? 'system' : 'agent',
          text: reply.text,
          blocks: sanitizeUiBlocks(reply.ui_blocks),
          at: Date.now(),
        },
        turn,
      )
      setQuickReplies(reply.quick_replies)
      setShowOperatorButton(reply.show_operator_button)
      if (reply.human_mode) setMode('operator')
    } catch {
      // The POST failed, but if tokens streamed in, the turn did complete
      // server-side — keep the accumulated text instead of dropping the answer.
      const { stream: current, streamTurn } = useChat.getState()
      const streamed = streamTurn === turn ? current.text : ''
      if (streamed) {
        streamFinalize({ id: nextId(), role: 'agent', text: streamed, at: Date.now() }, turn)
        update(localId, { pending: false })
      } else {
        streamFinalize(undefined, turn)
        update(localId, { pending: false, failed: true })
      }
      haptic.error()
    } finally {
      setTyping(false)
    }
  }

  const callOperator = async () => {
    setNotice('')
    const response = await api.toggleOperator(true)
    if (!response.ok) {
      setNotice(response.message || t('common.error'))
      if (response.reason === 'phone_required') push('settings')
      return
    }
    setMode('queue')
    setNotice(response.message || t('chat.operatorSearching'))
    haptic.tap()
  }

  const backToBot = async () => {
    await api.toggleOperator(false)
    setMode('bot')
    setNotice('')
  }

  /* Deep link from Home: "Оператор" opens the chat already requesting one. */
  useEffect(() => {
    if (entry.params?.requestOperator && !requestedOperator.current && loaded) {
      requestedOperator.current = true
      void callOperator()
    }
  }, [entry.params, loaded])

  const sendRating = async () => {
    if (!rating) return
    await api.rateOperator(rating)
    setRatingSent(true)
    setAskRating(false)
    haptic.success()
  }

  const outOfHours = operatorHours?.hours_enforced && !operatorHours.is_open_now

  return (
    <Screen
      title={mode === 'operator' ? operatorName || t('chat.operatorTitle') : t('chat.title')}
      subtitle={mode === 'operator' ? t('chat.operatorLabel') : undefined}
      flush
      main={askRating ? { text: t('chat.sendRating'), onClick: sendRating, disabled: !rating } : undefined}
    >
      <div className="chat">
        <div className="chat__feed" ref={feedRef}>
          <div className="system">{t('chat.disclaimer')}</div>

          {messages.map((message) => (
            <Bubble
              key={message.id}
              entry={message}
              operatorLabel={t('chat.operatorLabel')}
              onSend={mode === 'bot' && !blocked ? (value) => void send(value) : undefined}
            />
          ))}

          {stream.phase !== 'idle' && stream.text ? (
            <div className="bubble bubble--in">
              <RichText text={stream.text} />
              {stream.phase === 'accepting' ? <span className="stream-caret" /> : null}
            </div>
          ) : null}

          {typing && !stream.text ? (
            <div className="typing" aria-label={t('chat.searching')}>
              <i />
              <i />
              <i />
            </div>
          ) : null}

          {mode === 'queue' ? (
            <div className="system" style={{ background: 'var(--info-soft)' }}>
              {t('chat.operatorSearching')}
              <div
                style={{
                  height: 3,
                  borderRadius: 2,
                  background: 'var(--brand)',
                  marginTop: 8,
                  animation: 'tgpulse 1.4s ease-in-out infinite',
                }}
              />
            </div>
          ) : null}

          {mode === 'operator' ? (
            <div className="system">{t('chat.switchedToOperator')}</div>
          ) : null}

          {notice ? <div className="system">{notice}</div> : null}
          {blocked ? <div className="system">{blocked}</div> : null}
          {offline ? (
            <div className="system" style={{ background: 'var(--danger-soft)', color: 'var(--danger)' }}>
              {t('common.offline')}
            </div>
          ) : null}

          {outOfHours && mode === 'bot' ? (
            <div className="system" style={{ background: 'var(--warn-soft)', color: 'var(--warn)' }}>
              {`${operatorHours?.start_hour}:00–${operatorHours?.end_hour}:00`}
            </div>
          ) : null}

          {askRating ? (
            <div className="card" style={{ alignSelf: 'stretch' }}>
              <div className="t-row t-center">{t('chat.rateOperator')}</div>
              <div className="stars">
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    className={`star${value <= rating ? ' star--on' : ''}`}
                    onClick={() => {
                      haptic.select()
                      setRating(value)
                    }}
                    aria-label={`${value}`}
                  >
                    ★
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {ratingSent ? <div className="system">{t('chat.ratingThanks')}</div> : null}
        </div>

        {quickReplies.length ? (
          <div className="chat__quick">
            <Chips items={quickReplies.slice(0, 4)} onSelect={(value) => void send(value)} />
          </div>
        ) : null}

        {showOperatorButton && mode === 'bot' ? (
          <div className="chat__quick">
            <div className="duo">
              <button className="chip" onClick={() => void callOperator()}>
                {t('chat.callOperator')}
              </button>
              <button className="chip chip--neutral" onClick={() => setShowOperatorButton(false)}>
                {t('chat.askDifferently')}
              </button>
            </div>
          </div>
        ) : null}

        {mode !== 'bot' ? (
          <div className="chat__quick">
            <button className="chip chip--neutral" onClick={() => void backToBot()}>
              {t('chat.backToBot')}
            </button>
          </div>
        ) : null}

        <div className="chat__composer">
          <button className="t-hint" aria-label="attach" disabled>
            <Icon name="attach" size={22} />
          </button>
          <textarea
            className="chat__input"
            rows={1}
            value={draft}
            placeholder={offline ? t('chat.waitingConnection') : t('chat.placeholder')}
            disabled={Boolean(blocked) || offline}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void send(draft)
              }
            }}
          />
          <button
            aria-label="send"
            className={draft.trim() ? 't-brand' : 't-hint'}
            onClick={() => void send(draft)}
            disabled={!draft.trim() || offline}
          >
            <Icon name={draft.trim() ? 'send' : 'mic'} size={22} />
          </button>
        </div>
      </div>
    </Screen>
  )
}

function Bubble({
  entry,
  operatorLabel,
  onSend,
}: {
  entry: ChatEntry
  operatorLabel: string
  onSend?: (text: string) => void
}) {
  if (entry.role === 'system') {
    return <div className="system">{entry.text}</div>
  }
  const isOut = entry.role === 'user'
  const className = isOut
    ? 'bubble bubble--out'
    : entry.role === 'operator'
      ? 'bubble bubble--operator'
      : 'bubble bubble--in'

  return (
    <>
      {entry.text ? (
        <div className={`${className}${entry.pending ? ' bubble--pending' : ''}`}>
          {entry.role === 'operator' ? <div className="bubble__label">{operatorLabel}</div> : null}
          <RichText text={entry.text} />
          {isOut ? (
            <div className="bubble__time num">
              {formatTime(entry.at)} {entry.failed ? '!' : entry.pending ? '…' : '✓✓'}
            </div>
          ) : null}
        </div>
      ) : null}
      {entry.role === 'agent' && entry.blocks?.length ? (
        <UiBlocksView blocks={entry.blocks} onSend={onSend} />
      ) : null}
    </>
  )
}

/**
 * Agent replies come back as Telegram-flavoured HTML (<b>, <a>, \n). Render the
 * handful of tags the bot actually emits, and never inject raw HTML.
 * `**bold**` is also handled: models occasionally leak Markdown into an
 * HTML-parse-mode answer, and Telegram shows the asterisks raw too.
 */
function RichText({ text }: { text: string }) {
  const normalized = text.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
  const parts = normalized.split(/(<\/?b>|<\/?i>|<a href="[^"]*">|<\/a>)/g).filter(Boolean)
  const nodes: React.ReactNode[] = []
  let bold = false
  let italic = false
  let href: string | null = null

  parts.forEach((part, index) => {
    if (part === '<b>') return void (bold = true)
    if (part === '</b>') return void (bold = false)
    if (part === '<i>') return void (italic = true)
    if (part === '</i>') return void (italic = false)
    const linkMatch = part.match(/^<a href="([^"]*)">$/)
    if (linkMatch) return void (href = linkMatch[1])
    if (part === '</a>') return void (href = null)

    const content = part.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    if (href) {
      nodes.push(
        <a key={index} href={href} target="_blank" rel="noreferrer" style={{ color: 'var(--tg-link)' }}>
          {content}
        </a>,
      )
      return
    }
    nodes.push(
      <span key={index} style={{ fontWeight: bold ? 600 : undefined, fontStyle: italic ? 'italic' : undefined }}>
        {content}
      </span>,
    )
  })

  return <>{nodes}</>
}
