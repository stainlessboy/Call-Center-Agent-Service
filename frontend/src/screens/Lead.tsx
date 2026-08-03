import { useState } from 'react'

import { api } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { KV, Notice } from '../components/ui'
import { formatMoney, formatPhone, formatRate } from '../format'
import { useApp, useCalc, useLead, useNav, useT, type ScreenEntry } from '../store'
import { haptic } from '../telegram'

/** 18 — lead form. */
export function LeadScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const user = useApp((s) => s.data?.user)
  const { product, result, amount, termMonths } = useCalc()
  const requestId = useLead((s) => s.requestId)
  const newRequestId = useLead((s) => s.newRequestId)

  const [name, setName] = useState(user?.first_name ?? '')
  const [phone, setPhone] = useState(user?.phone ?? '')
  const [editingPhone, setEditingPhone] = useState(!user?.has_phone)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (name.trim().length < 2) {
      setError(t('lead.nameRequired'))
      haptic.error()
      return
    }
    if (!consent) {
      setError(t('lead.consentRequired'))
      haptic.error()
      return
    }
    if (!product) return

    setBusy(true)
    setError('')
    try {
      const response = await api.createLead({
        product_id: product.id,
        name: name.trim(),
        phone: phone.replace(/\D/g, ''),
        amount: result?.amount ?? amount,
        term_months: result?.term_months ?? termMonths,
        rate_pct: result?.rate_pct ?? null,
        consent,
        client_request_id: requestId,
      })
      haptic.success()
      // A fresh key for the next submission — the current one is now spent.
      newRequestId()
      push('leadSuccess', { number: response.lead_number })
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : t('common.error'))
      haptic.error()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Screen
      title={t('lead.title')}
      main={{
        text: busy ? t('lead.sending') : t('lead.submit'),
        onClick: submit,
        loading: busy,
        disabled: !consent || name.trim().length < 2,
        hint: !consent ? t('lead.consentRequired') : t('lead.nameRequired'),
      }}
    >
      <div className="t-h3">{t('lead.title')}</div>
      <div className="t-row t-sub">{t('lead.desc')}</div>

      <div className="stack stack--tight">
        <label className="t-cap">{t('lead.name')}</label>
        <input
          className="field"
          value={name}
          placeholder={t('lead.namePlaceholder')}
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
        />
      </div>

      <div className="stack stack--tight">
        <label className="t-cap">{t('lead.phone')}</label>
        {editingPhone ? (
          <input
            className="field num"
            inputMode="tel"
            value={phone}
            disabled={busy}
            onChange={(event) => setPhone(event.target.value)}
          />
        ) : (
          <div className="row" style={{ borderRadius: 12, border: '1px solid var(--tg-sep)' }}>
            <span className="row__main num">{formatPhone(phone)}</span>
            <button className="link" onClick={() => setEditingPhone(true)}>
              {t('lead.change')}
            </button>
          </div>
        )}
        <span className="t-cap">{t('lead.phoneHint')}</span>
      </div>

      {product ? (
        <div className="card card--flat">
          <div className="t-section">{t('lead.summary')}</div>
          <KV k={t('lead.product')} v={product.name} />
          <KV k={t('product.amount')} v={`${formatMoney(result?.amount ?? amount)} ${t('common.currency')}`} mono />
          <KV
            k={`${t('product.term')} · ${t('product.rate')}`}
            v={`${result?.term_months ?? termMonths} ${t('common.months')} · ${formatRate(result?.rate_pct ?? null)}`}
            mono
          />
          {result?.monthly_payment ? (
            <KV
              k={t('lead.payment')}
              v={`${formatMoney(result.monthly_payment)} ${t('common.perMonth')}`}
              mono
            />
          ) : null}
        </div>
      ) : null}

      <button className="checkbox" onClick={() => setConsent((value) => !value)} disabled={busy}>
        <span className={`checkbox__box${consent ? ' checkbox__box--on' : ''}`}>
          {consent ? <Icon name="check" size={13} /> : null}
        </span>
        <span className="t-cap" style={{ color: 'var(--tg-sub)' }}>
          {t('lead.consent')}
        </span>
      </button>

      {error ? <Notice tone="danger">{error}</Notice> : null}
    </Screen>
  )
}

/** 19 — success. */
export function LeadSuccessScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const reset = useNav((s) => s.reset)
  const push = useNav((s) => s.push)
  const { product, result } = useCalc()
  const number = String(entry.params?.number ?? '')

  return (
    <Screen
      title={t('lead.title')}
      text
      main={{ text: t('lead.toHome'), onClick: () => reset('home') }}
      secondary={{ text: t('lead.toChat'), onClick: () => push('chat') }}
    >
      <div className="center-col" style={{ paddingTop: 24 }}>
        <div className="plate plate--ok">
          <Icon name="check" size={34} />
        </div>
        <div className="t-h2">{t('lead.successTitle', { number })}</div>
        <div className="t-row t-sub">{t('lead.successDesc')}</div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        {product ? <KV k={t('lead.product')} v={product.name} /> : null}
        {result?.monthly_payment ? (
          <KV
            k={t('lead.payment')}
            v={`${formatMoney(result.monthly_payment)} ${t('common.perMonth')}`}
            mono
          />
        ) : null}
        <KV
          k={t('lead.status')}
          v={<span className="t-ok">● {t('lead.statusNew')}</span>}
        />
      </div>
    </Screen>
  )
}
