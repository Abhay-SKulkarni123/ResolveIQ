/**
 * The shared interface kit.
 *
 * These are the low-level, reusable pieces — buttons, fields, badges, panels,
 * alerts. They own the visual language (spacing, borders, focus, motion) so the
 * feature components can describe *what* they show, not *how* it looks.
 */

import {
  createContext,
  useContext,
  useId,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'

import { cn } from './cn'

/* -------------------------------------------------------------------------- */
/* Button                                                                     */
/* -------------------------------------------------------------------------- */

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type ButtonSize = 'sm' | 'md'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  block?: boolean
}

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-white border border-transparent shadow-xs hover:bg-accent-hover active:bg-accent-active',
  secondary:
    'bg-surface text-ink border border-line-strong shadow-xs hover:bg-surface-sunken active:bg-canvas',
  ghost: 'bg-transparent text-ink-muted border border-transparent hover:bg-surface-sunken hover:text-ink',
  danger:
    'bg-surface text-danger-text border border-danger-border shadow-xs hover:bg-danger-bg active:bg-danger-bg',
}

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[0.8125rem] gap-1.5 rounded-md',
  md: 'h-9 px-3.5 text-sm gap-2 rounded-md',
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  block = false,
  className,
  disabled,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        BUTTON_VARIANTS[variant],
        BUTTON_SIZES[size],
        block && 'w-full',
        className,
      )}
      {...props}
    >
      {loading && <IconGlyphSpinner />}
      {children}
    </button>
  )
}

function IconGlyphSpinner() {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} className="animate-spin" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
      <path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  )
}

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required: an icon-only control must still announce itself. */
  label: string
  variant?: ButtonVariant
}

export function IconButton({ label, variant = 'ghost', className, children, ...props }: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}

/* -------------------------------------------------------------------------- */
/* Panel / Card                                                               */
/* -------------------------------------------------------------------------- */

export function Panel({
  className,
  children,
  as: Tag = 'section',
}: {
  className?: string
  children: ReactNode
  as?: 'section' | 'div' | 'article'
}) {
  return (
    <Tag className={cn('rounded-lg border border-line bg-surface shadow-xs', className)}>
      {children}
    </Tag>
  )
}

export function PanelHeader({
  title,
  description,
  actions,
  icon,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  icon?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex flex-col gap-3 border-b border-line px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between',
        className,
      )}
    >
      <div className="flex items-start gap-3">
        {icon && <span className="mt-0.5 text-ink-faint">{icon}</span>}
        <div>
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          {description && <p className="mt-0.5 text-[0.8125rem] text-ink-muted">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function PanelBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('px-4 py-4', className)}>{children}</div>
}

/* -------------------------------------------------------------------------- */
/* Badge                                                                      */
/* -------------------------------------------------------------------------- */

export type BadgeTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'accent'

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: 'bg-neutral-bg text-neutral-text border-neutral-border',
  info: 'bg-info-bg text-info-text border-info-border',
  success: 'bg-success-bg text-success-text border-success-border',
  warning: 'bg-warning-bg text-warning-text border-warning-border',
  danger: 'bg-danger-bg text-danger-text border-danger-border',
  accent: 'bg-accent-soft text-accent-text border-accent-border',
}

const DOT_TONES: Record<BadgeTone, string> = {
  neutral: 'bg-neutral-text',
  info: 'bg-info-solid',
  success: 'bg-success-solid',
  warning: 'bg-warning-solid',
  danger: 'bg-danger-solid',
  accent: 'bg-accent',
}

export function Badge({
  tone = 'neutral',
  dot = true,
  className,
  title,
  children,
}: {
  tone?: BadgeTone
  dot?: boolean
  className?: string | undefined
  /** A tooltip for a badge whose meaning deserves an explanation. */
  title?: string
  children: ReactNode
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-2xs font-medium',
        BADGE_TONES[tone],
        className,
      )}
    >
      {dot && <span className={cn('h-1.5 w-1.5 rounded-full', DOT_TONES[tone])} aria-hidden="true" />}
      {children}
    </span>
  )
}

/* -------------------------------------------------------------------------- */
/* Alert                                                                      */
/* -------------------------------------------------------------------------- */

export type AlertTone = 'info' | 'success' | 'warning' | 'danger'

const ALERT_TONES: Record<AlertTone, { wrap: string; icon: string }> = {
  info: { wrap: 'bg-info-bg border-info-border text-info-text', icon: 'text-info-solid' },
  success: { wrap: 'bg-success-bg border-success-border text-success-text', icon: 'text-success-solid' },
  warning: { wrap: 'bg-warning-bg border-warning-border text-warning-text', icon: 'text-warning-solid' },
  danger: { wrap: 'bg-danger-bg border-danger-border text-danger-text', icon: 'text-danger-solid' },
}

export function Alert({
  tone = 'info',
  title,
  children,
  className,
  live = false,
}: {
  tone?: AlertTone
  title?: ReactNode
  children?: ReactNode
  className?: string
  live?: boolean
}) {
  const toneStyles = ALERT_TONES[tone]
  return (
    <div
      role={live ? 'status' : undefined}
      aria-live={live ? 'polite' : undefined}
      className={cn('flex gap-2.5 rounded-lg border px-3.5 py-3 text-[0.8125rem]', toneStyles.wrap, className)}
    >
      <AlertIcon tone={tone} className={cn('mt-0.5 shrink-0', toneStyles.icon)} />
      <div className="min-w-0">
        {title && <p className="font-semibold">{title}</p>}
        {children && <div className={cn(title ? 'mt-0.5' : undefined, 'leading-relaxed')}>{children}</div>}
      </div>
    </div>
  )
}

function AlertIcon({ tone, className }: { tone: AlertTone; className?: string }) {
  if (tone === 'success') {
    return (
      <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden="true" className={className}>
        <circle cx="12" cy="12" r="9" />
        <path d="m8.5 12 2.5 2.5 4.5-5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  }
  if (tone === 'danger') {
    return (
      <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden="true" className={className}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v5M12 16h.01" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  }
  if (tone === 'warning') {
    return (
      <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden="true" className={className}>
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" strokeLinejoin="round" />
        <path d="M12 9v4M12 17h.01" strokeLinecap="round" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden="true" className={className}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" strokeLinecap="round" />
    </svg>
  )
}

/* -------------------------------------------------------------------------- */
/* Form fields                                                                */
/* -------------------------------------------------------------------------- */

const FieldContext = createContext<{ id: string; describedBy?: string | undefined } | null>(null)

export function Field({
  label,
  hint,
  error,
  required,
  children,
  hideLabel = false,
  className,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  children: ReactNode
  hideLabel?: boolean
  className?: string
}) {
  const reactId = useId()
  const id = `field-${reactId}`
  const hintId = hint ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined
  return (
    <FieldContext.Provider value={{ id, describedBy }}>
      <div className={cn('flex flex-col gap-1.5', className)}>
        <label
          htmlFor={id}
          className={cn(
            'text-[0.8125rem] font-medium text-ink',
            hideLabel && 'sr-only',
          )}
        >
          {label}
          {required && (
            <span
              aria-hidden="true"
              title="Required"
              className="ml-0.5 text-danger-text after:content-['*']"
            />
          )}
        </label>
        {children}
        {hint && (
          <p id={hintId} className="text-xs text-ink-muted">
            {hint}
          </p>
        )}
        {error && (
          <p id={errorId} className="text-xs font-medium text-danger-text">
            {error}
          </p>
        )}
      </div>
    </FieldContext.Provider>
  )
}

function useFieldContext() {
  return useContext(FieldContext)
}

const CONTROL_BASE =
  'w-full rounded-md border bg-surface px-2.5 text-sm text-ink placeholder:text-ink-faint ' +
  'transition-colors disabled:cursor-not-allowed disabled:bg-surface-sunken disabled:text-ink-faint ' +
  'focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30'

export function TextInput({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  const field = useFieldContext()
  return (
    <input
      id={field?.id}
      aria-describedby={field?.describedBy}
      className={cn(CONTROL_BASE, 'h-9 border-line-strong', className)}
      {...props}
    />
  )
}

export function TextArea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const field = useFieldContext()
  return (
    <textarea
      id={field?.id}
      aria-describedby={field?.describedBy}
      className={cn(CONTROL_BASE, 'min-h-[4.5rem] border-line-strong py-2 leading-relaxed', className)}
      {...props}
    />
  )
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  const field = useFieldContext()
  return (
    <select
      id={field?.id}
      aria-describedby={field?.describedBy}
      className={cn(CONTROL_BASE, 'h-9 border-line-strong pr-8', className)}
      {...props}
    >
      {children}
    </select>
  )
}

/* -------------------------------------------------------------------------- */
/* Disclosure                                                                 */
/* -------------------------------------------------------------------------- */

export function Disclosure({
  summary,
  children,
  className,
  defaultOpen = false,
  tone = 'default',
}: {
  summary: ReactNode
  children: ReactNode
  className?: string
  defaultOpen?: boolean
  tone?: 'default' | 'plain'
}) {
  return (
    <details open={defaultOpen} className={cn('group', className)}>
      <summary
        className={cn(
          'flex cursor-pointer list-none items-center gap-1.5 text-[0.8125rem] font-medium text-ink-muted transition-colors hover:text-ink',
          '[&::-webkit-details-marker]:hidden',
          tone === 'default' && 'rounded-md px-1 -mx-1',
        )}
      >
        <svg
          viewBox="0 0 24 24"
          width={14}
          height={14}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
          className="shrink-0 transition-transform group-open:rotate-90"
        >
          <path d="m9 6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {summary}
      </summary>
      <div className="mt-2.5">{children}</div>
    </details>
  )
}

/* -------------------------------------------------------------------------- */
/* States                                                                     */
/* -------------------------------------------------------------------------- */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('relative overflow-hidden rounded-md bg-surface-sunken', className)} />
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={18} height={18} className={cn('animate-spin text-ink-faint', className)} aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
      <path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  )
}

export function EmptyStateBlock({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center px-6 py-12 text-center', className)}>
      {icon && (
        <span className="mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-surface-sunken text-ink-faint">
          {icon}
        </span>
      )}
      <p className="text-sm font-semibold text-ink">{title}</p>
      {description && <p className="mt-1 max-w-sm text-[0.8125rem] text-ink-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Stat                                                                       */
/* -------------------------------------------------------------------------- */

export function Stat({
  label,
  value,
  hint,
  tone = 'default',
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: 'default' | 'warn' | 'danger'
}) {
  const valueTone =
    tone === 'warn' ? 'text-warning-text' : tone === 'danger' ? 'text-danger-text' : 'text-ink'
  return (
    <div className="min-w-0">
      <dt className="text-2xs font-medium uppercase tracking-wide text-ink-faint">{label}</dt>
      <dd className={cn('mt-1 text-lg font-semibold tabular-nums', valueTone)}>{value}</dd>
      {hint && <p className="mt-0.5 text-xs text-ink-muted">{hint}</p>}
    </div>
  )
}
