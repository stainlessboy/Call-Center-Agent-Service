import { useEffect, useMemo, useState } from 'react'

import { api, type ProductDetail } from '../api'
import { Screen } from '../components/Screen'
import { ErrorState, KV, Notice, Section, Segments, Skeleton } from '../components/ui'
import { formatMoney, formatRate, formatRateRange } from '../format'
import { useApp, useCalc, useNav, useT, type ScreenEntry } from '../store'

const CREDIT_CATEGORIES = ['mortgage', 'autoloan', 'microloan', 'education_credit']

/** 14/15 — product detail. Credits, deposits and cards share one screen shell. */
export function ProductScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const push = useNav((s) => s.push)
  const lang = useApp((s) => s.lang)
  const setProduct = useCalc((s) => s.setProduct)
  const id = String(entry.params?.id ?? '')

  const [product, setProduct_] = useState<ProductDetail | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setFailed(false)
    api
      .product(id)
      .then((data) => !cancelled && setProduct_(data))
      .catch(() => !cancelled && setFailed(true))
    return () => {
      cancelled = true
    }
  }, [id, lang])

  if (failed) {
    return (
      <Screen title={t('catalog.title')}>
        <ErrorState message={t('common.error')} retryLabel={t('common.retry')} />
      </Screen>
    )
  }
  if (!product) {
    return (
      <Screen title={t('catalog.title')}>
        <Skeleton lines={6} />
      </Screen>
    )
  }

  const isCredit = CREDIT_CATEGORIES.includes(product.category)
  const isDeposit = product.category === 'deposit'
  const calculable = isCredit || isDeposit

  const openCalculator = () => {
    setProduct(product)
    push('calc', { id: product.id })
  }

  return (
    <Screen
      title={product.name}
      subtitle={product.category_label}
      main={
        calculable
          ? {
              text: isDeposit ? t('product.calcIncome') : t('product.calculate'),
              onClick: openCalculator,
            }
          : undefined
      }
      secondary={calculable ? { text: t('product.ask'), onClick: () => push('chat') } : undefined}
    >
      <div className="t-h3">{product.name}</div>

      {isDeposit ? (
        <DepositBody product={product} />
      ) : isCredit ? (
        <CreditBody product={product} />
      ) : (
        <CardBody product={product} />
      )}

      <Notice tone="info">{t('product.ratesDisclaimer')}</Notice>
    </Screen>
  )
}

function RateHeadline({ value, tone }: { value: string; tone?: 'ok' }) {
  const t = useT()
  return (
    <div>
      <div className="t-cap">{t('product.rate')}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span
          className="num"
          style={{ fontSize: 34, fontWeight: 700, color: tone === 'ok' ? 'var(--ok)' : 'inherit' }}
        >
          {value}
        </span>
        <span className="t-cap">{t('common.annual')}</span>
      </div>
    </div>
  )
}

function CreditBody({ product }: { product: ProductDetail }) {
  const t = useT()
  const push = useNav((s) => s.push)
  const setProduct = useCalc((s) => s.setProduct)

  const explainers = useMemo(() => {
    const rows: string[] = []
    for (const entry of product.rate_matrix ?? []) {
      const parts: string[] = []
      if (entry.downpayment_min_pct != null) {
        parts.push(`${t('product.downpayment')} ${formatRate(entry.downpayment_min_pct)}`)
      }
      if (entry.term_min_months != null && entry.term_max_months != null) {
        parts.push(`${entry.term_min_months}–${entry.term_max_months} ${t('common.months')}`)
      }
      if (entry.condition_text) parts.push(entry.condition_text)
      const rate = formatRateRange(entry.rate_min_pct, entry.rate_max_pct)
      if (parts.length) rows.push(`${parts.join(' · ')} → ${rate}`)
    }
    return rows.slice(0, 6)
  }, [product.rate_matrix, t])

  return (
    <>
      <RateHeadline value={formatRateRange(product.rate_min_pct, product.rate_max_pct)} />

      {explainers.length ? (
        <div className="card card--flat">
          <div className="t-cap" style={{ fontWeight: 600, color: 'var(--tg-text)' }}>
            {t('product.rateExplainerTitle')}
          </div>
          {explainers.map((row) => (
            <div key={row} className="t-cap">
              • {row}
            </div>
          ))}
          <button
            className="link"
            style={{ alignSelf: 'flex-start' }}
            onClick={() => {
              setProduct(product)
              push('calc', { id: product.id })
            }}
          >
            {t('product.calcMyRate')} →
          </button>
        </div>
      ) : null}

      <div className="card">
        {product.amount_text ? <KV k={t('product.amount')} v={product.amount_text} mono /> : null}
        {product.term_text ? <KV k={t('product.term')} v={product.term_text} mono /> : null}
        {product.downpayment_text ? (
          <KV k={t('product.downpayment')} v={product.downpayment_text} mono />
        ) : null}
        {product.collateral ? <KV k={t('product.collateral')} v={product.collateral} /> : null}
        {product.purpose ? <KV k={t('product.purpose')} v={product.purpose} /> : null}
      </div>
    </>
  )
}

function DepositBody({ product }: { product: ProductDetail }) {
  const t = useT()
  const currencies = product.currencies?.length ? product.currencies : ['UZS']
  const [currency, setCurrency] = useState(currencies[0])

  const rows = (product.rate_schedule ?? []).filter((row) => row.currency === currency)
  const bestRate = rows.reduce<number | null>(
    (best, row) => (row.rate_pct != null && (best == null || row.rate_pct > best) ? row.rate_pct : best),
    null,
  )

  return (
    <>
      <RateHeadline value={bestRate != null ? formatRate(bestRate) : product.rate_text || '—'} tone="ok" />

      {currencies.length > 1 ? (
        <Segments
          items={currencies.map((code) => ({ value: code, label: code }))}
          value={currency}
          onChange={setCurrency}
        />
      ) : null}

      <Section title={t('product.depositTable')}>
        <div className="table-wrap card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="table">
            <thead>
              <tr>
                <th>{t('product.term')}</th>
                <th>{t('product.rate')}</th>
                <th>{t('product.amount')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr
                  key={`${row.term_months}-${index}`}
                  className={row.rate_pct != null && row.rate_pct === bestRate ? 'is-best' : ''}
                >
                  <td>
                    {row.term_text || `${row.term_months ?? '—'} ${t('common.months')}`}
                  </td>
                  <td>{row.rate_pct != null ? formatRate(row.rate_pct) : '—'}</td>
                  <td>{row.min_amount_text || (row.min_amount ? formatMoney(row.min_amount) : '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="card">
        {product.topup ? <KV k={t('product.topup')} v={product.topup} /> : null}
        {product.payout ? <KV k={t('product.payout')} v={product.payout} /> : null}
      </div>
    </>
  )
}

function CardBody({ product }: { product: ProductDetail }) {
  const t = useT()
  return (
    <div className="card">
      {product.network ? <KV k={t('product.network')} v={product.network.toUpperCase()} /> : null}
      {product.currency ? <KV k={t('product.currency')} v={product.currency} /> : null}
      {product.cashback ? <KV k={t('product.cashback')} v={product.cashback} /> : null}
      {product.issue_fee ? <KV k={t('product.issueFee')} v={product.issue_fee} /> : null}
      {product.annual_fee ? <KV k={t('product.annualFee')} v={product.annual_fee} /> : null}
      {product.validity ? <KV k={t('product.validity')} v={product.validity} /> : null}
      {product.delivery != null ? (
        <KV k={t('product.delivery')} v={product.delivery ? t('common.yes') : t('common.no')} />
      ) : null}
    </div>
  )
}
