/**
 * Renderers for the agent's structured chat cards (`ui_blocks`) — one
 * component per block type, all defensive against partial/legacy data
 * (blocks are persisted in the DB and may predate the current schema).
 *
 * `onSend` makes product cards tappable (tap = send the product name as a
 * user message, continuing the bot's own select_product flow). When absent
 * (read-only feed) the same cards render as plain, non-interactive rows.
 */
import { useState } from 'react'

import { formatMoney, formatRate, formatRateRange } from '../format'
import { useApp, useT } from '../store'
import { haptic, openExternal } from '../telegram'
import {
  creditDownpaymentMin,
  creditTermRange,
  officeField,
  officeMapUrl,
  productName,
  rateName,
  type BlockOffice,
  type BlockProduct,
  type CalcResultData,
  type CalcScheduleRow,
  type ComparisonTableData,
  type OfficeDetailData,
  type OfficeListData,
  type ProductCardData,
  type ProductListData,
  type RateTableData,
  type UiBlock,
} from '../uiBlocks'
import { Icon } from './Icon'

const CREDIT_CATEGORIES = new Set(['mortgage', 'autoloan', 'microloan', 'education_credit'])

interface BlocksProps {
  blocks: UiBlock[]
  /** Send a message on card tap; omit to render the blocks read-only. */
  onSend?: (text: string) => void
}

export function UiBlocksView({ blocks, onSend }: BlocksProps) {
  if (!blocks.length) return null
  return (
    <div className="ublocks">
      {blocks.map((block, index) => {
        switch (block.type) {
          case 'product_list':
            return <ProductListBlock key={index} data={block.data} onSend={onSend} />
          case 'product_card':
            return <ProductCardBlock key={index} data={block.data} />
          case 'office_list':
            return <OfficeListBlock key={index} data={block.data} />
          case 'office_detail':
            return <OfficeDetailBlock key={index} data={block.data} />
          case 'rate_table':
            return <RateTableBlock key={index} data={block.data} />
          case 'calc_result':
            return <CalcResultBlock key={index} data={block.data} />
          case 'comparison_table':
            return <ComparisonTableBlock key={index} data={block.data} />
          default:
            return null
        }
      })}
    </div>
  )
}

/* ── Shared product formatting (numeric fields first, RU strings last) ── */

type Translate = ReturnType<typeof useT>

function productRateText(product: BlockProduct): string {
  if (product.rate_min_pct != null || product.rate_max_pct != null) {
    return formatRateRange(product.rate_min_pct ?? null, product.rate_max_pct ?? null)
  }
  if (product.rate_pct != null) return formatRate(product.rate_pct)
  return product.rate || ''
}

function creditAmountText(product: BlockProduct, t: Translate): string {
  const min = product.amount_min
  const max = product.amount_max
  if (min != null && max != null)
    return t('blocks.amountRange', { min: formatMoney(min), max: formatMoney(max) })
  if (max != null) return t('blocks.amountTo', { v: formatMoney(max) })
  if (min != null) return t('blocks.amountFrom', { v: formatMoney(min) })
  return product.amount || ''
}

function creditTermText(product: BlockProduct, t: Translate): string {
  const { min, max } = creditTermRange(product)
  if (min != null && max != null && min !== max) return t('blocks.termRange', { min, max })
  if (max != null) return t('blocks.termTo', { n: max })
  return product.term || ''
}

function creditDownpaymentText(product: BlockProduct, t: Translate): string {
  const min = creditDownpaymentMin(product)
  if (min != null) return t('blocks.downFrom', { p: min })
  return product.downpayment || ''
}

function depositTermText(product: BlockProduct, t: Translate): string {
  if (product.term_months != null) return `${product.term_months} ${t('common.months')}`
  if (product.term_min != null && product.term_max != null)
    return t('blocks.termRange', { min: product.term_min, max: product.term_max })
  return product.term || ''
}

/** The hint line under a product name: 2–3 key figures joined with " · ". */
function productMetaText(product: BlockProduct, category: string | null | undefined, t: Translate): string {
  const parts: string[] = []
  if (category && CREDIT_CATEGORIES.has(category)) {
    parts.push(creditAmountText(product, t), creditTermText(product, t), creditDownpaymentText(product, t))
  } else if (category === 'deposit') {
    parts.push(depositTermText(product, t), product.min_amount || '', product.currency || '')
  } else if (category === 'debit_card' || category === 'fx_card') {
    parts.push(product.network || '', product.currency || '', product.cashback || '')
  } else {
    // Unknown/legacy category — show whatever display strings exist.
    parts.push(product.amount || product.min_amount || '', product.term || '', product.network || '')
  }
  return parts.filter(Boolean).join(' · ')
}

const REASON_KEYS: Record<string, string> = {
  best_rate: 'results.best',
  age_fit: 'blocks.reasonAgeFit',
}

/* ── product_list ──────────────────────────────────────────────────────── */

function ProductListBlock({ data, onSend }: { data: ProductListData; onSend?: (text: string) => void }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const products = data.products ?? []
  if (!products.length) return null
  const recommend = data.kind === 'recommend'

  return (
    <div className="ublock ublock--list">
      {products.map((product, index) => {
        const name = productName(product, lang)
        const rate = productRateText(product)
        const meta = productMetaText(product, data.category, t)
        const reasons = recommend ? (product.reason ?? []) : []
        const body = (
          <>
            <span className="ublock-item__top">
              <span className="ublock-item__name">{name || '—'}</span>
              {rate ? <span className="ublock-item__rate num">{rate}</span> : null}
            </span>
            {meta ? <span className="ublock-item__meta num">{meta}</span> : null}
            {reasons.length ? (
              <span className="ublock-item__badges">
                {reasons.map((reason) => (
                  <span key={reason} className={`badge${reason === 'best_rate' ? ' badge--ok' : ' badge--brand'}`}>
                    {t(REASON_KEYS[reason] ?? reason)}
                  </span>
                ))}
              </span>
            ) : null}
          </>
        )
        // Tap continues the bot's own select_product flow: the product's base
        // name is what the reply-keyboard flow sends, so match it exactly.
        if (onSend && product.name) {
          return (
            <button
              key={product.id ?? index}
              className="ublock-item ublock-item--tap"
              onClick={() => {
                haptic.tap()
                onSend(product.name as string)
              }}
            >
              {body}
            </button>
          )
        }
        return (
          <div key={product.id ?? index} className="ublock-item">
            {body}
          </div>
        )
      })}
    </div>
  )
}

/* ── product_card ──────────────────────────────────────────────────────── */

function ProductCardBlock({ data }: { data: ProductCardData }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const product = data.product
  if (!product) return null
  const category = data.category
  const isCredit = Boolean(category && CREDIT_CATEGORIES.has(category))
  const isDeposit = category === 'deposit'
  const rate = productRateText(product)

  const rows: [string, string][] = []
  const pushRow = (label: string, value: string | null | undefined) => {
    if (value) rows.push([label, value])
  }
  if (isCredit) {
    pushRow(t('product.amount'), creditAmountText(product, t))
    pushRow(t('product.term'), creditTermText(product, t))
    pushRow(t('product.downpayment'), creditDownpaymentText(product, t))
    pushRow(t('product.purpose'), product.purpose)
    pushRow(t('product.collateral'), product.collateral)
  } else if (isDeposit) {
    pushRow(t('product.term'), depositTermText(product, t))
    pushRow(t('blocks.minAmount'), product.min_amount)
    pushRow(t('product.currency'), product.currency)
    pushRow(t('product.topup'), product.topup)
    pushRow(t('product.payout'), product.payout)
  } else {
    pushRow(t('product.network'), product.network)
    pushRow(t('product.currency'), product.currency)
    pushRow(t('product.cashback'), product.cashback)
    pushRow(t('product.issueFee'), product.issue_fee)
    pushRow(t('product.annualFee'), product.annual_fee)
    pushRow(t('product.validity'), product.validity)
  }

  return (
    <div className="ublock">
      <div className="ublock-item__top">
        <span className="ublock-title">{productName(product, lang) || '—'}</span>
        {rate ? <span className="ublock-item__rate num">{rate}</span> : null}
      </div>
      {data.personal_rate_pct != null ? (
        <div className="ublock-personal num">
          {t('blocks.personalRate', { rate: formatRate(data.personal_rate_pct) })}
        </div>
      ) : null}
      {rows.length ? (
        <div>
          {rows.map(([label, value]) => (
            <div key={label} className="kv" style={{ fontSize: 14 }}>
              <span className="kv__k">{label}</span>
              <span className="kv__v">{value}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/* ── office_list / office_detail ───────────────────────────────────────── */

function OfficeActions({ office, compact }: { office: BlockOffice; compact?: boolean }) {
  const t = useT()
  const mapUrl = officeMapUrl(office)
  if (!mapUrl && !office.phone) return null
  return (
    <div className="ublock-office__actions">
      {mapUrl ? (
        <button
          className={`ublock-action${compact ? '' : ' ublock-action--primary'}`}
          onClick={() => {
            haptic.tap()
            openExternal(mapUrl)
          }}
        >
          <Icon name="pin" size={15} />
          {compact ? t('blocks.onMap') : t('branches.route')}
        </button>
      ) : null}
      {office.phone ? (
        <a className="ublock-action" href={`tel:${office.phone.replace(/[^+\d]/g, '')}`}>
          <Icon name="phone" size={15} />
          {compact ? office.phone : t('branches.call')}
        </a>
      ) : null}
    </div>
  )
}

function OfficeSummary({ office, compact }: { office: BlockOffice; compact?: boolean }) {
  const lang = useApp((s) => s.lang)
  const name = officeField(office, 'name', lang)
  const address = officeField(office, 'address', lang)
  const region = officeField(office, 'region', lang)
  const landmark = officeField(office, 'landmark', lang)
  return (
    <>
      <span className="ublock-item__name">{name || '—'}</span>
      {address || region ? (
        <span className="ublock-item__meta">{[region, address].filter(Boolean).join(', ')}</span>
      ) : null}
      {!compact && landmark ? <span className="ublock-item__meta">{landmark}</span> : null}
      {office.hours ? <span className="ublock-item__meta num">{office.hours}</span> : null}
    </>
  )
}

function OfficeListBlock({ data }: { data: OfficeListData }) {
  const offices = data.offices ?? []
  if (!offices.length) return null
  return (
    <div className="ublock ublock--list">
      {offices.map((office, index) => (
        <div key={office.id ?? index} className="ublock-item">
          <OfficeSummary office={office} compact />
          <OfficeActions office={office} compact />
        </div>
      ))}
    </div>
  )
}

function OfficeDetailBlock({ data }: { data: OfficeDetailData }) {
  const office = data.office
  if (!office) return null
  return (
    <div className="ublock">
      <OfficeSummary office={office} />
      <OfficeActions office={office} />
    </div>
  )
}

/* ── rate_table ────────────────────────────────────────────────────────── */

/** 12 750.5 — group the integer part, keep real decimals (formatMoney rounds). */
function preciseMoney(value: number): string {
  const rounded = Math.round(value * 100) / 100
  const whole = Math.trunc(rounded)
  const frac = Math.round(Math.abs(rounded - whole) * 100)
  return frac ? `${formatMoney(whole)}.${String(frac).padStart(2, '0')}` : formatMoney(whole)
}

function RateTableBlock({ data }: { data: RateTableData }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const rates = (data.rates ?? []).filter((row) => row?.code)
  if (!rates.length) return null
  return (
    <div className="ublock">
      <div className="ublock-item__top">
        <span className="ublock-title">{t('rates.title')}</span>
        {data.date ? <span className="t-cap num">{data.date}</span> : null}
      </div>
      <div>
        {rates.map((row) => {
          const diff = row.diff ?? 0
          const tone = diff > 0 ? 'var(--ok)' : diff < 0 ? 'var(--danger)' : 'var(--tg-hint)'
          return (
            <div key={row.code} className="ublock-rate">
              <span className="ublock-rate__icon">{row.icon || ''}</span>
              <span className="ublock-rate__name">
                <b>{(row.nominal ?? 1) > 1 ? `${row.nominal} ${row.code}` : row.code}</b>
                <span className="t-cap">{rateName(row, lang)}</span>
              </span>
              <span className="ublock-rate__value num">
                {row.rate != null ? preciseMoney(row.rate) : '—'}
              </span>
              <span className="ublock-rate__diff num" style={{ color: tone }}>
                {row.rate != null && diff ? (
                  <>
                    <Icon name={diff > 0 ? 'arrowUp' : 'arrowDown'} size={11} />
                    {Math.abs(diff).toFixed(2)}
                  </>
                ) : null}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ── calc_result ───────────────────────────────────────────────────────── */

/**
 * Interest vs principal share of the annuity payment, month by month —
 * a part-to-whole split of one measure, so both areas are steps of the one
 * brand hue (identity carried by lightness, which survives CVD) with a 2px
 * surface gap between the stacked fills. The full schedule table right
 * below is the accessible data view.
 */
function ScheduleChart({ rows }: { rows: CalcScheduleRow[] }) {
  const t = useT()
  const points = rows.filter((row) => typeof row?.payment === 'number' && typeof row?.interest_part === 'number')
  if (points.length < 2) return null

  // Downsample long schedules; always keep the last month.
  const step = Math.max(1, Math.ceil(points.length / 60))
  const sampled = points.filter((_, index) => index % step === 0)
  if (sampled[sampled.length - 1] !== points[points.length - 1]) sampled.push(points[points.length - 1])

  const W = 320
  const H = 92
  const PAD = 2
  const maxY = Math.max(...sampled.map((row) => row.payment ?? 0))
  if (!(maxY > 0)) return null
  const x = (index: number) => PAD + (index / (sampled.length - 1)) * (W - PAD * 2)
  const y = (value: number) => H - PAD - (Math.max(0, value) / maxY) * (H - PAD * 2)

  const interestPts = sampled.map((row, index) => `${x(index).toFixed(1)},${y(row.interest_part ?? 0).toFixed(1)}`)
  const paymentPts = sampled.map((row, index) => `${x(index).toFixed(1)},${y(row.payment ?? 0).toFixed(1)}`)
  const interestArea = `M ${PAD},${H - PAD} L ${interestPts.join(' L ')} L ${W - PAD},${H - PAD} Z`
  const principalArea = `M ${paymentPts.join(' L ')} L ${[...interestPts].reverse().join(' L ')} Z`
  const lastMonth = points[points.length - 1]?.month ?? points.length

  return (
    <div className="ublock-chart">
      <div className="ublock-chart__legend">
        <span>
          <i style={{ background: 'var(--brand)', opacity: 0.25 }} />
          {t('blocks.principalShare')}
        </span>
        <span>
          <i style={{ background: 'var(--brand)' }} />
          {t('blocks.interestShare')}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        <path d={principalArea} fill="var(--brand)" opacity={0.25} />
        <path d={interestArea} fill="var(--brand)" />
        {/* 2px surface gap between the two stacked fills */}
        <polyline
          points={interestPts.join(' ')}
          fill="none"
          stroke="var(--tg-section-bg)"
          strokeWidth={2}
        />
      </svg>
      <div className="ublock-chart__axis num">
        <span>1</span>
        <span>
          {lastMonth} {t('common.months')}
        </span>
      </div>
    </div>
  )
}

function CalcTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="ublock-tile">
      <span className="t-cap">{label}</span>
      <span className="num" style={{ fontSize: 15, fontWeight: 600 }}>
        {value}
      </span>
    </div>
  )
}

/** "Платёж ≈ N% дохода" — ok tone up to the warn threshold, danger above. */
function DtiBadge({ data }: { data: CalcResultData }) {
  const t = useT()
  if (data.dti_ratio == null) return null
  const warn = data.dti_warn_ratio ?? 0.45
  return (
    <div>
      <span className={`badge${data.dti_ratio > warn ? ' badge--danger' : ' badge--ok'}`}>
        {t('blocks.dtiShare', { p: Math.round(data.dti_ratio * 100) })}
      </span>
    </div>
  )
}

function CalcResultBlock({ data }: { data: CalcResultData }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const isDeposit = data.kind === 'deposit'
  const isAffordability = data.kind === 'affordability'
  const big = isDeposit ? data.interest_total : data.monthly_payment
  const schedule = !isDeposit && !isAffordability ? (data.schedule ?? []) : []
  const PREVIEW_ROWS = 12
  const visibleRows = showAll ? schedule : schedule.slice(0, PREVIEW_ROWS)

  // Affordability check may carry only a bare monthly payment — show
  // whatever figures the tool actually knew.
  const affordabilityTiles: [string, number | null | undefined][] = isAffordability
    ? [
        [t('calc.loanAmount'), data.loan_amount],
        [t('blocks.clientIncome'), data.income_monthly],
      ]
    : []
  const visibleAffordabilityTiles = affordabilityTiles.filter(([, value]) => value != null)

  return (
    <div className="ublock">
      <div className="ublock-item__top">
        <span className="ublock-label">
          {isAffordability ? t('blocks.affordability') : t('blocks.calc')}
        </span>
        {data.product_name ? <span className="t-cap">{data.product_name}</span> : null}
      </div>
      {data.is_hypothetical ? <div className="t-cap">{t('blocks.hypothetical')}</div> : null}

      <div>
        <span className="t-cap">{isDeposit ? t('calc.income') : t('calc.monthlyPayment')}</span>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span className="num" style={{ fontSize: 30, fontWeight: 700, lineHeight: 1.1 }}>
            {formatMoney(big)}
          </span>
          <span className="t-cap">{isDeposit ? t('common.currency') : t('common.perMonth')}</span>
        </div>
      </div>

      <DtiBadge data={data} />

      {isAffordability ? (
        visibleAffordabilityTiles.length ? (
          <div className="ublock-tiles">
            {visibleAffordabilityTiles.map(([label, value]) => (
              <CalcTile key={label} label={label} value={formatMoney(value)} />
            ))}
          </div>
        ) : null
      ) : (
        <div className="ublock-tiles">
          {isDeposit ? (
            <>
              <CalcTile label={t('calc.amount')} value={formatMoney(data.amount)} />
              <CalcTile label={t('calc.incomeMonthly')} value={formatMoney(data.monthly_income)} />
              <CalcTile label={t('calc.atEnd')} value={formatMoney(data.total)} />
            </>
          ) : (
            <>
              <CalcTile label={t('calc.loanAmount')} value={formatMoney(data.principal)} />
              <CalcTile label={t('calc.overpayment')} value={formatMoney(data.overpayment)} />
              <CalcTile label={t('calc.total')} value={formatMoney(data.total_payment)} />
            </>
          )}
        </div>
      )}

      <div className="t-cap num">
        {[
          data.rate_pct != null ? formatRate(data.rate_pct) : '',
          data.term_months != null ? `${data.term_months} ${t('common.months')}` : '',
          !isDeposit && data.downpayment != null && data.downpayment > 0
            ? `${t('calc.downpayment').toLowerCase()} ${formatMoney(data.downpayment)}`
            : '',
        ]
          .filter(Boolean)
          .join(' · ')}
      </div>

      {schedule.length ? (
        <>
          <button className="ublock-toggle" onClick={() => setOpen(!open)}>
            <span>{t('calc.schedule')}</span>
            <Icon name="chevron" size={16} className={open ? 'rot90' : 'rot0'} />
          </button>
          {open ? (
            <>
              <ScheduleChart rows={schedule} />
              {data.schedule_truncated ? (
                <div className="t-cap">{t('blocks.scheduleTruncated', { n: schedule.length })}</div>
              ) : null}
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('calc.colMonth')}</th>
                      <th>{t('calc.colPayment')}</th>
                      <th>{t('calc.colInterest')}</th>
                      <th>{t('calc.colBalance')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRows.map((row, index) => (
                      <tr key={row.month ?? index}>
                        <td>{row.month ?? index + 1}</td>
                        <td style={{ fontWeight: 600 }}>{formatMoney(row.payment)}</td>
                        <td>{formatMoney(row.interest_part)}</td>
                        <td>{formatMoney(row.balance)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!showAll && schedule.length > PREVIEW_ROWS ? (
                <button className="ublock-toggle" style={{ justifyContent: 'center' }} onClick={() => setShowAll(true)}>
                  {t('calc.showAll', { n: schedule.length })}
                </button>
              ) : null}
            </>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

/* ── comparison_table ──────────────────────────────────────────────────── */

const COLUMN_LABEL_KEYS: Record<string, string> = {
  rate: 'product.rate',
  term: 'product.term',
  amount: 'product.amount',
  downpayment: 'product.downpayment',
  min_amount: 'blocks.minAmount',
  currency: 'product.currency',
  network: 'product.network',
  annual_fee: 'product.annualFee',
  cashback: 'product.cashback',
}

function comparisonValue(
  product: BlockProduct,
  column: string,
  category: string | null | undefined,
  t: Translate,
): string {
  const isCredit = Boolean(category && CREDIT_CATEGORIES.has(category))
  switch (column) {
    case 'rate':
      return productRateText(product) || '—'
    case 'term':
      return (isCredit ? creditTermText(product, t) : depositTermText(product, t)) || '—'
    case 'amount':
      return creditAmountText(product, t) || '—'
    case 'downpayment':
      return creditDownpaymentText(product, t) || '—'
    case 'min_amount':
      return product.min_amount || '—'
    case 'currency':
      return product.currency || '—'
    case 'network':
      return product.network || '—'
    case 'annual_fee':
      return product.annual_fee || '—'
    case 'cashback':
      return product.cashback || '—'
    default: {
      const raw = (product as Record<string, unknown>)[column]
      return typeof raw === 'string' || typeof raw === 'number' ? String(raw) : '—'
    }
  }
}

function ComparisonTableBlock({ data }: { data: ComparisonTableData }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const products = data.products ?? []
  const columns = data.columns?.length ? data.columns : ['rate', 'term', 'amount']
  if (products.length < 2) return null

  return (
    <div className="ublock" style={{ padding: 0, overflow: 'hidden' }}>
      <div className="ublock-item__top" style={{ padding: 'var(--sp-3) var(--sp-3) 0' }}>
        <span className="ublock-title">{t('blocks.comparison')}</span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th />
              {products.map((product, index) => (
                <th key={product.id ?? index}>{productName(product, lang) || '—'}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {columns.map((column) => (
              <tr key={column}>
                <td className="t-hint">{t(COLUMN_LABEL_KEYS[column] ?? column)}</td>
                {products.map((product, index) => (
                  <td key={product.id ?? index} className="num">
                    {comparisonValue(product, column, data.category, t)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
