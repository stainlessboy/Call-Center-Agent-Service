import { useEffect, useMemo, useRef, useState } from 'react'

import { api, type CalcAdjustment, type CalcPayload, type ScheduleRow } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Notice, Presets, Skeleton } from '../components/ui'
import { formatMoney, formatRate, parseMoney } from '../format'
import { useCalc, useNav, useT } from '../store'
import { haptic, openExternal } from '../telegram'

const TERM_PRESETS = [12, 24, 36, 60]
const AGE_PRESETS = [25, 35, 50]

function payloadOf(state: ReturnType<typeof useCalc.getState>): CalcPayload | null {
  if (!state.product) return null
  return {
    product_id: state.product.id,
    amount: state.amount,
    term_months: state.termMonths,
    downpayment_pct: state.downpaymentPct,
    age: state.age,
  }
}

/** 16 — calculator with soft correction of out-of-range input. */
export function CalculatorScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const { product, amount, termMonths, downpaymentPct, age, patch, setResult } = useCalc()

  const [rate, setRate] = useState<number | null>(null)
  const [adjustments, setAdjustments] = useState<CalcAdjustment[]>([])
  const [busy, setBusy] = useState(false)
  const [amountText, setAmountText] = useState(() => formatMoney(amount))
  const debounce = useRef<number | undefined>(undefined)

  const bounds = product?.bounds
  const isDeposit = product?.category === 'deposit'
  const needsAge = Boolean(product?.needs_age)

  // Only offer terms the product actually supports, so the user never picks a
  // value the backend has to clamp away.
  const termOptions = useMemo(() => {
    if (bounds?.term_options?.length) return bounds.term_options.slice(0, 4)
    const min = bounds?.term_min_months ?? 1
    const max = bounds?.term_max_months
    const within = TERM_PRESETS.filter((value) => value >= min && (max == null || value <= max))
    if (max != null && !within.includes(max)) within.push(max)
    return (within.length ? within : [max ?? TERM_PRESETS[0]]).slice(-4)
  }, [bounds])

  useEffect(() => {
    setAmountText(formatMoney(amount))
  }, [amount])

  // Live rate preview — debounced so typing an amount does not spam the API.
  useEffect(() => {
    const payload = payloadOf(useCalc.getState())
    if (!payload || !payload.amount || !payload.term_months) return
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(async () => {
      try {
        const result = await api.calc(payload)
        setRate(result.rate_pct)
        setAdjustments(result.adjustments)
      } catch {
        setRate(null)
      }
    }, 400)
    return () => window.clearTimeout(debounce.current)
  }, [amount, termMonths, downpaymentPct, age, product?.id])

  const amountAdjustment = adjustments.find((item) => item.field === 'amount')
  const termAdjustment = adjustments.find((item) => item.field === 'term_months')

  const submit = async () => {
    const payload = payloadOf(useCalc.getState())
    if (!payload) return
    setBusy(true)
    try {
      const result = await api.calc(payload)
      setResult(result)
      // The backend clamps; mirror the applied values back into the form so
      // the result screen and the form never disagree.
      patch({
        amount: result.amount,
        termMonths: result.term_months,
        downpaymentPct: result.downpayment_pct ?? null,
      })
      setResult(result)
      haptic.success()
      push('calcResult')
    } catch {
      haptic.error()
    } finally {
      setBusy(false)
    }
  }

  if (!product) {
    return (
      <Screen title={t('calc.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  const downpaymentAmount = downpaymentPct != null ? Math.round((amount * downpaymentPct) / 100) : 0

  return (
    <Screen
      title={t('calc.title')}
      subtitle={product.name}
      main={{
        text: t('calc.calculate'),
        onClick: submit,
        loading: busy,
        disabled: !amount || !termMonths,
        hint: t('calc.amount'),
      }}
    >
      <div className={`amount${amountAdjustment ? ' amount--warn' : ''}`}>
        <span className="t-cap">{t('calc.amount')}</span>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <input
            className="amount__input"
            inputMode="numeric"
            value={amountText}
            onChange={(event) => setAmountText(formatMoney(parseMoney(event.target.value)))}
            onBlur={() => patch({ amount: parseMoney(amountText) })}
          />
          <span className="t-sub">{t('common.currency')}</span>
        </div>
        {bounds?.amount_min || bounds?.amount_max ? (
          <span className="t-cap">
            {t('calc.amountHint', {
              min: formatMoney(bounds?.amount_min ?? 0),
              max: formatMoney(bounds?.amount_max ?? 0),
            })}
          </span>
        ) : null}
      </div>

      {amountAdjustment ? (
        <Notice tone="warn">
          {amountAdjustment.reason === 'max'
            ? t('calc.softMax', { value: formatMoney(amountAdjustment.applied) })
            : t('calc.softMin', { value: formatMoney(amountAdjustment.applied) })}
        </Notice>
      ) : null}

      {!isDeposit && bounds?.downpayment_max_pct != null ? (
        <div className="card card--flat">
          <div style={{ display: 'flex', alignItems: 'baseline' }}>
            <span className="t-cap" style={{ flex: 1 }}>
              {t('calc.downpayment')}
            </span>
            <span className="num" style={{ fontSize: 16, fontWeight: 600 }}>
              {downpaymentPct ?? 0}% · {formatMoney(downpaymentAmount)}
            </span>
          </div>
          <input
            className="slider"
            type="range"
            min={bounds.downpayment_min_pct ?? 0}
            max={bounds.downpayment_max_pct ?? 90}
            step={5}
            value={downpaymentPct ?? bounds.downpayment_min_pct ?? 0}
            onChange={(event) => patch({ downpaymentPct: Number(event.target.value) })}
            onPointerUp={() => haptic.select()}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span className="t-cap num">{bounds.downpayment_min_pct ?? 0}%</span>
            <span className="t-cap num">{bounds.downpayment_max_pct}%</span>
          </div>
        </div>
      ) : null}

      <div className="stack stack--tight">
        <span className="t-cap">{t('calc.term')}</span>
        <Presets
          items={termOptions}
          value={termMonths}
          onChange={(value) => patch({ termMonths: value })}
          render={(value) => `${value} ${t('common.months')}`}
        />
        {termAdjustment ? (
          <Notice tone="warn">{t('calc.softTerm', { value: termAdjustment.applied })}</Notice>
        ) : null}
      </div>

      {needsAge ? (
        <div className="stack stack--tight">
          <span className="t-cap">{t('calc.age')}</span>
          <Presets items={AGE_PRESETS} value={age} onChange={(value) => patch({ age: value })} />
        </div>
      ) : null}

      <div className="notice notice--brand" style={{ marginTop: 'auto' }}>
        <div className="t-cap" style={{ color: 'inherit' }}>
          {t('calc.yourRate')}
        </div>
        <div className="num" style={{ fontSize: 20, fontWeight: 700 }}>
          {rate != null ? formatRate(rate) : '—'}
        </div>
      </div>
    </Screen>
  )
}

/** 17 — calculation result with the amortization preview. */
export function CalcResultScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const { product, result } = useCalc()
  const [rows, setRows] = useState<ScheduleRow[] | null>(null)
  const [totalRows, setTotalRows] = useState(0)
  const [expanded, setExpanded] = useState(false)

  const payload = useMemo(() => payloadOf(useCalc.getState()), [product?.id, result])

  useEffect(() => {
    if (!payload || result?.kind !== 'credit') return
    let cancelled = false
    api
      .schedule(payload, !expanded)
      .then((data) => {
        if (cancelled) return
        setRows(data.rows)
        setTotalRows(data.total_rows)
      })
      .catch(() => !cancelled && setRows([]))
    return () => {
      cancelled = true
    }
  }, [payload, expanded, result?.kind])

  if (!result || !product) {
    return (
      <Screen title={t('calc.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  const isDeposit = result.kind === 'deposit'

  return (
    <Screen
      title={t('calc.title')}
      subtitle={product.name}
      main={{ text: t('calc.wantCall'), onClick: () => push('lead') }}
    >
      <div className="card center-col">
        <span className="t-cap">
          {isDeposit ? t('calc.income') : t('calc.monthlyPayment')}
        </span>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span className="num" style={{ fontSize: 36, fontWeight: 700 }}>
            {formatMoney(isDeposit ? result.income : result.monthly_payment)}
          </span>
          <span className="t-sub">{t('common.currency')}</span>
        </div>
        <div className="duo" style={{ width: '100%', paddingTop: 8 }}>
          <Tile
            label={isDeposit ? t('calc.amount') : t('calc.loanAmount')}
            value={formatMoney(isDeposit ? result.amount : result.principal)}
          />
          <Tile
            label={isDeposit ? t('calc.incomeMonthly') : t('calc.overpayment')}
            value={formatMoney(isDeposit ? result.monthly_income : result.overpayment)}
          />
          <Tile
            label={isDeposit ? t('calc.atEnd') : t('calc.total')}
            value={formatMoney(isDeposit ? result.total : result.total_payment)}
          />
        </div>
        <div className="t-cap num" style={{ paddingTop: 4 }}>
          {formatRate(result.rate_pct)} · {result.term_months} {t('common.months')}
        </div>
      </div>

      {!isDeposit ? (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              padding: 'var(--sp-3) var(--sp-4)',
            }}
          >
            <span className="t-section" style={{ flex: 1 }}>
              {t('calc.schedule')}
            </span>
            {payload ? (
              <button
                className="link"
                style={{ display: 'flex', alignItems: 'center', gap: 4 }}
                onClick={() => openExternal(api.schedulePdfUrl(payload))}
              >
                <Icon name="download" size={16} />
                {t('calc.downloadPdf')}
              </button>
            ) : null}
          </div>
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
                {(rows ?? []).map((row) => (
                  <tr key={row.month}>
                    <td>{row.month}</td>
                    <td style={{ fontWeight: 600 }}>{formatMoney(row.payment)}</td>
                    <td>{formatMoney(row.interest_part)}</td>
                    <td>{formatMoney(row.balance)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!expanded && totalRows > (rows?.length ?? 0) ? (
            <button
              className="row"
              onClick={() => setExpanded(true)}
              style={{ justifyContent: 'center', color: 'var(--tg-link)' }}
            >
              {t('calc.showAll', { n: totalRows })}
            </button>
          ) : null}
        </div>
      ) : null}

      <Notice tone="info">{t('calc.disclaimer')}</Notice>
    </Screen>
  )
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="card card--flat" style={{ padding: 10, gap: 2, borderRadius: 11 }}>
      <span className="t-cap">{label}</span>
      <span className="num" style={{ fontSize: 15, fontWeight: 600 }}>
        {value}
      </span>
    </div>
  )
}
