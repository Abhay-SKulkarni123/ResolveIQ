/**
 * Open a dispute from its source records.
 *
 * The backend opens a case from the whole ingest payload, not from an id, because
 * this is the only point at which the records exist. So this form collects exactly
 * that: the dispute, the invoice as submitted, its lines, and -- optionally -- the
 * usage records behind it.
 *
 * Two rules shape the code here. Amounts stay strings end to end: the inputs are
 * text, validation is a regex, and nothing is ever converted to a number. And the
 * form refuses to send an invalid payload rather than letting the server answer
 * with a 422, because a reviewer who has to decode a server error to find a missing
 * field has been failed twice.
 *
 * A contract price term is optional too. When one is entered it is stored as
 * evidence and the engine prices the invoice against it exactly as written -- but
 * writing it down here does not make it contractual, so the form says so beside the
 * fields rather than letting the number masquerade as a verified rate. The term's
 * currency is taken from the invoice, not typed, so a term can never be submitted in
 * a currency the invoice is not in.
 *
 * The remaining ingest collections the endpoint accepts (`payments`,
 * `adjustments`) are not built here; they are supported by the API and can be
 * attached to a live case afterwards.
 *
 * A usage record's `<input type="datetime-local">` value carries no offset, but the
 * API refuses an instant it cannot place in the invoice period, so the wall-clock is
 * written out as an explicit UTC instant before it is sent.
 */

import { useMemo, useState } from 'react'

import { ApiError, api } from '../api/client'
import type {
  DisputeSeverity,
  OpenDisputeRequest,
  PriceTermBillingMode,
  PriceTermInput,
} from '../api/types'
import {
  Alert,
  Button,
  Field,
  IconButton,
  Panel,
  PanelBody,
  PanelHeader,
  Select,
  TextArea,
  TextInput,
} from '../components/ui'
import { ErrorPanel } from '../components/primitives'
import { IconPlus, IconX } from '../components/icons'

const AMOUNT_RE = /^-?\d+(\.\d+)?$/
const NON_NEGATIVE_AMOUNT_RE = /^\d+(\.\d+)?$/
const CURRENCY_RE = /^[A-Z]{3}$/

const PRICE_TERM_MODES: { value: PriceTermBillingMode; label: string }[] = [
  { value: 'PER_UNIT', label: 'Per unit' },
  { value: 'COMMITMENT', label: 'Committed minimum' },
]

const SEVERITIES: { value: DisputeSeverity; label: string }[] = [
  { value: 'LOW', label: 'Low' },
  { value: 'MEDIUM', label: 'Medium' },
  { value: 'HIGH', label: 'High' },
  { value: 'CRITICAL', label: 'Critical' },
]

let rowSeq = 0
function nextRowId(): string {
  rowSeq += 1
  return `row-${rowSeq}`
}

function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `open-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

interface LineDraft {
  id: string
  metric_key: string
  line_type: 'USAGE' | 'FIXED'
  amount: string
  currency: string
}

interface UsageDraft {
  id: string
  external_id: string
  dedupe_key: string
  metric_key: string
  occurred_at: string
  quantity: string
  unit: string
}

interface PriceTermDraft {
  id: string
  metric_key: string
  billing_mode: PriceTermBillingMode
  rate: string
  included_units: string
  minimum_commitment: string
}

interface FormState {
  dispute_external_id: string
  contract_external_id: string
  severity: DisputeSeverity
  dispute_text: string
  description: string
  invoice_external_id: string
  currency: string
  period_start: string
  period_end: string
  stated_total: string
  lines: LineDraft[]
  price_terms: PriceTermDraft[]
  usage_events: UsageDraft[]
}

function blankLine(currency: string): LineDraft {
  return { id: nextRowId(), metric_key: '', line_type: 'USAGE', amount: '', currency }
}

function blankPriceTerm(): PriceTermDraft {
  return {
    id: nextRowId(),
    metric_key: '',
    billing_mode: 'PER_UNIT',
    rate: '',
    included_units: '',
    minimum_commitment: '',
  }
}

function blankUsage(): UsageDraft {
  return {
    id: nextRowId(),
    external_id: '',
    dedupe_key: '',
    metric_key: '',
    occurred_at: '',
    quantity: '',
    unit: '',
  }
}

function initialState(): FormState {
  return {
    dispute_external_id: '',
    contract_external_id: '',
    severity: 'MEDIUM',
    dispute_text: '',
    description: '',
    invoice_external_id: '',
    currency: 'USD',
    period_start: '',
    period_end: '',
    stated_total: '',
    lines: [blankLine('USD')],
    price_terms: [],
    usage_events: [],
  }
}

/**
 * Render a `<input type="datetime-local">` value as the instant the API requires.
 *
 * The control yields a wall-clock with no offset ("2026-03-15T09:00"). The API will
 * not guess the zone -- an instant it cannot place inside the invoice period is a
 * billing error waiting to happen -- and the backend's `datetime.fromisoformat`
 * (Python 3.10) does not read a trailing `Z`. The workbench records every instant in
 * UTC, so the entered wall-clock is read as UTC and the offset is written out.
 */
function toApiInstant(value: string): string {
  if (value === '') return value
  if (/[+-]\d{2}:\d{2}$/.test(value)) return value
  const withSeconds = value.length === 16 ? `${value}:00` : value
  return `${withSeconds}+00:00`
}

function buildPriceTerm(term: PriceTermDraft, currency: string): PriceTermInput {
  const input: PriceTermInput = {
    metric_key: term.metric_key.trim(),
    billing_mode: term.billing_mode,
    currency,
    included_units: term.included_units.trim() === '' ? '0' : term.included_units.trim(),
    unit_price: { amount: term.rate.trim(), currency },
  }
  if (term.billing_mode === 'COMMITMENT') {
    return {
      ...input,
      minimum_commitment: { amount: term.minimum_commitment.trim(), currency },
    }
  }
  return input
}

function buildPayload(form: FormState): OpenDisputeRequest {
  const payload: OpenDisputeRequest = {
    dispute_external_id: form.dispute_external_id.trim(),
    invoice: {
      external_id: form.invoice_external_id.trim(),
      currency: form.currency,
      period_start: form.period_start,
      period_end: form.period_end,
      stated_total: { amount: form.stated_total.trim(), currency: form.currency },
      lines: form.lines.map((line) => ({
        metric_key: line.metric_key.trim(),
        line_type: line.line_type,
        recorded_amount: { amount: line.amount.trim(), currency: line.currency },
      })),
    },
    contract_external_id: form.contract_external_id.trim(),
    dispute_text: form.dispute_text.trim(),
    severity: form.severity,
    description: form.description.trim(),
  }
  if (form.price_terms.length > 0) {
    payload.price_terms = form.price_terms.map((term) => buildPriceTerm(term, form.currency))
  }
  if (form.usage_events.length > 0) {
    payload.usage_events = form.usage_events.map((event) => ({
      external_id: event.external_id.trim(),
      dedupe_key: event.dedupe_key.trim(),
      metric_key: event.metric_key.trim(),
      occurred_at: toApiInstant(event.occurred_at),
      quantity: event.quantity.trim(),
      unit: event.unit.trim(),
    }))
  }
  return payload
}

function validate(form: FormState): { errors: Record<string, string>; payload: OpenDisputeRequest | null } {
  const errors: Record<string, string> = {}

  if (form.dispute_external_id.trim().length < 3) {
    errors.dispute_external_id = 'At least 3 characters.'
  } else if (form.dispute_external_id.trim().length > 64) {
    errors.dispute_external_id = 'At most 64 characters.'
  }
  if (form.contract_external_id.trim() === '') {
    errors.contract_external_id = 'Required.'
  }
  if (form.dispute_text.trim() === '') {
    errors.dispute_text = 'Describe what the customer disputes.'
  }
  if (form.invoice_external_id.trim() === '') {
    errors.invoice_external_id = 'Required.'
  }
  if (!CURRENCY_RE.test(form.currency)) {
    errors.currency = 'A three-letter code, e.g. USD.'
  }
  if (form.period_start === '') {
    errors.period_start = 'Required.'
  }
  if (form.period_end === '') {
    errors.period_end = 'Required.'
  } else if (form.period_start !== '' && form.period_end < form.period_start) {
    errors.period_end = 'The period cannot end before it starts.'
  }
  if (!AMOUNT_RE.test(form.stated_total.trim())) {
    errors.stated_total = 'A plain decimal, e.g. 5021.86.'
  }
  if (form.lines.length === 0) {
    errors.lines = 'At least one invoice line is required.'
  }
  form.lines.forEach((line, index) => {
    const prefix = `line.${line.id}`
    if (line.metric_key.trim() === '') errors[`${prefix}.metric_key`] = `Line ${index + 1}: metric is required.`
    if (!AMOUNT_RE.test(line.amount.trim())) {
      errors[`${prefix}.amount`] = `Line ${index + 1}: a plain decimal is required.`
    }
    if (!CURRENCY_RE.test(line.currency)) {
      errors[`${prefix}.currency`] = `Line ${index + 1}: a three-letter currency code is required.`
    }
  })
  form.price_terms.forEach((term, index) => {
    const prefix = `term.${term.id}`
    const label = `Price term ${index + 1}`
    if (term.metric_key.trim() === '') {
      errors[`${prefix}.metric_key`] = `${label}: a metric is required.`
    }
    if (!NON_NEGATIVE_AMOUNT_RE.test(term.rate.trim())) {
      errors[`${prefix}.rate`] = `${label}: a non-negative decimal rate is required, e.g. 0.0013.`
    }
    if (term.included_units.trim() !== '' && !NON_NEGATIVE_AMOUNT_RE.test(term.included_units.trim())) {
      errors[`${prefix}.included_units`] = `${label}: included units must be a non-negative number.`
    }
    if (
      term.billing_mode === 'COMMITMENT' &&
      !NON_NEGATIVE_AMOUNT_RE.test(term.minimum_commitment.trim())
    ) {
      errors[`${prefix}.minimum_commitment`] = `${label}: a committed minimum is required.`
    }
  })
  form.usage_events.forEach((event, index) => {
    const prefix = `usage.${event.id}`
    const label = `Usage ${index + 1}`
    if (event.external_id.trim() === '') errors[`${prefix}.external_id`] = `${label}: id is required.`
    if (event.dedupe_key.trim() === '') errors[`${prefix}.dedupe_key`] = `${label}: dedupe key is required.`
    if (event.metric_key.trim() === '') errors[`${prefix}.metric_key`] = `${label}: metric is required.`
    if (event.occurred_at === '') errors[`${prefix}.occurred_at`] = `${label}: a timestamp is required.`
    if (!AMOUNT_RE.test(event.quantity.trim())) {
      errors[`${prefix}.quantity`] = `${label}: a plain decimal is required.`
    }
    if (event.unit.trim() === '') errors[`${prefix}.unit`] = `${label}: a unit is required.`
  })

  return {
    errors,
    payload: Object.keys(errors).length === 0 ? buildPayload(form) : null,
  }
}

export function OpenDispute({ onOpened }: { onOpened: (caseId: string) => void }) {
  const [form, setForm] = useState<FormState>(initialState)
  const [idempotencyKey] = useState(newIdempotencyKey)
  const [busy, setBusy] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [serverError, setServerError] = useState<ApiError | null>(null)

  const { errors, payload } = useMemo(() => validate(form), [form])
  const showError = (key: string) => (attempted ? errors[key] : undefined)

  function patch(next: Partial<FormState>) {
    setForm((current) => ({ ...current, ...next }))
  }

  function updateLine(id: string, next: Partial<LineDraft>) {
    setForm((current) => ({
      ...current,
      lines: current.lines.map((line) => (line.id === id ? { ...line, ...next } : line)),
    }))
  }

  function updateUsage(id: string, next: Partial<UsageDraft>) {
    setForm((current) => ({
      ...current,
      usage_events: current.usage_events.map((event) =>
        event.id === id ? { ...event, ...next } : event,
      ),
    }))
  }

  function updatePriceTerm(id: string, next: Partial<PriceTermDraft>) {
    setForm((current) => ({
      ...current,
      price_terms: current.price_terms.map((term) =>
        term.id === id ? { ...term, ...next } : term,
      ),
    }))
  }

  async function submit() {
    setAttempted(true)
    if (payload === null) return
    setBusy(true)
    setServerError(null)
    try {
      const detail = await api.createDispute(payload, idempotencyKey)
      onOpened(detail.id)
    } catch (caught) {
      setServerError(
        caught instanceof ApiError ? caught : new ApiError(0, 'UNKNOWN', String(caught), null, null),
      )
    } finally {
      setBusy(false)
    }
  }

  const errorCount = Object.keys(errors).length

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-ink">Open a dispute</h1>
        <p className="max-w-2xl text-sm leading-relaxed text-ink-muted">
          Record a dispute from its invoice, contract and usage. The case is stored exactly as
          submitted, so what an investigation reads and what a reviewer sees cannot drift apart.
        </p>
      </header>

      {serverError && (
        <ErrorPanel
          title="The dispute could not be opened"
          message={
            serverError.code === 'VALIDATION_FAILED'
              ? `${serverError.message} Check the values below and try again.`
              : serverError.message
          }
          code={serverError.code}
        />
      )}

      {attempted && errorCount > 0 && (
        <p role="alert" className="rounded-lg border border-danger-border bg-danger-bg px-3.5 py-2.5 text-sm text-danger-text">
          {errorCount} field{errorCount === 1 ? '' : 's'} need attention before this dispute can be opened.
        </p>
      )}

      <Panel>
        <PanelHeader
          title="The dispute"
          description="What is being disputed, and who raised it."
        />
        <PanelBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Dispute reference"
              hint="Your own identifier for this dispute."
              error={showError('dispute_external_id')}
              required
            >
              <TextInput
                value={form.dispute_external_id}
                onChange={(event) => patch({ dispute_external_id: event.target.value })}
                placeholder="DSC-000123"
                autoComplete="off"
              />
            </Field>
            <Field label="Contract reference" error={showError('contract_external_id')} required>
              <TextInput
                value={form.contract_external_id}
                onChange={(event) => patch({ contract_external_id: event.target.value })}
                placeholder="CTR-5512"
                autoComplete="off"
              />
            </Field>
          </div>

          <Field label="Severity" hint="How urgent the dispute is for triage.">
            <Select
              value={form.severity}
              onChange={(event) => patch({ severity: event.target.value as DisputeSeverity })}
              className="sm:max-w-48"
            >
              {SEVERITIES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Dispute text"
            hint="The customer's own words, recorded as evidence."
            error={showError('dispute_text')}
            required
          >
            <TextArea
              value={form.dispute_text}
              onChange={(event) => patch({ dispute_text: event.target.value })}
              placeholder="We were billed the wrong rate for API calls."
            />
          </Field>

          <Field label="Internal description" hint="Optional note for the reviewer.">
            <TextInput
              value={form.description}
              onChange={(event) => patch({ description: event.target.value })}
              placeholder="Reference ticket INC-4482."
            />
          </Field>
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="The invoice"
          description="The invoice as submitted, with at least one line."
        />
        <PanelBody className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Invoice reference" error={showError('invoice_external_id')} required>
              <TextInput
                value={form.invoice_external_id}
                onChange={(event) => patch({ invoice_external_id: event.target.value })}
                placeholder="INV-2026-03-0042"
                autoComplete="off"
              />
            </Field>
            <Field label="Currency" error={showError('currency')} required>
              <TextInput
                value={form.currency}
                onChange={(event) => patch({ currency: event.target.value.toUpperCase() })}
                maxLength={3}
                placeholder="USD"
                autoComplete="off"
                className="font-mono uppercase tracking-wide"
              />
            </Field>
            <Field label="Period start" error={showError('period_start')} required>
              <TextInput
                type="date"
                value={form.period_start}
                onChange={(event) => patch({ period_start: event.target.value })}
              />
            </Field>
            <Field label="Period end" error={showError('period_end')} required>
              <TextInput
                type="date"
                value={form.period_end}
                onChange={(event) => patch({ period_end: event.target.value })}
              />
            </Field>
            <Field
              label="Stated total"
              hint="Exactly as it appears on the invoice, never rounded."
              error={showError('stated_total')}
              required
            >
              <TextInput
                value={form.stated_total}
                onChange={(event) => patch({ stated_total: event.target.value })}
                inputMode="decimal"
                placeholder="12480.00"
                className="amount"
                autoComplete="off"
              />
            </Field>
          </div>

          <div className="space-y-3 border-t border-line pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-ink">Invoice lines</p>
                <p className="text-xs text-ink-muted">
                  One line per metric the invoice charges for.
                </p>
              </div>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => patch({ lines: [...form.lines, blankLine(form.currency)] })}
              >
                <IconPlus size={14} />
                Add line
              </Button>
            </div>

            {showError('lines') && (
              <p className="text-xs font-medium text-danger-text">{showError('lines')}</p>
            )}

            {form.lines.map((line, index) => (
              <div
                key={line.id}
                className="grid items-start gap-3 rounded-lg border border-line bg-surface-subtle p-3 sm:grid-cols-[1fr_8rem_1fr_6rem_auto]"
              >
                <Field label={`Metric key ${index + 1}`} error={showError(`line.${line.id}.metric_key`)}>
                  <TextInput
                    value={line.metric_key}
                    onChange={(event) => updateLine(line.id, { metric_key: event.target.value })}
                    placeholder="api_calls"
                    autoComplete="off"
                  />
                </Field>
                <Field label="Type">
                  <Select
                    value={line.line_type}
                    onChange={(event) =>
                      updateLine(line.id, { line_type: event.target.value as 'USAGE' | 'FIXED' })
                    }
                  >
                    <option value="USAGE">Usage</option>
                    <option value="FIXED">Fixed</option>
                  </Select>
                </Field>
                <Field label="Recorded amount" error={showError(`line.${line.id}.amount`)}>
                  <TextInput
                    value={line.amount}
                    onChange={(event) => updateLine(line.id, { amount: event.target.value })}
                    inputMode="decimal"
                    placeholder="12480.00"
                    className="amount"
                    autoComplete="off"
                  />
                </Field>
                <Field label="Currency" error={showError(`line.${line.id}.currency`)}>
                  <TextInput
                    value={line.currency}
                    onChange={(event) =>
                      updateLine(line.id, { currency: event.target.value.toUpperCase() })
                    }
                    maxLength={3}
                    className="font-mono uppercase"
                    autoComplete="off"
                  />
                </Field>
                <div className="flex items-end pt-6">
                  <IconButton
                    label={`Remove line ${index + 1}`}
                    onClick={() => patch({ lines: form.lines.filter((item) => item.id !== line.id) })}
                    disabled={form.lines.length === 1}
                  >
                    <IconX size={15} />
                  </IconButton>
                </div>
              </div>
            ))}
          </div>
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="Contract price terms"
          description="Optional. The rates the engine re-prices the invoice lines against."
          actions={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => patch({ price_terms: [...form.price_terms, blankPriceTerm()] })}
            >
              <IconPlus size={14} />
              Add price term
            </Button>
          }
        />
        {form.price_terms.length === 0 ? (
          <PanelBody>
            <p className="text-sm text-ink-muted">
              No price terms. The engine leaves lines it cannot price unresolved, and the
              investigation is reported as degraded rather than priced with a rate nobody agreed to.
            </p>
          </PanelBody>
        ) : (
          <PanelBody className="space-y-3">
            <Alert tone="info" title="These are test inputs, not a verified contract">
              A price term entered here is stored as evidence and priced exactly as written. It is
              not checked against a signed contract, and supplying one does not establish a real
              contractual rate. The term uses the invoice's currency.
            </Alert>
            {form.price_terms.map((term, index) => (
              <div
                key={term.id}
                className="grid items-start gap-3 rounded-lg border border-line bg-surface-subtle p-3 sm:grid-cols-2 lg:grid-cols-3"
              >
                <Field
                  label={`Price metric ${index + 1}`}
                  error={showError(`term.${term.id}.metric_key`)}
                  required
                >
                  <TextInput
                    value={term.metric_key}
                    onChange={(event) => updatePriceTerm(term.id, { metric_key: event.target.value })}
                    placeholder="api_calls"
                    autoComplete="off"
                  />
                </Field>
                <Field label={`Pricing model ${index + 1}`}>
                  <Select
                    value={term.billing_mode}
                    onChange={(event) =>
                      updatePriceTerm(term.id, {
                        billing_mode: event.target.value as PriceTermBillingMode,
                      })
                    }
                  >
                    {PRICE_TERM_MODES.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field
                  label={`Rate ${index + 1}`}
                  hint="The amount charged per billable unit."
                  error={showError(`term.${term.id}.rate`)}
                  required
                >
                  <TextInput
                    value={term.rate}
                    onChange={(event) => updatePriceTerm(term.id, { rate: event.target.value })}
                    inputMode="decimal"
                    placeholder="0.0013"
                    className="amount"
                    autoComplete="off"
                  />
                </Field>
                <Field
                  label={`Included units ${index + 1}`}
                  hint="Units covered before the rate applies. Defaults to 0."
                  error={showError(`term.${term.id}.included_units`)}
                >
                  <TextInput
                    value={term.included_units}
                    onChange={(event) =>
                      updatePriceTerm(term.id, { included_units: event.target.value })
                    }
                    inputMode="decimal"
                    placeholder="10000"
                    className="amount"
                    autoComplete="off"
                  />
                </Field>
                {term.billing_mode === 'COMMITMENT' && (
                  <Field
                    label={`Committed minimum ${index + 1}`}
                    hint="The floor the customer is committed to."
                    error={showError(`term.${term.id}.minimum_commitment`)}
                    required
                  >
                    <TextInput
                      value={term.minimum_commitment}
                      onChange={(event) =>
                        updatePriceTerm(term.id, { minimum_commitment: event.target.value })
                      }
                      inputMode="decimal"
                      placeholder="1000.00"
                      className="amount"
                      autoComplete="off"
                    />
                  </Field>
                )}
                <div className="flex items-end lg:pt-6">
                  <IconButton
                    label={`Remove price term ${index + 1}`}
                    onClick={() =>
                      patch({
                        price_terms: form.price_terms.filter((item) => item.id !== term.id),
                      })
                    }
                  >
                    <IconX size={15} />
                  </IconButton>
                </div>
              </div>
            ))}
          </PanelBody>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Usage records"
          description="Optional. The metered events the invoice is built from, so the engine can re-price them."
          actions={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => patch({ usage_events: [...form.usage_events, blankUsage()] })}
            >
              <IconPlus size={14} />
              Add usage record
            </Button>
          }
        />
        {form.usage_events.length === 0 ? (
          <PanelBody>
            <p className="text-sm text-ink-muted">
              No usage records attached. The engine will price the invoice lines only.
            </p>
          </PanelBody>
        ) : (
          <PanelBody className="space-y-3">
            {form.usage_events.map((event, index) => (
              <div
                key={event.id}
                className="grid items-start gap-3 rounded-lg border border-line bg-surface-subtle p-3 sm:grid-cols-3"
              >
                <Field label={`External id ${index + 1}`} error={showError(`usage.${event.id}.external_id`)}>
                  <TextInput
                    value={event.external_id}
                    onChange={(e) => updateUsage(event.id, { external_id: e.target.value })}
                    autoComplete="off"
                  />
                </Field>
                <Field label="Dedupe key" error={showError(`usage.${event.id}.dedupe_key`)}>
                  <TextInput
                    value={event.dedupe_key}
                    onChange={(e) => updateUsage(event.id, { dedupe_key: e.target.value })}
                    autoComplete="off"
                  />
                </Field>
                <Field label="Metric key" error={showError(`usage.${event.id}.metric_key`)}>
                  <TextInput
                    value={event.metric_key}
                    onChange={(e) => updateUsage(event.id, { metric_key: e.target.value })}
                    placeholder="api_calls"
                    autoComplete="off"
                  />
                </Field>
                <Field
                  label="Occurred at"
                  hint="Entered and stored in UTC."
                  error={showError(`usage.${event.id}.occurred_at`)}
                >
                  <TextInput
                    type="datetime-local"
                    value={event.occurred_at}
                    onChange={(e) => updateUsage(event.id, { occurred_at: e.target.value })}
                  />
                </Field>
                <Field label="Quantity" error={showError(`usage.${event.id}.quantity`)}>
                  <TextInput
                    value={event.quantity}
                    onChange={(e) => updateUsage(event.id, { quantity: e.target.value })}
                    inputMode="decimal"
                    placeholder="120000"
                    className="amount"
                    autoComplete="off"
                  />
                </Field>
                <div className="grid grid-cols-[1fr_auto] items-end gap-3">
                  <Field label="Unit" error={showError(`usage.${event.id}.unit`)}>
                    <TextInput
                      value={event.unit}
                      onChange={(e) => updateUsage(event.id, { unit: e.target.value })}
                      placeholder="calls"
                      autoComplete="off"
                    />
                  </Field>
                  <IconButton
                    label={`Remove usage record ${index + 1}`}
                    onClick={() =>
                      patch({
                        usage_events: form.usage_events.filter((item) => item.id !== event.id),
                      })
                    }
                  >
                    <IconX size={15} />
                  </IconButton>
                </div>
              </div>
            ))}
          </PanelBody>
        )}
      </Panel>

      <div className="sticky bottom-0 -mx-4 flex items-center justify-end gap-3 border-t border-line bg-canvas/90 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
        <p className="mr-auto text-xs text-ink-faint">
          The case is opened with the records above and nothing more.
        </p>
        <Button variant="primary" onClick={submit} loading={busy} disabled={busy}>
          Open dispute
        </Button>
      </div>
    </div>
  )
}