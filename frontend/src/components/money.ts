/**
 * Formatting amounts that arrived as text.
 *
 * The rule this file exists to enforce: **an amount is never converted to a number.**
 * `Number("5021.86")` yields 5021.859999999999, and a reviewer comparing that against
 * an invoice sees a figure the system never calculated. The backend sends text for
 * exactly this reason, and the first thing this UI does with it is group the digits --
 * never parse them.
 *
 * Everything here is string manipulation. There is no arithmetic, which means there is
 * no rounding mode to get wrong and no float to leak.
 */

import type { AmountText, MoneyResponse } from '../api/types'

/** A digit group separator that cannot be confused with a decimal point. */
const GROUP = ','
const DECIMAL = '.'

/**
 * Group the integer part of a decimal string.
 *
 * Handles the sign and any exponent-free plain decimal. Deliberately does *not* touch
 * a value it does not understand: an amount that arrives as `"1e3"` or `"abc"` is shown
 * as-is, because silently mangling a figure is worse than showing an odd one.
 */
export function groupDigits(amount: AmountText): AmountText {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(amount)
  if (!match) return amount

  const [, sign = '', whole = '', fraction] = match
  if (whole.length <= 3) return amount

  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, GROUP)
  return fraction === undefined ? `${sign}${grouped}` : `${sign}${grouped}${DECIMAL}${fraction}`
}

/**
 * Render an amount with its currency.
 *
 * Returns an em dash when there is nothing to show. An empty cell and a zero are
 * different claims -- "not assessable" versus "nothing left to bill" -- and collapsing
 * them would hide a figure a reviewer needed.
 */
export function formatMoney(amount: AmountText | null, currency: string | null): string {
  if (amount === null || amount === '') return '--'
  const grouped = groupDigits(amount)
  return currency ? `${currency} ${grouped}` : grouped
}

/**
 * Render a `MoneyResponse`, including the reason there is no amount.
 *
 * The reason is the point. `NOT_ASSESSABLE` shown without its explanation reads as a
 * bug, and a reviewer cannot ask about a blank they cannot interpret.
 */
export function formatMoneyDetail(money: MoneyResponse): string {
  if (money.amount !== null && money.amount !== '') {
    return formatMoney(money.amount, money.currency)
  }
  if (money.not_assessable_reason) {
    return `Not assessable: ${money.not_assessable_reason}`
  }
  return 'Not assessable'
}

/**
 * Render a signed difference, which is the comparison a reviewer is actually here for.
 *
 * The sign is derived from the first character of the string, not by comparing the
 * value to zero, so a difference of `"0.0000"` is neither shown as positive nor
 * negative. Whether a zero difference is good news is a judgement about the dispute,
 * not something a formatter should decide.
 */
export function formatDifference(amount: AmountText | null, currency: string | null): string {
  if (amount === null || amount === '') return '--'
  const magnitude = groupDigits(amount.replace(/^[-+]/, ''))
  const sign = amount.startsWith('-') ? '-' : amount.startsWith('+') ? '+' : ''
  const body = currency ? `${currency} ${magnitude}` : magnitude
  return `${sign}${body}`
}

/** A short form of a fingerprint, for display beside a longer one. */
export function shortFingerprint(fingerprint: string): string {
  return fingerprint.startsWith('sha256:') ? fingerprint.slice(7, 15) : fingerprint.slice(0, 8)
}
