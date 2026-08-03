import { useEffect, useMemo, useState } from 'react'

import { api, type CatalogGroup, type ProductSummary, type QualifyTree } from '../api'
import { CATEGORY_ICONS, Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Chips, ErrorState, Notice, Option, Progress, Row, Rows, Section, Skeleton } from '../components/ui'
import { formatRateRange } from '../format'
import { useApp, useNav, useT, type ScreenEntry } from '../store'
import { haptic } from '../telegram'

/** 09 — product catalog grouped into "Кредиты" / "Сбережения и карты". */
export function CatalogScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const lang = useApp((s) => s.lang)
  const [groups, setGroups] = useState<CatalogGroup[] | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .catalog()
      .then((data) => !cancelled && setGroups(data.groups))
      .catch(() => !cancelled && setGroups([]))
    return () => {
      cancelled = true
    }
  }, [lang])

  return (
    <Screen title={t('catalog.title')} secondaryBg>
      {groups === null ? (
        <Skeleton lines={4} />
      ) : (
        groups.map((group) => (
          <Section key={group.key} title={group.title}>
            <Rows>
              {group.categories.map((category) => (
                <Row
                  key={category.category}
                  icon={CATEGORY_ICONS[category.category] ?? 'card'}
                  title={category.title}
                  subtitle={category.subtitle}
                  chevron
                  onClick={() =>
                    push(category.has_qualify ? 'qualify' : 'results', {
                      category: category.category,
                    })
                  }
                />
              ))}
            </Rows>
          </Section>
        ))
      )}

      <button
        className="notice notice--brand"
        style={{ textAlign: 'left' }}
        onClick={() => {
          haptic.tap()
          push('chat')
        }}
      >
        {t('catalog.helpBanner')}
      </button>
    </Screen>
  )
}

interface Answered {
  nodeKey: string
  question: string
  label: string
  optionIndex: number
}

/**
 * 10/11 — qualification, one question per screen.
 *
 * The tree is fetched once and walked on the client; the collected answers are
 * posted to /qualify/result, which runs the same DB filter the bot uses.
 */
export function QualifyScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const push = useNav((s) => s.push)
  const replace = useNav((s) => s.replace)
  const lang = useApp((s) => s.lang)
  const category = String(entry.params?.category ?? '')

  const [tree, setTree] = useState<QualifyTree | null>(null)
  const [failed, setFailed] = useState(false)
  const [nodeKey, setNodeKey] = useState<string | null>(null)
  const [answered, setAnswered] = useState<Answered[]>([])
  const [selected, setSelected] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    setFailed(false)
    api
      .qualifyTree(category)
      .then((data) => {
        if (cancelled) return
        setTree(data)
        setNodeKey(data.entry)
      })
      .catch(() => {
        if (cancelled) return
        // No tree for this category — go straight to the product list.
        replace('results', { category })
      })
    return () => {
      cancelled = true
    }
  }, [category, lang, replace])

  const node = tree && nodeKey ? tree.nodes[nodeKey] : null

  // Answers accumulate the `set` patches of every chosen option.
  const answers = useMemo(() => {
    if (!tree) return {}
    const collected: Record<string, unknown> = {}
    for (const step of answered) {
      const option = tree.nodes[step.nodeKey]?.options?.[step.optionIndex]
      Object.assign(collected, option?.set ?? {})
    }
    return collected
  }, [answered, tree])

  useEffect(() => {
    if (!node || !tree) return
    if (node.type === 'filter') {
      // Carry the chosen option labels so the results screen can show readable
      // filter chips instead of the raw answer keys.
      replace('results', { category, answers, labels: answered.map((step) => step.label) })
    }
  }, [node, tree, answers, answered, category, replace])

  if (failed) {
    return (
      <Screen title={t('catalog.title')}>
        <ErrorState message={t('common.error')} retryLabel={t('common.retry')} />
      </Screen>
    )
  }

  if (!tree || !node) {
    return (
      <Screen title={t('catalog.title')}>
        <Skeleton lines={4} />
      </Screen>
    )
  }

  // 13 — dead-end branch ("подходящих предложений нет").
  if (node.type === 'dead_end') {
    return (
      <Screen
        title={tree.category_label}
        text
        main={{ text: t('qualify.askAssistant'), onClick: () => push('chat') }}
      >
        <div className="center-col" style={{ paddingTop: 32 }}>
          <div className="plate plate--dashed">
            <Icon name="search" size={26} />
          </div>
          <div className="t-h3">{node.message || t('results.empty')}</div>
          <div className="t-row t-sub">{t('results.emptyHint')}</div>
        </div>
        <button
          className="link t-center"
          style={{ marginTop: 'auto' }}
          onClick={() => {
            setAnswered([])
            setSelected(null)
            setNodeKey(tree.entry)
          }}
        >
          {t('qualify.changeAnswers')}
        </button>
      </Screen>
    )
  }

  const options = node.options ?? []
  const stepNumber = answered.length + 1

  const goNext = () => {
    if (selected == null) return
    const option = options[selected]
    setAnswered((current) => [
      ...current,
      {
        nodeKey: node.key,
        question: node.question ?? '',
        label: option.label,
        optionIndex: selected,
      },
    ])
    setSelected(null)
    setNodeKey(option.goto)
  }

  const rewindTo = (index: number) => {
    const step = answered[index]
    setAnswered((current) => current.slice(0, index))
    setSelected(step.optionIndex)
    setNodeKey(step.nodeKey)
  }

  const isLast = selected != null && tree.nodes[options[selected]?.goto ?? '']?.type === 'filter'

  return (
    <Screen
      title={tree.category_label}
      subtitle={t('qualify.step', { n: stepNumber, total: tree.max_steps })}
      main={{
        text: isLast ? t('qualify.showOffers') : t('common.next'),
        onClick: goNext,
        disabled: selected == null,
      }}
    >
      <Progress total={tree.max_steps} done={answered.length} />
      <div className="t-cap">{t('qualify.step', { n: stepNumber, total: tree.max_steps })}</div>
      <div className="t-h2">{node.question}</div>

      <div className="stack stack--tight" style={{ paddingTop: 4 }}>
        {options.map((option) => (
          <Option
            key={option.index}
            label={option.label}
            selected={selected === option.index}
            onClick={() => setSelected(option.index)}
          />
        ))}
      </div>

      {answered.length ? (
        <div className="card card--flat" style={{ marginTop: 4 }}>
          <div className="t-section">{t('qualify.yourAnswers')}</div>
          {answered.map((step, index) => (
            <button
              key={step.nodeKey}
              className="kv"
              style={{ width: '100%', textAlign: 'left' }}
              onClick={() => rewindTo(index)}
            >
              <span className="kv__k">{step.question}</span>
              <span className="kv__v">{step.label}</span>
            </button>
          ))}
        </div>
      ) : null}

      <button
        className="link t-center"
        style={{ marginTop: 'auto', paddingTop: 12 }}
        onClick={() => push('chat')}
      >
        {t('qualify.askAssistant')}
      </button>
    </Screen>
  )
}

/** 12 — qualification results (or the plain product list when no tree). */
export function ResultsScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const push = useNav((s) => s.push)
  const pop = useNav((s) => s.pop)
  const lang = useApp((s) => s.lang)
  const category = String(entry.params?.category ?? '')
  const answers = (entry.params?.answers ?? null) as Record<string, unknown> | null
  const appliedChips = (entry.params?.labels ?? []) as string[]

  const [items, setItems] = useState<ProductSummary[] | null>(null)
  const [bestId, setBestId] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [emptyMessage, setEmptyMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    const load = answers
      ? api.qualifyResult(category, answers).then((data) => {
          if (cancelled) return
          setItems(data.items)
          setBestId(data.best_id)
          setEmptyMessage(data.empty_message)
        })
      : api.products(category).then((data) => {
          if (cancelled) return
          setItems(data.items)
          setTitle(data.category_label)
        })
    load.catch(() => !cancelled && setItems([]))
    return () => {
      cancelled = true
    }
  }, [category, lang, answers])

  const best = items?.find((item) => item.id === bestId) ?? null

  if (items === null) {
    return (
      <Screen title={title || t('results.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  // 13 — empty result. Never a dead end: chat and "change answers" stay open.
  if (!items.length) {
    return (
      <Screen
        title={title || t('results.title')}
        text
        main={{ text: t('qualify.askAssistant'), onClick: () => push('chat') }}
      >
        <div className="center-col" style={{ paddingTop: 32 }}>
          <div className="plate plate--dashed">
            <Icon name="search" size={26} />
          </div>
          <div className="t-h3">{emptyMessage || t('results.empty')}</div>
          <div className="t-row t-sub">{t('results.emptyHint')}</div>
        </div>
        <div className="stack stack--tight" style={{ paddingTop: 16 }}>
          <Rows>
            <Row icon="bank" title={t('catalog.title')} chevron onClick={() => push('catalog')} />
            <Row icon="headset" title={t('chat.callOperator')} chevron onClick={() => push('chat', { requestOperator: true })} />
          </Rows>
        </div>
        {answers ? (
          <button className="link t-center" style={{ marginTop: 'auto' }} onClick={pop}>
            {t('qualify.changeAnswers')}
          </button>
        ) : null}
      </Screen>
    )
  }

  return (
    <Screen
      title={title || t('results.title')}
      secondaryBg
      main={
        best
          ? { text: t('results.openBest'), onClick: () => push('product', { id: best.id }) }
          : undefined
      }
    >
      {appliedChips.length ? <Chips items={appliedChips} neutral onSelect={() => pop()} /> : null}

      <div className="stack">
        {items.map((item) => (
          <ProductCard
            key={item.id}
            item={item}
            best={item.id === bestId}
            bestLabel={t('results.best')}
            onClick={() => push('product', { id: item.id })}
          />
        ))}
      </div>

      {answers ? (
        <button className="link t-center" onClick={pop}>
          {t('qualify.changeAnswers')}
        </button>
      ) : null}
    </Screen>
  )
}

function ProductCard({
  item,
  best,
  bestLabel,
  onClick,
}: {
  item: ProductSummary
  best: boolean
  bestLabel: string
  onClick: () => void
}) {
  const isDeposit = item.category === 'deposit'
  const rate = isDeposit
    ? item.rate_text || '—'
    : formatRateRange(item.rate_min_pct, item.rate_max_pct)

  const meta = [item.amount_text, item.term_text, item.downpayment_text, item.min_amount_text, item.currency]
    .filter(Boolean)
    .join(' · ')

  return (
    <button
      className={`card card--tap${best ? ' card--selected' : ''}`}
      onClick={() => {
        haptic.tap()
        onClick()
      }}
    >
      {best ? (
        <span className="badge badge--ok" style={{ alignSelf: 'flex-start' }}>
          {bestLabel}
        </span>
      ) : null}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <span className="t-title" style={{ flex: 1, fontSize: best ? 19 : 17, fontWeight: 700 }}>
          {item.name}
        </span>
        <span className="num" style={{ fontSize: best ? 24 : 18, fontWeight: 700, color: 'var(--ok)' }}>
          {rate}
        </span>
      </div>
      {meta ? <span className="t-cap">{meta}</span> : null}
    </button>
  )
}

export function ProductListNotice({ message }: { message: string }) {
  return <Notice tone="warn">{message}</Notice>
}
