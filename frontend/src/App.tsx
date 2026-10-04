/**
 * The application shell.
 *
 * Two jobs: fetch the capabilities once and use them to tell the user the truth about
 * the build they are looking at, and route between the queue and a case.
 *
 * There is no router dependency. Two views and a single piece of state do not justify
 * one, and adding it would mean another thing to configure, another thing to mock in
 * tests, and one more place for the "which case am I looking at" answer to be wrong.
 */

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api, setActor } from './api/client'
import type { Capabilities } from './api/types'
import { CaseDetailView } from './components/CaseDetailView'
import { ErrorPanel } from './components/primitives'
import { CaseQueue } from './pages/CaseQueue'

type Route = { name: 'queue' } | { name: 'case'; id: string }

export function App() {
  const [route, setRoute] = useState<Route>({ name: 'queue' })
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [capabilityError, setCapabilityError] = useState<ApiError | null>(null)
  // Bumped after a write so the queue refetches. Passed down as a prop rather than
  // held in context: one caller, one level deep, and a context here would be a
  // subscription that re-renders the detail view on every queue change.
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
    <div className="min-h-screen">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3">
          <button
            type="button"
            onClick={() => setRoute({ name: 'queue' })}
            className="text-sm font-semibold"
          >
            ResolveIQ
          </button>
          <span className="text-xs text-ink-faint">reviewer workbench</span>
          {route.name === 'case' && (
            <button
              type="button"
              onClick={() => setRoute({ name: 'queue' })}
              className="ml-auto rounded border border-line px-2 py-1 text-xs hover:bg-gray-50"
            >
              Back to queue
            </button>
          )}
        </div>
      </header>

      {capabilityError && (
        <div className="mx-auto max-w-5xl px-4 pt-4">
          <ErrorPanel
            title="Could not reach the API"
            message={`${capabilityError.message} The workbench cannot show whether this build is durable, so it will not pretend to.`}
            code={capabilityError.code}
          />
        </div>
      )}

      <main className="mx-auto max-w-5xl space-y-4 px-4 py-6">
        {capabilities && <StoreNotice capabilities={capabilities} />}
        {route.name === 'queue' ? (
          <CaseQueue
            key={revision}
            onSelect={(id) => setRoute({ name: 'case', id })}
          />
        ) : (
          <CaseDetailView
            key={`${route.id}-${revision}`}
            caseId={route.id}
            onChanged={() => setRevision((value) => value + 1)}
          />
        )}
      </main>
    </div>
  )
}

/**
 * The store and identity notice.
 *
 * Kept above the content rather than in a corner, because "these cases will vanish on
 * restart" and "the reviewer name is a self-declared header" are both things a user
 * must read *before* relying on what is on screen.
 */
function StoreNotice({ capabilities }: { capabilities: Capabilities }) {
  const ephemeral = !capabilities.persistence_is_durable
  const demoIdentity = !capabilities.identity.is_authentication
  if (!ephemeral && !demoIdentity) return null

  return (
    <div className="space-y-1 rounded border border-warn-border bg-warn-bg px-3 py-2 text-xs text-warn-text">
      {ephemeral && (
        <p>
          <strong>Not durable.</strong> The <code className="font-mono">{capabilities.store}</code>{' '}
          store is in-process only. Every case below is lost when the server restarts.
        </p>
      )}
      {demoIdentity && (
        <p>
          <strong>Identity is self-declared.</strong> Reviews are labelled{' '}
          <code className="font-mono">{capabilities.identity.actor_id}</code> (
          {capabilities.identity.actor_role}) from a request header. Nothing is authorised
          by it.
        </p>
      )}
    </div>
  )
}
