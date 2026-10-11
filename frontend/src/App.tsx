/**
 * The application root.
 *
 * Three jobs: fetch the capabilities once and use them to tell the truth about the
 * build on screen, hold the current location, and pass down the one piece of shared
 * state (a revision counter that a write bumps so the queue refetches).
 *
 * There is still no router dependency. A handful of views and a single piece of
 * state do not justify one, and a router would add a thing to configure, a thing to
 * mock in tests, and one more place for the answer to "which case am I looking at"
 * to be wrong.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api, setActor } from './api/client'
import type { Capabilities } from './api/types'
import { AppShell, type AppRoute } from './components/AppShell'
import { CaseDetailView } from './components/CaseDetailView'
import { ErrorPanel } from './components/primitives'
import { CaseQueue } from './pages/CaseQueue'
import { OpenDispute } from './pages/OpenDispute'

export function App() {
  const [route, setRoute] = useState<AppRoute>({ name: 'queue', status: 'AWAITING_REVIEW' })
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [capabilityError, setCapabilityError] = useState<ApiError | null>(null)
  // Bumped after a write so the queue refetches. Passed down as a prop rather than
  // held in context: a context here would re-render the detail view on every queue
  // change for no benefit.
  const [revision, setRevision] = useState(0)

  const loadCapabilities = useCallback(async () => {
    try {
      const loaded = await api.capabilities()
      setCapabilities(loaded)
      setActor(loaded.identity.actor_id, loaded.identity.actor_role)
      setCapabilityError(null)
    } catch (caught) {
      setCapabilityError(
        caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null),
      )
    }
  }, [])

  useEffect(() => {
    void loadCapabilities()
  }, [loadCapabilities])

  return (
    <AppShell
      route={route}
      capabilities={capabilities}
      capabilityError={capabilityError}
      onNavigate={setRoute}
    >
      {capabilityError && (
        <div className="mb-4">
          <ErrorPanel
            title="Could not reach the API"
            message={`${capabilityError.message} The workbench cannot show whether this build is durable, so it will not pretend to.`}
            code={capabilityError.code}
          />
        </div>
      )}
      {capabilities && (
        <div className="mb-4">
          <StoreNotice capabilities={capabilities} />
        </div>
      )}

      {route.name === 'open' ? (
        <OpenDispute
          onOpened={(id) => {
            setRevision((value) => value + 1)
            setRoute({ name: 'case', id })
          }}
        />
      ) : route.name === 'case' ? (
        <CaseDetailView
          key={`${route.id}-${revision}`}
          caseId={route.id}
          actorRole={capabilities?.identity.actor_role ?? 'reviewer'}
          onBack={() => setRoute({ name: 'queue', status: 'AWAITING_REVIEW' })}
          onChanged={() => setRevision((value) => value + 1)}
        />
      ) : (
        <CaseQueue
          key={`queue-${route.status ?? 'all'}-${revision}`}
          status={route.status}
          onSelectStatus={(status) => setRoute({ name: 'queue', status })}
          onSelect={(id) => setRoute({ name: 'case', id })}
        />
      )}
    </AppShell>
  )
}

/**
 * The store and identity notice.
 *
 * Kept above the content rather than in a corner, because "these cases will vanish
 * on restart" and "the reviewer name is a self-declared header" are both things a
 * user must read *before* relying on what is on screen.
 */
function StoreNotice({ capabilities }: { capabilities: Capabilities }) {
  const ephemeral = !capabilities.persistence_is_durable
  const demoIdentity = !capabilities.identity.is_authentication
  if (!ephemeral && !demoIdentity) return null

  return (
    <div className="space-y-1 rounded-lg border border-warning-border bg-warning-bg px-3.5 py-2.5 text-xs text-warning-text">
      {ephemeral && (
        <p>
          <strong>Not durable.</strong> The <code className="font-mono text-[0.6875rem]">{capabilities.store}</code>{' '}
          store is in-process only. Every case below is lost when the server restarts.
        </p>
      )}
      {demoIdentity && (
        <p>
          <strong>Identity is self-declared.</strong> Reviews are labelled{' '}
          <code className="font-mono text-[0.6875rem]">{capabilities.identity.actor_id}</code> (
          {capabilities.identity.actor_role}) from a request header. Nothing is authorised by it.
        </p>
      )}
    </div>
  )
}