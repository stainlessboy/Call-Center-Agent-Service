/**
 * Line icons — 1.6 stroke, rounded caps, per the handoff's icon spec.
 * They replace the bot's emoji in chrome; emoji stay only inside bot text.
 */

export type IconName =
  | 'chat'
  | 'mortgage'
  | 'autoloan'
  | 'microloan'
  | 'education'
  | 'deposit'
  | 'card'
  | 'bank'
  | 'pin'
  | 'chevron'
  | 'check'
  | 'search'
  | 'settings'
  | 'phone'
  | 'close'
  | 'arrowUp'
  | 'arrowDown'
  | 'download'
  | 'attach'
  | 'mic'
  | 'send'
  | 'external'
  | 'rates'
  | 'headset'
  | 'back'
  | 'document'

const PATHS: Record<IconName, string> = {
  chat: 'M21 11.5a8.4 8.4 0 0 1-9 8.4L3 21l1.1-4.6A8.4 8.4 0 1 1 21 11.5Z',
  mortgage: 'M3 10.5 12 3l9 7.5M5.5 9.5V20h13V9.5M10 20v-5.5h4V20',
  autoloan: 'M4 16.5v2.5h3v-2.5m10 0V19h3v-2.5M3 16.5h18v-4l-1.8-4.2A2 2 0 0 0 17.4 7H6.6a2 2 0 0 0-1.8 1.3L3 12.5v4Zm3.5-2h.01m10.99 0h.01',
  microloan: 'M12 3v18M16.5 7.5c0-1.7-2-3-4.5-3S7.5 5.8 7.5 7.5s2 2.6 4.5 3 4.5 1.3 4.5 3-2 3-4.5 3-4.5-1.3-4.5-3',
  education: 'M3 9.5 12 5l9 4.5-9 4.5-9-4.5Zm3.5 2v5c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5v-5M20 10v5',
  deposit: 'M4 8h16v11H4V8Zm0 0 2.5-4h11L20 8M9 13h6',
  card: 'M3 7.5A1.5 1.5 0 0 1 4.5 6h15A1.5 1.5 0 0 1 21 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 16.5v-9Zm0 3.5h18M6.5 14.5h3',
  bank: 'M3 9.5 12 4l9 5.5M5 10v9m4-9v9m6-9v9m4-9v9M3 20h18',
  pin: 'M12 21s7-5.5 7-11a7 7 0 1 0-14 0c0 5.5 7 11 7 11Zm0-8.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
  chevron: 'm9 5 7 7-7 7',
  check: 'm4.5 12.5 5 5 10-11',
  search: 'M11 18.5a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15Zm5.5-2 4 4',
  settings:
    'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm8-3.5-.1-1.3 1.8-1.4-1.8-3.1-2.2.7-2.1-1.2L15.2 3H8.8l-.4 2.7-2.1 1.2-2.2-.7L2.3 9.3l1.8 1.4a9 9 0 0 0 0 2.6l-1.8 1.4 1.8 3.1 2.2-.7 2.1 1.2.4 2.7h6.4l.4-2.7 2.1-1.2 2.2.7 1.8-3.1-1.8-1.4.1-1.3Z',
  phone: 'M6.5 3.5h3l1.5 4-2 1.5a12 12 0 0 0 6 6l1.5-2 4 1.5v3a2 2 0 0 1-2.2 2A17 17 0 0 1 4.5 5.7a2 2 0 0 1 2-2.2Z',
  close: 'm6 6 12 12M18 6 6 18',
  arrowUp: 'M12 19V5m0 0-6 6m6-6 6 6',
  arrowDown: 'M12 5v14m0 0 6-6m-6 6-6-6',
  download: 'M12 4v11m0 0 4-4m-4 4-4-4M4.5 19h15',
  attach: 'M20 11.5 12 19.5a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7.5-7.5',
  mic: 'M12 15.5a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 1 0-7 0v6a3.5 3.5 0 0 0 3.5 3.5Zm-6-4v.5a6 6 0 0 0 12 0v-.5M12 19v2.5',
  send: 'M4 12 21 4l-8 17-2-7-7-2Z',
  external: 'M14 4h6v6m0-6L10.5 13.5M18 14v4.5A1.5 1.5 0 0 1 16.5 20h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10',
  rates: 'M4 18.5 9 12l3.5 3.5L20 6M20 6h-4.5M20 6v4.5',
  headset:
    'M4 16v-4a8 8 0 0 1 16 0v4m-16 0a2 2 0 0 0 2 2h1v-6H6a2 2 0 0 0-2 2Zm16 0a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2Z',
  back: 'm14 5-7 7 7 7',
  document: 'M14 3v5h5M14 3H7.5A1.5 1.5 0 0 0 6 4.5v15A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V8l-4-5Z',
}

interface IconProps {
  name: IconName
  size?: number
  className?: string
}

export function Icon({ name, size = 22, className }: IconProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}

/** Category → icon, mirroring the emoji map the bot uses. */
export const CATEGORY_ICONS: Record<string, IconName> = {
  mortgage: 'mortgage',
  autoloan: 'autoloan',
  microloan: 'microloan',
  education_credit: 'education',
  deposit: 'deposit',
  debit_card: 'card',
  fx_card: 'card',
}
