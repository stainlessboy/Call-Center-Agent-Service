/** Money and number formatting — thin space between groups, per the handoff. */

const THIN_SPACE = ' '

export function formatMoney(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return Math.round(value)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, THIN_SPACE)
}

export function parseMoney(raw: string): number {
  const digits = raw.replace(/\D/g, '')
  return digits ? Number(digits) : 0
}

export function formatRate(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${value.toFixed(1).replace(/\.0$/, '')}%`
}

export function formatRateRange(min: number | null, max: number | null): string {
  if (min == null && max == null) return '—'
  if (min != null && max != null && Math.abs(min - max) > 0.01) {
    return `${formatRate(min).replace('%', '')}–${formatRate(max)}`
  }
  return formatRate(min ?? max)
}

export function formatDiff(value: number): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}${Math.abs(value).toFixed(2)}`
}

/** BCP-47 tag for the app language — ru/uz are 24-hour, en is not. */
export function localeOf(lang: string): string {
  return lang === 'ru' ? 'ru-RU' : lang === 'uz' ? 'uz-UZ' : 'en-US'
}

export function formatTime(timestamp: number, lang = 'ru'): string {
  return new Date(timestamp).toLocaleTimeString(localeOf(lang), {
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDay(timestamp: number, lang = 'ru'): string {
  return new Date(timestamp).toLocaleDateString(localeOf(lang), {
    day: 'numeric',
    month: 'long',
  })
}

/** "+998 90 123 45 67" from a raw phone string. */
export function formatPhone(raw: string): string {
  const digits = raw.replace(/\D/g, '')
  if (digits.length !== 12) return raw
  return `+${digits.slice(0, 3)} ${digits.slice(3, 5)} ${digits.slice(5, 8)} ${digits.slice(8, 10)} ${digits.slice(10)}`
}
