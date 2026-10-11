/**
 * The application shell: the quiet frame the work sits in.
 *
 * Two decisions come from the design direction. The navigation recedes -- muted
 * text, small icons, no coloured slabs -- so the case in front of the reviewer
 * is the loudest thing on screen. And there is one accent colour, spent on the
 * active location and the primary action, so a glance tells you where "here" is.
 *
 * On narrow screens the rail collapses into a top bar and a drawer. That is the
 * only structural difference: the same items, the same order, reachable by
 * keyboard either way.
 */

import { useEffect, useState, type ComponentType, type ReactNode } from 'react'

import { ApiError } from '../api/client'
import type { Capabilities, CaseStatus } from '../api/types'
import { cn } from './cn'
import { IconInbox, IconList, IconMenu, IconPlus, IconScale, IconX } from './icons'

export type AppRoute =
  | { name: 'queue'; status: CaseStatus | undefined }
  | { name: 'case'; id: string }
  | { name: 'open' }

interface NavItem {
  key: string
  label: string
  icon: ComponentType<{ size?: number }>
  route: AppRoute
  isActive: (route: AppRoute) => boolean
}

const NAV: NavItem[] = [
  {
    key: 'review',
    label: 'Review queue',
    icon: IconInbox,
    route: { name: 'queue', status: 'AWAITING_REVIEW' },
    isActive: (route) => route.name === 'queue' && route.status === 'AWAITING_REVIEW',
  },
  {
    key: 'all',
    label: 'All disputes',
    icon: IconList,
    route: { name: 'queue', status: undefined },
    isActive: (route) => route.name === 'queue' && route.status === undefined,
  },
  {
    key: 'open',
    label: 'Open dispute',
    icon: IconPlus,
    route: { name: 'open' },
    isActive: (route) => route.name === 'open',
  },
]

export function AppShell({
  route,
  capabilities,
  capabilityError,
  onNavigate,
  children,
}: {
  route: AppRoute
  capabilities: Capabilities | null
  capabilityError: ApiError | null
  onNavigate: (route: AppRoute) => void
  children: ReactNode
}) {
  const [menuOpen, setMenuOpen] = useState(false)

  // Close the drawer when the location changes; staying open over the new page
  // would be a small lie about where you are.
  useEffect(() => {
    setMenuOpen(false)
  }, [route])

  useEffect(() => {
    if (!menuOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen])

  return (
    <div className="min-h-screen lg:flex">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:shadow-md"
      >
        Skip to content
      </a>

      {/* Desktop rail */}
      <aside className="hidden lg:flex lg:w-60 lg:shrink-0 lg:flex-col lg:border-r lg:border-line lg:bg-canvas">
        <RailContent route={route} capabilities={capabilities} capabilityError={capabilityError} onNavigate={onNavigate} />
      </aside>

      {/* Mobile bar */}
      <div className="sticky top-0 z-30 flex items-center gap-2 border-b border-line bg-surface px-4 py-3 lg:hidden">
        <button
          type="button"
          onClick={() => setMenuOpen(true)}
          aria-label="Open navigation"
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-muted hover:bg-surface-sunken hover:text-ink"
        >
          <IconMenu size={18} />
        </button>
        <Wordmark />
      </div>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-ink/20 backdrop-blur-[1px]"
            onClick={() => setMenuOpen(false)}
            aria-hidden="true"
          />
          <div className="absolute inset-y-0 left-0 flex w-64 max-w-[85vw] flex-col border-r border-line bg-canvas shadow-md">
            <div className="flex items-center justify-between px-4 py-3">
              <Wordmark />
              <button
                type="button"
                onClick={() => setMenuOpen(false)}
                aria-label="Close navigation"
                className="inline-flex h-8 w-8 items-center justify-center rounded-md text-ink-muted hover:bg-surface-sunken hover:text-ink"
              >
                <IconX size={18} />
              </button>
            </div>
            <RailContent
              route={route}
              capabilities={capabilities}
              capabilityError={capabilityError}
              onNavigate={onNavigate}
            />
          </div>
        </div>
      )}

      <main id="main" className="min-w-0 flex-1">
        <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">{children}</div>
      </main>
    </div>
  )
}

function RailContent({
  route,
  capabilities,
  capabilityError,
  onNavigate,
}: {
  route: AppRoute
  capabilities: Capabilities | null
  capabilityError: ApiError | null
  onNavigate: (route: AppRoute) => void
}) {
  return (
    <div className="flex h-full flex-col px-3 py-4">
      <div className="hidden px-2 pb-5 lg:block">
        <Wordmark />
      </div>

      <nav aria-label="Main" className="space-y-0.5">
        {NAV.map((item) => {
          const active = item.isActive(route)
          const Icon = item.icon
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onNavigate(item.route)}
              aria-current={active ? 'page' : undefined}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[0.8125rem] font-medium transition-colors',
                active
                  ? 'bg-surface text-ink shadow-xs ring-1 ring-line'
                  : 'text-ink-muted hover:bg-surface/60 hover:text-ink',
              )}
            >
              <span className={cn(active ? 'text-accent' : 'text-ink-faint')}>
                <Icon size={16} />
              </span>
              {item.label}
            </button>
          )
        })}
      </nav>

      <div className="mt-auto space-y-2 px-2.5 pt-6 text-xs text-ink-faint">
        {capabilityError && <p className="text-danger-text">API unreachable</p>}
        {capabilities && (
          <p className="leading-relaxed">
            <span className="font-medium text-ink-muted">{capabilities.store}</span> store ·{' '}
            <span className="font-mono">{capabilities.llm_provider}</span> investigator
          </p>
        )}
        {capabilities && (
          <p className="leading-relaxed">
            Acting as <span className="font-medium text-ink-muted">{capabilities.identity.actor_id}</span>{' '}
            ({capabilities.identity.actor_role})
          </p>
        )}
      </div>
    </div>
  )
}

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-soft text-accent">
        <IconScale size={18} />
      </span>
      <span className="leading-tight">
        <span className="block text-sm font-semibold text-ink">ResolveIQ</span>
        <span className="block text-2xs text-ink-faint">reviewer workbench</span>
      </span>
    </div>
  )
}
