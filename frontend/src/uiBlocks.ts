/**
 * Structured chat cards (`ui_blocks`) the agent publishes alongside its text —
 * see docs/MINIAPP.md "UI blocks" for the authoritative JSON schemas.
 *
 * Every field is optional on purpose: blocks are persisted in the DB, so a
 * loaded history can contain blocks written by an older backend. Renderers
 * must survive any missing field (optional chaining + fallbacks), and
 * `sanitizeUiBlocks` only guarantees the envelope shape, not the data inside.
 */
import type { Lang } from './api'

/* ── Product dict (product_list / product_card / comparison_table) ────── */

export interface BlockRateMatrixEntry {
  income_type?: string | null
  rate_min_pct?: number | null
  rate_max_pct?: number | null
  rate_condition_text?: string | null
  term_min_months?: number | null
  term_max_months?: number | null
  downpayment_min_pct?: number | null
  downpayment_max_pct?: number | null
}

export interface BlockDepositScheduleEntry {
  currency?: string | null
  term_months?: number | null
  term_text?: string | null
  rate_pct?: number | null
  rate_text?: string | null
  min_amount?: number | null
  min_amount_text?: string | null
}

/** One schema for all categories — the field set depends on `category`. */
export interface BlockProduct {
  id?: number | string
  name?: string
  name_en?: string | null
  name_uz?: string | null
  // credit (mortgage / autoloan / microloan / education_credit)
  rate?: string | null // display string
  rate_min_pct?: number | null
  rate_max_pct?: number | null
  term?: string | null // legacy RU display string — prefer numeric fields
  amount?: string | null // legacy RU display string
  amount_min?: number | null
  amount_max?: number | null
  downpayment?: string | null // legacy RU display string
  collateral?: string | null
  purpose?: string | null
  rate_matrix?: BlockRateMatrixEntry[]
  rate_condition_kind?: string | null
  needs_age?: boolean
  needs_downpayment?: boolean
  /** Only present when the list came from recommend_product. */
  reason?: string[]
  // deposit
  rate_pct?: number | null
  term_months?: number | null
  term_min?: number | null
  term_max?: number | null
  min_amount?: string | null
  min_amounts_by_currency?: Record<string, [number | null, string]>
  currency?: string | null
  topup?: string | null
  payout?: string | null
  rate_schedule?: BlockDepositScheduleEntry[]
  // debit_card / fx_card
  network?: string | null
  cashback?: string | null
  issue_fee?: string | null
  annual_fee?: string | null
  delivery?: boolean | null
  validity?: string | null
  reissue_fee?: string | null
  transfer_fee?: string | null
  issuance_time?: string | null
  mobile_order?: boolean | null
  pickup?: boolean | null
  payroll?: boolean | null
}

/* ── Office dict (office_list / office_detail) ─────────────────────────── */

export interface BlockOffice {
  id?: number | string
  office_type?: string | null
  name_ru?: string | null
  name_uz?: string | null
  address_ru?: string | null
  address_uz?: string | null
  region_ru?: string | null
  region_uz?: string | null
  landmark_ru?: string | null
  landmark_uz?: string | null
  location_url?: string | null
  latitude?: number | null
  longitude?: number | null
  phone?: string | null
  hours?: string | null
}

/* ── Block payloads ────────────────────────────────────────────────────── */

export interface ProductListData {
  category?: string | null
  /** "recommend" → products carry reason[]; "qualify_result" → questionnaire result. */
  kind?: 'recommend' | 'qualify_result' | string | null
  products?: BlockProduct[]
}

export interface ProductCardData {
  category?: string | null
  product?: BlockProduct
  /** Client's personal rate, if the profile allowed computing one. */
  personal_rate_pct?: number | null
}

export interface OfficeListData {
  office_type?: string | null
  query?: string | null
  offices?: BlockOffice[]
}

export interface OfficeDetailData {
  office?: BlockOffice
}

export interface RateTableRow {
  code?: string
  name_ru?: string | null
  name_en?: string | null
  name_uz?: string | null
  nominal?: number | null
  rate?: number | null
  diff?: number | null
  icon?: string | null
}

export interface RateTableData {
  date?: string | null
  rates?: RateTableRow[]
}

export interface CalcScheduleRow {
  month?: number
  payment?: number
  principal_part?: number
  interest_part?: number
  balance?: number
}

export interface CalcResultData {
  kind?: 'credit' | 'deposit' | 'affordability' | string
  product_name?: string | null
  amount?: number | null
  // credit
  downpayment?: number | null
  downpayment_pct?: number | null
  principal?: number | null
  rate_pct?: number | null
  term_months?: number | null
  monthly_payment?: number | null
  total_payment?: number | null
  overpayment?: number | null
  schedule?: CalcScheduleRow[]
  schedule_truncated?: boolean
  // deposit
  interest_total?: number | null
  monthly_income?: number | null
  total?: number | null
  // Phase 4 ("Экспертиза") — see docs/MINIAPP.md "calc_result"
  /** monthly_payment / client income, 0..1+; null when income is unknown. */
  dti_ratio?: number | null
  /** true only for what_if_scenario — a side "what if" recalculation. */
  is_hypothetical?: boolean
  // kind === "affordability" (affordability_check tool) — may carry only
  // a bare monthly_payment; every other field below can be null/absent.
  loan_amount?: number | null
  income_monthly?: number | null
  dti_warn_ratio?: number | null
}

export interface ComparisonTableData {
  category?: string | null
  /** Rendering hint — which product fields are comparable for this category. */
  columns?: string[]
  products?: BlockProduct[]
}

/* ── The discriminated union ───────────────────────────────────────────── */

export type UiBlock =
  | { type: 'product_list'; data: ProductListData }
  | { type: 'product_card'; data: ProductCardData }
  | { type: 'office_list'; data: OfficeListData }
  | { type: 'office_detail'; data: OfficeDetailData }
  | { type: 'rate_table'; data: RateTableData }
  | { type: 'calc_result'; data: CalcResultData }
  | { type: 'comparison_table'; data: ComparisonTableData }

const KNOWN_TYPES = new Set<UiBlock['type']>([
  'product_list',
  'product_card',
  'office_list',
  'office_detail',
  'rate_table',
  'calc_result',
  'comparison_table',
])

/**
 * Keep only blocks with a known type and an object `data`. History rows may
 * carry blocks from older/newer backend versions — unknown types are dropped
 * silently rather than breaking the feed.
 */
export function sanitizeUiBlocks(raw: unknown): UiBlock[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((block): block is UiBlock => {
    if (!block || typeof block !== 'object') return false
    const candidate = block as { type?: unknown; data?: unknown }
    return (
      typeof candidate.type === 'string' &&
      KNOWN_TYPES.has(candidate.type as UiBlock['type']) &&
      typeof candidate.data === 'object' &&
      candidate.data !== null
    )
  })
}

/* ── Localization helpers ──────────────────────────────────────────────── */

/** Product name in the app language, falling back to the base (RU) name. */
export function productName(product: BlockProduct | undefined, lang: Lang): string {
  if (!product) return ''
  if (lang === 'en' && product.name_en) return product.name_en
  if (lang === 'uz' && product.name_uz) return product.name_uz
  return product.name ?? ''
}

/** Localized *_ru/*_uz office field — en falls back to ru (no *_en columns). */
export function officeField(
  office: BlockOffice | undefined,
  field: 'name' | 'address' | 'region' | 'landmark',
  lang: Lang,
): string {
  if (!office) return ''
  const ru = office[`${field}_ru`] ?? ''
  const uz = office[`${field}_uz`] ?? ''
  return (lang === 'uz' ? uz || ru : ru || uz) ?? ''
}

/** Maps link: location_url if present, else a maps URL from lat/lng. */
export function officeMapUrl(office: BlockOffice | undefined): string | null {
  if (!office) return null
  if (office.location_url) return office.location_url
  if (office.latitude != null && office.longitude != null) {
    return `https://maps.google.com/?q=${office.latitude},${office.longitude}`
  }
  return null
}

export function rateName(row: RateTableRow, lang: Lang): string {
  const byLang = lang === 'uz' ? row.name_uz : lang === 'en' ? row.name_en : row.name_ru
  return byLang ?? row.name_ru ?? row.code ?? ''
}

/* ── Numeric-first display helpers (RU strings only as fallback) ───────── */

/** min/max over the rate_matrix for term/downpayment (credit products have no
 * top-level numeric term fields — the matrix is the numeric source). */
export function creditTermRange(product: BlockProduct): { min: number | null; max: number | null } {
  let min: number | null = null
  let max: number | null = null
  for (const entry of product.rate_matrix ?? []) {
    if (entry?.term_min_months != null) min = min == null ? entry.term_min_months : Math.min(min, entry.term_min_months)
    if (entry?.term_max_months != null) max = max == null ? entry.term_max_months : Math.max(max, entry.term_max_months)
  }
  return { min, max }
}

export function creditDownpaymentMin(product: BlockProduct): number | null {
  let min: number | null = null
  for (const entry of product.rate_matrix ?? []) {
    if (entry?.downpayment_min_pct != null) {
      min = min == null ? entry.downpayment_min_pct : Math.min(min, entry.downpayment_min_pct)
    }
  }
  return min
}
