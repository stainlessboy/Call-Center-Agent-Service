import { useEffect, useState } from 'react'

import { api, type Office } from '../api'
import { Icon } from '../components/Icon'
import { Screen } from '../components/Screen'
import { KV, Notice, Section, Segments, Skeleton } from '../components/ui'
import { useApp, useNav, useT, type ScreenEntry } from '../store'
import { haptic, openExternal } from '../telegram'

const ALL = 'all'

/** 20 — office list with type filter, search and geolocation. */
export function BranchesScreen() {
  const t = useT()
  const push = useNav((s) => s.push)
  const lang = useApp((s) => s.lang)

  const [items, setItems] = useState<Office[] | null>(null)
  const [types, setTypes] = useState<{ code: string; label: string }[]>([])
  const [type, setType] = useState<string>(ALL)
  const [query, setQuery] = useState('')
  const [geoError, setGeoError] = useState('')
  const [geoBusy, setGeoBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Debounced search — the handoff asks for 300 ms and a 2-character floor.
    const timer = window.setTimeout(
      () => {
        api
          .branches({ type: type === ALL ? undefined : type, q: query.length >= 2 ? query : '' })
          .then((data) => {
            if (cancelled) return
            setItems(data.items)
            setTypes(data.types)
          })
          .catch(() => !cancelled && setItems([]))
      },
      query ? 300 : 0,
    )
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [type, query, lang])

  const findNearest = () => {
    if (!navigator.geolocation) {
      setGeoError(t('branches.geoDenied'))
      return
    }
    setGeoBusy(true)
    setGeoError('')
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        try {
          const data = await api.nearestBranches(position.coords.latitude, position.coords.longitude)
          setItems(data.items)
          haptic.success()
        } catch {
          setGeoError(t('branches.geoDenied'))
        } finally {
          setGeoBusy(false)
        }
      },
      () => {
        setGeoError(t('branches.geoDenied'))
        setGeoBusy(false)
      },
      { timeout: 10000 },
    )
  }

  const segments = [{ value: ALL, label: t('common.all') }, ...types.map((item) => ({ value: item.code, label: item.label }))]

  return (
    <Screen title={t('branches.title')} secondaryBg>
      <div className="row" style={{ borderRadius: 11, minHeight: 42, border: '1px solid var(--tg-sep)' }}>
        <span className="t-hint" style={{ display: 'flex' }}>
          <Icon name="search" size={18} />
        </span>
        <input
          style={{ flex: 1, border: 'none', background: 'transparent', outline: 'none' }}
          placeholder={t('branches.search')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      {segments.length > 1 ? (
        <Segments brand items={segments} value={type} onChange={setType} />
      ) : null}

      <button className="notice notice--brand" style={{ textAlign: 'left' }} onClick={findNearest}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {geoBusy ? <span className="spinner" /> : <Icon name="pin" size={18} />}
          {t('branches.nearest')}
        </span>
      </button>

      {geoError ? <Notice tone="warn">{geoError}</Notice> : null}

      {items === null ? (
        <Skeleton lines={5} />
      ) : items.length === 0 ? (
        <Notice tone="info">{t('branches.empty')}</Notice>
      ) : (
        <div className="stack stack--tight">
          {items.map((office) => (
            <button
              key={office.id}
              className="card card--tap"
              onClick={() => {
                haptic.tap()
                const [officeType, rawId] = office.id.split(':')
                push('branch', { officeType, id: Number(rawId) })
              }}
            >
              <span className={`badge${office.office_type === 'filial' ? ' badge--brand' : ''}`} style={{ alignSelf: 'flex-start' }}>
                {office.office_type_label}
              </span>
              <span style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                <span style={{ flex: 1 }}>
                  <span className="t-title" style={{ display: 'block', fontSize: 16 }}>
                    {office.name}
                  </span>
                  <span className="t-cap">{office.address}</span>
                </span>
                {office.distance_km != null ? (
                  <span className="num" style={{ fontSize: 14, fontWeight: 600 }}>
                    {office.distance_km} {t('common.km')}
                  </span>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      )}

      <button className="link t-center" onClick={() => push('branchTypes')}>
        {t('branches.typesLink')}
      </button>
    </Screen>
  )
}

/** 21 — office detail. */
export function BranchScreen({ entry }: { entry: ScreenEntry }) {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const officeType = String(entry.params?.officeType ?? '')
  const id = Number(entry.params?.id ?? 0)

  const [office, setOffice] = useState<Office | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .branch(officeType, id)
      .then((data) => !cancelled && setOffice(data))
      .catch(() => !cancelled && setOffice(null))
    return () => {
      cancelled = true
    }
  }, [officeType, id, lang])

  if (!office) {
    return (
      <Screen title={t('branches.title')}>
        <Skeleton lines={5} />
      </Screen>
    )
  }

  const mapsUrl =
    office.location_url ||
    (office.latitude != null && office.longitude != null
      ? `https://maps.google.com/?q=${office.latitude},${office.longitude}`
      : '')

  return (
    <Screen
      title={office.name}
      subtitle={office.office_type_label}
      main={mapsUrl ? { text: t('branches.route'), onClick: () => openExternal(mapsUrl) } : undefined}
      secondary={
        office.phone
          ? { text: t('branches.call'), onClick: () => openExternal(`tel:${office.phone}`) }
          : undefined
      }
    >
      <div className="map-tile">
        {mapsUrl ? (
          <span className="badge" style={{ background: 'var(--tg-bg)' }}>
            {t('branches.openMaps')}
          </span>
        ) : null}
      </div>

      <span className={`badge${office.office_type === 'filial' ? ' badge--brand' : ''}`} style={{ alignSelf: 'flex-start' }}>
        {office.office_type_label}
      </span>
      <div className="t-h3">{office.name}</div>

      <div className="card">
        <KV k={t('branches.address')} v={office.landmark ? `${office.address} · ${office.landmark}` : office.address} />
        {office.hours ? <KV k={t('branches.hours')} v={office.hours} /> : null}
        {office.phone ? <KV k={t('branches.phone')} v={office.phone} mono /> : null}
      </div>

      {office.services?.length ? (
        <Section title={t('branches.services')}>
          <div className="chips" style={{ flexWrap: 'wrap' }}>
            {office.services.map((service) => (
              <span key={service.code} className="chip chip--neutral">
                {service.label}
              </span>
            ))}
          </div>
        </Section>
      ) : null}
    </Screen>
  )
}

/** 22 — service matrix: which office type does what. */
export function BranchTypesScreen() {
  const t = useT()
  const lang = useApp((s) => s.lang)
  const [matrix, setMatrix] = useState<Awaited<ReturnType<typeof api.serviceMatrix>> | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .serviceMatrix()
      .then((data) => !cancelled && setMatrix(data))
      .catch(() => !cancelled && setMatrix(null))
    return () => {
      cancelled = true
    }
  }, [lang])

  if (!matrix) {
    return (
      <Screen title={t('branches.typesTitle')}>
        <Skeleton lines={6} />
      </Screen>
    )
  }

  return (
    <Screen title={t('branches.typesTitle')}>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('branches.serviceTable')}</th>
                {matrix.types.map((type) => (
                  <th key={type.code}>{type.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.services.map((service) => (
                <tr key={service.code}>
                  <td>{service.label}</td>
                  {matrix.types.map((type) => (
                    <td
                      key={type.code}
                      style={{
                        color: service.availability[type.code] ? 'var(--ok)' : 'var(--tg-hint)',
                        fontWeight: service.availability[type.code] ? 600 : 400,
                      }}
                    >
                      {service.availability[type.code] ? `✓ ${t('common.yes')}` : `— ${t('common.no')}`}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Screen>
  )
}
