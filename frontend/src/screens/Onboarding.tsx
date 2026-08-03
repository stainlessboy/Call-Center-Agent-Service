import { useState } from 'react'

import type { Lang } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { Notice, Option } from '../components/ui'
import { formatPhone } from '../format'
import { LANGUAGE_NAMES } from '../i18n'
import { useApp, useNav, useT } from '../store'
import { haptic, requestContact } from '../telegram'

/** 01 — language picker. */
export function LanguageScreen() {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const setLang = useApp((s) => s.setLang)
  const hasPhone = useApp((s) => s.data?.user.has_phone ?? false)
  const push = useNav((s) => s.push)
  const reset = useNav((s) => s.reset)
  const [selected, setSelected] = useState<Lang>(lang)
  const [saving, setSaving] = useState(false)

  const languages = useApp((s) => s.data?.langs ?? (['ru', 'uz', 'en'] as Lang[]))

  const submit = async () => {
    setSaving(true)
    try {
      await setLang(selected)
      if (hasPhone) reset('home')
      else push('phone')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Screen
      title={t('lang.title')}
      text
      main={{ text: t('common.continue'), onClick: submit, loading: saving }}
    >
      <div className="center-col" style={{ paddingTop: 24, paddingBottom: 8 }}>
        <div className="plate">A</div>
        <div className="t-h2">{t('lang.title')}</div>
        <div className="t-row t-hint">{t('lang.subtitle')}</div>
      </div>
      <div className="stack stack--tight">
        {languages.map((code) => (
          <Option
            key={code}
            label={LANGUAGE_NAMES[code] ?? code}
            selected={selected === code}
            onClick={() => setSelected(code)}
          />
        ))}
      </div>
      <div className="t-cap t-center" style={{ marginTop: 'auto', paddingTop: 16 }}>
        {t('lang.footer')}
      </div>
    </Screen>
  )
}

/** 02 — phone request. */
export function PhoneScreen() {
  const t = useT()
  const user = useApp((s) => s.data?.user)
  const setPhone = useApp((s) => s.setPhone)
  const refresh = useApp((s) => s.refresh)
  const push = useNav((s) => s.push)
  const [manual, setManual] = useState('')
  const [showManual, setShowManual] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const goNext = () => push('welcome')

  const share = async () => {
    setBusy(true)
    setError('')
    try {
      const shared = await requestContact()
      if (shared) {
        // The bot backend stores the contact; re-read it from bootstrap.
        await refresh()
        haptic.success()
        goNext()
        return
      }
      setShowManual(true)
    } finally {
      setBusy(false)
    }
  }

  const saveManual = async () => {
    const digits = manual.replace(/\D/g, '')
    if (digits.length < 9) {
      setError(t('phone.invalid'))
      haptic.error()
      return
    }
    setBusy(true)
    setError('')
    try {
      await setPhone(`+${digits}`)
      haptic.success()
      goNext()
    } catch {
      setError(t('phone.invalid'))
      haptic.error()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Screen
      title={t('phone.title')}
      text
      main={{
        text: showManual ? t('common.continue') : t('phone.share'),
        onClick: showManual ? saveManual : share,
        loading: busy,
      }}
    >
      <div className="center-col" style={{ paddingTop: 16 }}>
        <div className="plate plate--soft" style={{ width: 60, height: 60, borderRadius: 16 }}>
          <Icon name="phone" size={26} />
        </div>
        <div className="t-h3">{t('phone.title')}</div>
        <div className="t-row t-sub">{t('phone.desc')}</div>
      </div>

      <div className="card card--flat">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="plate" style={{ width: 36, height: 36, borderRadius: '50%', fontSize: 15 }}>
            {(user?.first_name || 'U').slice(0, 1).toUpperCase()}
          </span>
          <div>
            <div className="t-row" style={{ fontWeight: 600 }}>
              {user?.first_name || '—'}
            </div>
            <div className="t-cap num">{user?.phone_masked || t('settings.phoneEmpty')}</div>
          </div>
        </div>
      </div>

      {showManual ? (
        <div className="stack stack--tight">
          <label className="t-cap">{t('phone.manual')}</label>
          <input
            className="field num"
            inputMode="tel"
            placeholder={t('phone.placeholder')}
            value={manual}
            onChange={(event) => setManual(event.target.value)}
            onBlur={() => setManual((value) => formatPhone(value))}
          />
          {error ? <div className="t-cap t-danger">{error}</div> : null}
        </div>
      ) : null}

      <Notice tone="info">{t('phone.hint')}</Notice>

      <button className="link t-center" style={{ marginTop: 'auto' }} onClick={goNext}>
        {t('phone.skip')}
      </button>
    </Screen>
  )
}

/** 03 — welcome + test-mode disclaimer. */
export function WelcomeScreen() {
  const t = useT()
  const reset = useNav((s) => s.reset)

  const features: { icon: 'mortgage' | 'rates' | 'headset'; text: string }[] = [
    { icon: 'mortgage', text: t('welcome.f1') },
    { icon: 'rates', text: t('welcome.f2') },
    { icon: 'headset', text: t('welcome.f3') },
  ]

  return (
    <Screen title={t('welcome.title')} text main={{ text: t('welcome.start'), onClick: () => reset('home') }}>
      <div className="t-h1" style={{ paddingTop: 24 }}>
        {t('welcome.title')}
      </div>
      <p className="t-body" style={{ color: 'var(--tg-sub)', margin: 0 }}>
        {t('welcome.body')}
      </p>
      <div className="stack stack--tight" style={{ paddingTop: 8 }}>
        {features.map((feature) => (
          <div key={feature.icon} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span
              className="row__icon"
              style={{ width: 32, height: 32, borderRadius: 9, flex: 'none' }}
            >
              <Icon name={feature.icon} size={17} />
            </span>
            <span className="t-row">{feature.text}</span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 'auto', paddingTop: 16 }}>
        <Notice tone="warn">{t('welcome.disclaimer')}</Notice>
      </div>
    </Screen>
  )
}
