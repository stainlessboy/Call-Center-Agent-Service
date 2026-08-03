import type { ReactNode } from 'react'

import { haptic } from '../telegram'
import { Icon, type IconName } from './Icon'

export function Section({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="stack stack--tight">
      {title ? <div className="t-section">{title}</div> : null}
      {children}
    </div>
  )
}

interface RowProps {
  icon?: IconName
  title: string
  subtitle?: string
  value?: string
  onClick?: () => void
  chevron?: boolean
  danger?: boolean
  right?: ReactNode
}

export function Row({ icon, title, subtitle, value, onClick, chevron, danger, right }: RowProps) {
  const content = (
    <>
      {icon ? (
        <span className="row__icon">
          <Icon name={icon} size={20} />
        </span>
      ) : null}
      <span className="row__main">
        <span className="row__title">{title}</span>
        {subtitle ? <span className="row__sub">{subtitle}</span> : null}
      </span>
      {value ? <span className="row__value num">{value}</span> : null}
      {right}
      {chevron ? (
        <span className="row__chevron">
          <Icon name="chevron" size={18} />
        </span>
      ) : null}
    </>
  )

  const className = `row${danger ? ' row--danger' : ''}`
  if (!onClick) return <div className={className}>{content}</div>
  return (
    <button
      className={className}
      onClick={() => {
        haptic.tap()
        onClick()
      }}
    >
      {content}
    </button>
  )
}

export function Rows({ children }: { children: ReactNode }) {
  return <div className="rows">{children}</div>
}

export function KV({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="kv">
      <span className="kv__k">{k}</span>
      <span className={`kv__v${mono ? ' num' : ''}`}>{v}</span>
    </div>
  )
}

export function Notice({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'warn' | 'danger' | 'brand'
  children: ReactNode
}) {
  return <div className={`notice notice--${tone}`}>{children}</div>
}

export function Option({
  label,
  selected,
  onClick,
}: {
  label: string
  selected?: boolean
  onClick: () => void
}) {
  return (
    <button
      className={`option${selected ? ' option--selected' : ''}`}
      onClick={() => {
        haptic.select()
        onClick()
      }}
    >
      <span className="option__main option__title">{label}</span>
      {selected ? (
        <span className="option__check">
          <Icon name="check" size={13} />
        </span>
      ) : null}
    </button>
  )
}

export function Chips({
  items,
  onSelect,
  neutral,
}: {
  items: string[]
  onSelect: (value: string, index: number) => void
  neutral?: boolean
}) {
  if (!items.length) return null
  return (
    <div className="chips">
      {items.map((item, index) => (
        <button
          key={`${item}-${index}`}
          className={`chip${neutral ? ' chip--neutral' : ''}`}
          onClick={() => {
            haptic.select()
            onSelect(item, index)
          }}
        >
          {item}
        </button>
      ))}
    </div>
  )
}

export function Segments<T extends string>({
  items,
  value,
  onChange,
  brand,
}: {
  items: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
  brand?: boolean
}) {
  return (
    <div className="segments">
      {items.map((item) => (
        <button
          key={item.value}
          className={`segment${brand ? ' segment--brand' : ''}${
            item.value === value ? ' segment--active' : ''
          }`}
          onClick={() => {
            haptic.select()
            onChange(item.value)
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

export function Presets<T extends string | number>({
  items,
  value,
  onChange,
  render,
}: {
  items: T[]
  value: T | null
  onChange: (value: T) => void
  render?: (value: T) => string
}) {
  return (
    <div className="presets">
      {items.map((item) => (
        <button
          key={String(item)}
          className={`preset${item === value ? ' preset--active' : ''}`}
          onClick={() => {
            haptic.select()
            onChange(item)
          }}
        >
          {render ? render(item) : String(item)}
        </button>
      ))}
    </div>
  )
}

export function Progress({ total, done }: { total: number; done: number }) {
  return (
    <div className="progress">
      {Array.from({ length: Math.max(total, 1) }, (_, index) => (
        <span
          key={index}
          className={`progress__bar${index < done ? ' progress__bar--done' : ''}`}
        />
      ))}
    </div>
  )
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack stack--tight" aria-busy="true">
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className="skel" style={{ width: `${100 - index * 12}%` }} />
      ))}
    </div>
  )
}

export function ErrorState({ message, onRetry, retryLabel }: { message: string; onRetry?: () => void; retryLabel: string }) {
  return (
    <div className="center-col" style={{ padding: '32px 0' }}>
      <div className="plate plate--dashed">
        <Icon name="close" size={26} />
      </div>
      <div className="t-body">{message}</div>
      {onRetry ? (
        <button className="link" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  )
}

export function Toggle({ on, onChange }: { on: boolean; onChange: (value: boolean) => void }) {
  return (
    <button
      className={`toggle${on ? ' toggle--on' : ''}`}
      onClick={() => {
        haptic.select()
        onChange(!on)
      }}
      aria-pressed={on}
    >
      <span className="toggle__knob" />
    </button>
  )
}
