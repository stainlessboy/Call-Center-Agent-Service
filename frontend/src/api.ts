/** Typed client for the Mini App API (see app/miniapp/routes). */
import { rawInitData } from './telegram'

const BASE = '/api/miniapp'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body) headers.set('Content-Type', 'application/json')
  // The convention used by Telegram Mini App backends: the signed launch
  // params travel in Authorization with a `tma` scheme.
  if (rawInitData) headers.set('Authorization', `tma ${rawInitData}`)

  const response = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

const get = <T>(path: string) => request<T>(path)
const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

/* ── Types ───────────────────────────────────────────────────────────── */

export type Lang = 'ru' | 'uz' | 'en'
export type ThemeName = 'auto' | 'light' | 'dark'

export interface Bootstrap {
  user: {
    telegram_user_id: number
    first_name: string
    last_name: string
    username: string
    photo_url: string
    phone: string
    phone_masked: string
    has_phone: boolean
    lang: Lang
    theme: ThemeName
  }
  session: { id: string | null; human_mode: boolean; started_at: string | null }
  limits: { daily_used: number; daily_max: number; max_message_length: number }
  operator: {
    enabled: boolean
    hours_enforced: boolean
    start_hour: number
    end_hour: number
    tz_offset: number
    is_open_now: boolean
  }
  langs: Lang[]
  dev_mode: boolean
}

export interface CatalogCategory {
  category: string
  title: string
  subtitle: string
  has_qualify: boolean
}

export interface CatalogGroup {
  key: string
  title: string
  categories: CatalogCategory[]
}

export interface ProductSummary {
  id: string
  category: string
  name: string
  rate_text: string
  rate_min_pct: number | null
  rate_max_pct: number | null
  rate_pct?: number | null
  amount_text?: string
  term_text?: string
  downpayment_text?: string
  needs_age?: boolean
  currency?: string
  term_min?: number | null
  term_max?: number | null
  min_amount_text?: string
  network?: string
  cashback?: string
  issue_fee?: string
  annual_fee?: string
}

export interface ProductBounds {
  amount_min: number | null
  amount_max: number | null
  term_min_months: number | null
  term_max_months: number | null
  term_options: number[]
  downpayment_min_pct: number | null
  downpayment_max_pct: number | null
}

export interface RateMatrixEntry {
  income_type: string | null
  rate_min_pct: number | null
  rate_max_pct: number | null
  condition_text: string
  term_min_months: number | null
  term_max_months: number | null
  downpayment_min_pct: number | null
  downpayment_max_pct: number | null
}

export interface DepositScheduleEntry {
  currency: string
  term_months: number | null
  term_text: string
  rate_pct: number | null
  min_amount: number | null
  min_amount_text: string
}

export interface ProductDetail extends ProductSummary {
  category_label: string
  purpose?: string
  collateral?: string
  rate_low_pct?: number | null
  rate_high_pct?: number | null
  rate_matrix?: RateMatrixEntry[]
  rate_schedule?: DepositScheduleEntry[]
  currencies?: string[]
  topup?: string
  payout?: string
  bounds?: ProductBounds
  validity?: string
  reissue_fee?: string
  transfer_fee?: string
  issuance_time?: string
  delivery?: boolean | null
  pickup?: boolean | null
  mobile_order?: boolean | null
  payroll?: boolean | null
}

export interface QualifyOption {
  index: number
  label: string
  set: Record<string, unknown>
  goto: string | null
}

export interface QualifyNode {
  key: string
  type: 'question' | 'filter' | 'dead_end'
  question?: string
  options?: QualifyOption[]
  message?: string
}

export interface QualifyTree {
  category: string
  category_label: string
  entry: string
  max_steps: number
  nodes: Record<string, QualifyNode>
}

export interface CalcAdjustment {
  field: 'amount' | 'term_months' | 'downpayment_pct'
  requested: number
  applied: number
  reason: 'min' | 'max' | 'not_available'
}

export interface CalcResult {
  kind: 'credit' | 'deposit'
  amount: number
  principal?: number
  downpayment_pct?: number | null
  downpayment_amount?: number
  term_months: number
  rate_pct: number
  monthly_payment?: number
  total_payment?: number
  overpayment?: number
  income?: number
  monthly_income?: number
  total?: number
  adjustments: CalcAdjustment[]
  product: { id: string; category: string; name: string }
}

export interface ScheduleRow {
  month: number
  payment: number
  principal_part: number
  interest_part: number
  balance: number
}

export interface Office {
  id: string
  office_type: string
  office_type_label: string
  name: string
  address: string
  landmark: string
  location_url: string
  latitude: number | null
  longitude: number | null
  phone: string
  hours: string
  distance_km: number | null
  services?: { code: string; label: string }[]
}

export interface RateItem {
  code: string
  name: string
  icon: string
  nominal: string
  rate: number | null
  rate_text: string
  diff: number
  date: string
}

export type SessionState = 'active' | 'ended' | 'expired'

export interface SessionSummary {
  id: string
  title: string
  state: SessionState
  is_current: boolean
  human_mode: boolean
  message_count: number
  started_at: string | null
  ended_at: string | null
  expires_at: string | null
  readable_until: string | null
  closed_reason: string
  feedback_rating: number | null
}

export interface SessionDetail extends SessionSummary {
  writable: boolean
  messages: ChatMessage[]
}

export interface ChatMessage {
  id: number
  role: string
  text: string
  created_at: string | null
}

export interface CalcPayload {
  product_id: string
  amount: number
  term_months: number
  downpayment_pct?: number | null
  age?: number | null
  income_type?: string | null
}

/* ── Endpoints ───────────────────────────────────────────────────────── */

export const api = {
  bootstrap: () => get<Bootstrap>('/bootstrap'),
  setLanguage: (lang: Lang) => post<{ ok: boolean; lang: Lang }>('/settings/language', { lang }),
  setTheme: (theme: ThemeName) => post<{ ok: boolean; theme: ThemeName }>('/settings/theme', { theme }),
  sessions: (limit = 20) =>
    get<{
      items: SessionSummary[]
      archive_days: number
      ttl_minutes: number
      counts: { active: number; ended: number; expired: number }
    }>(`/sessions?limit=${limit}`),
  session: (id: string) => get<SessionDetail>(`/sessions/${encodeURIComponent(id)}`),
  setPhone: (phone: string) => post<{ ok: boolean; phone_masked: string }>('/settings/phone', { phone }),
  sessionHistory: () =>
    get<{ session_id: string | null; started_at?: string; items: ChatMessage[] }>('/session/history'),
  endSession: () => post<{ ok: boolean }>('/session/end', {}),

  catalog: () => get<{ groups: CatalogGroup[] }>('/catalog'),
  products: (category: string) =>
    get<{ category: string; category_label: string; items: ProductSummary[] }>(
      `/products?category=${encodeURIComponent(category)}`,
    ),
  product: (id: string) => get<ProductDetail>(`/products/${encodeURIComponent(id)}`),

  qualifyTree: (category: string) => get<QualifyTree>(`/qualify/${encodeURIComponent(category)}/tree`),
  qualifyResult: (category: string, answers: Record<string, unknown>) =>
    post<{ category: string; items: ProductSummary[]; best_id: string | null; empty_message: string }>(
      '/qualify/result',
      { category, answers },
    ),

  calc: (payload: CalcPayload) => post<CalcResult>('/calc', payload),
  schedule: (payload: CalcPayload, preview = true) =>
    post<{
      total_rows: number
      rows: ScheduleRow[]
      monthly_payment: number
      total_payment: number
      overpayment: number
      rate_pct: number
    }>(`/calc/schedule?preview=${preview}`, payload),
  schedulePdfUrl: (payload: CalcPayload) => {
    const params = new URLSearchParams({
      product_id: payload.product_id,
      amount: String(payload.amount),
      term_months: String(payload.term_months),
    })
    if (payload.downpayment_pct != null) params.set('downpayment_pct', String(payload.downpayment_pct))
    if (payload.age != null) params.set('age', String(payload.age))
    if (payload.income_type) params.set('income_type', payload.income_type)
    return `${BASE}/calc/schedule.pdf?${params.toString()}`
  },

  createLead: (payload: {
    product_id: string
    name: string
    phone: string
    amount?: number | null
    term_months?: number | null
    rate_pct?: number | null
    consent: boolean
    client_request_id: string
  }) =>
    post<{ ok: boolean; duplicate: boolean; lead_id: number; lead_number: string; status: string }>(
      '/leads',
      payload,
    ),

  branches: (params: { type?: string; q?: string; limit?: number } = {}) => {
    const search = new URLSearchParams()
    if (params.type) search.set('type', params.type)
    if (params.q) search.set('q', params.q)
    if (params.limit) search.set('limit', String(params.limit))
    const qs = search.toString()
    return get<{ items: Office[]; types: { code: string; label: string }[] }>(
      `/branches${qs ? `?${qs}` : ''}`,
    )
  },
  nearestBranches: (lat: number, lon: number) =>
    get<{ items: Office[] }>(`/branches/nearest?lat=${lat}&lon=${lon}`),
  branch: (officeType: string, id: number) => get<Office>(`/branches/${officeType}/${id}`),
  serviceMatrix: () =>
    get<{
      types: { code: string; label: string }[]
      services: { code: string; label: string; availability: Record<string, boolean> }[]
    }>('/branches/service-matrix'),

  rates: (top?: number) =>
    get<{ items: RateItem[]; updated_at: string; coming_soon: { key: string; label: string }[] }>(
      `/rates${top ? `?top=${top}` : ''}`,
    ),
  links: () =>
    get<{
      apps: { key: string; label: string; url: string }[]
      socials: { key: string; label: string; url: string }[]
      contacts: { call_center: string; trust_line: string }
    }>('/links'),

  chatHistory: () =>
    get<{ session_id: string | null; human_mode: boolean; messages: ChatMessage[] }>('/chat/history'),
  sendMessage: (text: string, sessionId?: string) =>
    post<{
      blocked: string | null
      text: string
      quick_replies: string[]
      show_operator_button: boolean
      human_mode: boolean
      session_id?: string
      has_pdf: boolean
      suggested_language: string | null
    }>('/chat/message', sessionId ? { text, session_id: sessionId } : { text }),
  toggleOperator: (enabled: boolean) =>
    post<{ ok: boolean; mode?: string; reason?: string; message?: string; already?: boolean }>(
      '/chat/operator',
      { enabled },
    ),
  rateOperator: (rating: number, comment?: string) =>
    post<{ ok: boolean }>('/chat/rating', { rating, comment }),
}

export function eventsSocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const params = rawInitData ? `?init_data=${encodeURIComponent(rawInitData)}` : ''
  return `${protocol}://${window.location.host}${BASE}/ws${params}`
}
