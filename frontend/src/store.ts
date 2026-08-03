import { create } from 'zustand'

import { api, type Bootstrap, type CalcResult, type Lang, type ProductDetail, type ThemeName } from './api'
import { translate } from './i18n'
import { applyStoredTheme } from './telegram'

/* ── Navigation ──────────────────────────────────────────────────────── */

export type ScreenName =
  | 'language'
  | 'phone'
  | 'welcome'
  | 'home'
  | 'chat'
  | 'catalog'
  | 'qualify'
  | 'results'
  | 'product'
  | 'calc'
  | 'calcResult'
  | 'lead'
  | 'leadSuccess'
  | 'branches'
  | 'branch'
  | 'branchTypes'
  | 'rates'
  | 'links'
  | 'settings'
  | 'history'
  | 'sessions'
  | 'archive'

export interface ScreenEntry {
  screen: ScreenName
  params?: Record<string, unknown>
}

interface NavState {
  stack: ScreenEntry[]
  push: (screen: ScreenName, params?: Record<string, unknown>) => void
  replace: (screen: ScreenName, params?: Record<string, unknown>) => void
  pop: () => void
  reset: (screen: ScreenName, params?: Record<string, unknown>) => void
}

export const useNav = create<NavState>((set) => ({
  stack: [{ screen: 'home' }],
  push: (screen, params) => set((s) => ({ stack: [...s.stack, { screen, params }] })),
  replace: (screen, params) =>
    set((s) => ({ stack: [...s.stack.slice(0, -1), { screen, params }] })),
  pop: () => set((s) => (s.stack.length > 1 ? { stack: s.stack.slice(0, -1) } : s)),
  reset: (screen, params) => set({ stack: [{ screen, params }] }),
}))

export const useCurrentScreen = (): ScreenEntry =>
  useNav((s) => s.stack[s.stack.length - 1])

/* ── App / session ───────────────────────────────────────────────────── */

interface AppState {
  status: 'loading' | 'ready' | 'error'
  error: string | null
  data: Bootstrap | null
  lang: Lang
  load: () => Promise<void>
  setLang: (lang: Lang) => Promise<void>
  setPhone: (phone: string) => Promise<void>
  setTheme: (theme: ThemeName) => Promise<void>
  refresh: () => Promise<void>
}

export const useApp = create<AppState>((set, get) => ({
  status: 'loading',
  error: null,
  data: null,
  lang: 'ru',

  load: async () => {
    set({ status: 'loading', error: null })
    try {
      const data = await api.bootstrap()
      applyStoredTheme(data.user.theme)
      set({ data, lang: data.user.lang, status: 'ready' })
    } catch (error) {
      set({ status: 'error', error: error instanceof Error ? error.message : String(error) })
    }
  },

  refresh: async () => {
    try {
      const data = await api.bootstrap()
      set({ data, lang: data.user.lang })
    } catch {
      /* keep the previous snapshot — refresh is best-effort */
    }
  },

  setLang: async (lang) => {
    set({ lang })
    document.documentElement.lang = lang
    await api.setLanguage(lang)
    const data = get().data
    if (data) set({ data: { ...data, user: { ...data.user, lang } } })
  },

  setTheme: async (theme) => {
    applyStoredTheme(theme)
    const data = get().data
    if (data) set({ data: { ...data, user: { ...data.user, theme } } })
    await api.setTheme(theme)
  },

  setPhone: async (phone) => {
    const result = await api.setPhone(phone)
    const data = get().data
    if (data) {
      set({
        data: {
          ...data,
          user: { ...data.user, phone, phone_masked: result.phone_masked, has_phone: true },
        },
      })
    }
  },
}))

/** Translation bound to the active language. */
export function useT() {
  const lang = useApp((s) => s.lang)
  return (key: string, vars?: Record<string, string | number>) => translate(lang, key, vars)
}

/* ── Qualification flow ──────────────────────────────────────────────── */

export interface QualifyStep {
  nodeKey: string
  optionIndex: number
  label: string
  question: string
}

interface QualifyState {
  category: string | null
  nodeKey: string | null
  answers: Record<string, unknown>
  history: QualifyStep[]
  start: (category: string, entry: string) => void
  answer: (step: QualifyStep, patch: Record<string, unknown>, next: string | null) => void
  goBackTo: (index: number) => void
  clear: () => void
}

export const useQualify = create<QualifyState>((set) => ({
  category: null,
  nodeKey: null,
  answers: {},
  history: [],
  start: (category, entry) => set({ category, nodeKey: entry, answers: {}, history: [] }),
  answer: (step, patch, next) =>
    set((s) => ({
      answers: { ...s.answers, ...patch },
      history: [...s.history, step],
      nodeKey: next,
    })),
  goBackTo: (index) =>
    set((s) => {
      const kept = s.history.slice(0, index)
      // Answers are rebuilt from the kept steps by the screen; here we only
      // rewind the cursor to the question the user tapped.
      return {
        history: kept,
        nodeKey: s.history[index]?.nodeKey ?? s.nodeKey,
      }
    }),
  clear: () => set({ category: null, nodeKey: null, answers: {}, history: [] }),
}))

/* ── Calculator ──────────────────────────────────────────────────────── */

interface CalcState {
  product: ProductDetail | null
  amount: number
  termMonths: number
  downpaymentPct: number | null
  age: number | null
  result: CalcResult | null
  setProduct: (product: ProductDetail) => void
  patch: (patch: Partial<Pick<CalcState, 'amount' | 'termMonths' | 'downpaymentPct' | 'age'>>) => void
  setResult: (result: CalcResult | null) => void
}

/** Sensible starting point so the calculator is never empty on open. */
function defaultAmount(product: ProductDetail): number {
  const min = product.bounds?.amount_min ?? 0
  const max = product.bounds?.amount_max ?? 0
  if (min && max) return Math.round((min + max) / 2 / 1_000_000) * 1_000_000 || min
  if (max) return Math.round(max / 2)
  if (min) return min
  return product.category === 'deposit' ? 10_000_000 : 100_000_000
}

function defaultTerm(product: ProductDetail): number {
  const options = product.bounds?.term_options ?? []
  if (options.length) return options[Math.min(2, options.length - 1)]
  const min = product.bounds?.term_min_months ?? 12
  const max = product.bounds?.term_max_months ?? 36
  return Math.min(Math.max(24, min), max)
}

export const useCalc = create<CalcState>((set) => ({
  product: null,
  amount: 0,
  termMonths: 0,
  downpaymentPct: null,
  age: null,
  result: null,
  setProduct: (product) =>
    set({
      product,
      amount: defaultAmount(product),
      termMonths: defaultTerm(product),
      downpaymentPct: product.bounds?.downpayment_min_pct ?? null,
      age: null,
      result: null,
    }),
  patch: (patch) => set({ ...patch, result: null }),
  setResult: (result) => set({ result }),
}))

/* ── Chat ────────────────────────────────────────────────────────────── */

export type ChatRole = 'user' | 'agent' | 'operator' | 'system'

export interface ChatEntry {
  id: string
  role: ChatRole
  text: string
  pending?: boolean
  failed?: boolean
  at: number
}

export type ChatMode = 'bot' | 'queue' | 'operator'

interface ChatState {
  messages: ChatEntry[]
  quickReplies: string[]
  typing: boolean
  mode: ChatMode
  operatorName: string
  showOperatorButton: boolean
  askRating: boolean
  loaded: boolean
  setAll: (messages: ChatEntry[]) => void
  add: (entry: ChatEntry) => void
  update: (id: string, patch: Partial<ChatEntry>) => void
  setQuickReplies: (replies: string[]) => void
  setTyping: (typing: boolean) => void
  setMode: (mode: ChatMode, operatorName?: string) => void
  setShowOperatorButton: (show: boolean) => void
  setAskRating: (ask: boolean) => void
  setLoaded: (loaded: boolean) => void
}

export const useChat = create<ChatState>((set) => ({
  messages: [],
  quickReplies: [],
  typing: false,
  mode: 'bot',
  operatorName: '',
  showOperatorButton: false,
  askRating: false,
  loaded: false,
  setAll: (messages) => set({ messages }),
  add: (entry) => set((s) => ({ messages: [...s.messages, entry] })),
  update: (id, patch) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)) })),
  setQuickReplies: (quickReplies) => set({ quickReplies }),
  setTyping: (typing) => set({ typing }),
  setMode: (mode, operatorName) =>
    set((s) => ({ mode, operatorName: operatorName ?? s.operatorName })),
  setShowOperatorButton: (showOperatorButton) => set({ showOperatorButton }),
  setAskRating: (askRating) => set({ askRating }),
  setLoaded: (loaded) => set({ loaded }),
}))

/* ── Lead ────────────────────────────────────────────────────────────── */

interface LeadState {
  requestId: string
  newRequestId: () => void
}

export const useLead = create<LeadState>((set) => ({
  requestId: crypto.randomUUID(),
  newRequestId: () => set({ requestId: crypto.randomUUID() }),
}))
