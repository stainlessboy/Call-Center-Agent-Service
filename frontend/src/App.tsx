import { useEffect } from 'react'

import { ErrorState } from './components/ui'
import { BranchScreen, BranchTypesScreen, BranchesScreen } from './screens/Branches'
import { CalcResultScreen, CalculatorScreen } from './screens/Calculator'
import { CatalogScreen, QualifyScreen, ResultsScreen } from './screens/Catalog'
import { ChatScreen } from './screens/Chat'
import { HomeScreen } from './screens/Home'
import { LeadScreen, LeadSuccessScreen } from './screens/Lead'
import { LanguageScreen, PhoneScreen, WelcomeScreen } from './screens/Onboarding'
import { ProductScreen } from './screens/Product'
import { HistoryScreen, LinksScreen, RatesScreen, SettingsScreen } from './screens/Service'
import { ArchiveScreen, SessionsScreen } from './screens/Sessions'
import { useApp, useCurrentScreen, useNav, useT } from './store'
import { closeApp, isTelegram, webApp } from './telegram'

export function App() {
  const status = useApp((s) => s.status)
  const error = useApp((s) => s.error)
  const load = useApp((s) => s.load)
  const data = useApp((s) => s.data)
  const lang = useApp((s) => s.lang)
  const t = useT()

  const stack = useNav((s) => s.stack)
  const pop = useNav((s) => s.pop)
  const reset = useNav((s) => s.reset)
  const entry = useCurrentScreen()

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    document.documentElement.lang = lang
  }, [lang])

  /* First launch goes through onboarding; returning users land on Home. */
  useEffect(() => {
    if (status !== 'ready' || !data) return
    if (!data.user.lang) reset('language')
  }, [status, data, reset])

  /* Native BackButton mirrors the navigation stack depth. */
  useEffect(() => {
    const button = webApp?.BackButton
    if (!isTelegram || !button) return
    const handler = () => {
      if (stack.length > 1) pop()
      else closeApp()
    }
    if (stack.length > 1) button.show()
    else button.hide()
    button.onClick(handler)
    return () => button.offClick(handler)
  }, [stack.length, pop])

  /* Browser fallback: the header arrow dispatches this event. */
  useEffect(() => {
    const handler = () => pop()
    window.addEventListener('miniapp:back', handler)
    return () => window.removeEventListener('miniapp:back', handler)
  }, [pop])

  if (status === 'loading') {
    return <div className="app"><div className="loader-screen">{t('common.loading')}</div></div>
  }

  if (status === 'error') {
    return (
      <div className="app">
        <div className="screen">
          <div className="screen__body">
            <ErrorState
              message={error || t('common.error')}
              retryLabel={t('common.retry')}
              onRetry={() => void load()}
            />
          </div>
        </div>
      </div>
    )
  }

  return <div className="app">{renderScreen(entry)}</div>
}

function renderScreen(entry: ReturnType<typeof useCurrentScreen>) {
  switch (entry.screen) {
    case 'language':
      return <LanguageScreen />
    case 'phone':
      return <PhoneScreen />
    case 'welcome':
      return <WelcomeScreen />
    case 'home':
      return <HomeScreen />
    case 'chat':
      return <ChatScreen entry={entry} />
    case 'catalog':
      return <CatalogScreen />
    case 'qualify':
      return <QualifyScreen entry={entry} />
    case 'results':
      return <ResultsScreen entry={entry} />
    case 'product':
      return <ProductScreen entry={entry} />
    case 'calc':
      return <CalculatorScreen />
    case 'calcResult':
      return <CalcResultScreen />
    case 'lead':
      return <LeadScreen />
    case 'leadSuccess':
      return <LeadSuccessScreen entry={entry} />
    case 'branches':
      return <BranchesScreen />
    case 'branch':
      return <BranchScreen entry={entry} />
    case 'branchTypes':
      return <BranchTypesScreen />
    case 'rates':
      return <RatesScreen />
    case 'links':
      return <LinksScreen />
    case 'settings':
      return <SettingsScreen />
    case 'history':
      return <HistoryScreen />
    case 'sessions':
      return <SessionsScreen />
    case 'archive':
      return <ArchiveScreen entry={entry} />
    default:
      return <HomeScreen />
  }
}
