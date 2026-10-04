/**
 * Money formatting.
 *
 * These tests exist because of one rule: an amount is never converted to a number.
 *
 * `Number("5021.86").toString()` is `"5021.859999999999"`. A reviewer comparing that
 * against an invoice sees a figure the system never calculated, and the discrepancy is
 * small enough to look like rounding. So the assertions here are about *string*
 * behaviour, and several of them pin the cases a naive `parseFloat` would get wrong.
 */

import { describe, expect, it } from 'vitest'

import {
  formatDifference,
  formatMoney,
  formatMoneyDetail,
  groupDigits,
  shortFingerprint,
} from '../components/money'

describe('groupDigits', () => {
  it('groups thousands', () => {
    expect(groupDigits('5021.86')).toBe('5,021.86')
    expect(groupDigits('1234567.89')).toBe('1,234,567.89')
  })

  it('leaves short numbers alone', () => {
    expect(groupDigits('999')).toBe('999')
    expect(groupDigits('0.5000')).toBe('0.5000')
  })

  it('keeps a negative sign outside the grouping', () => {
    expect(groupDigits('-5021.86')).toBe('-5,021.86')
    expect(groupDigits('-1234567')).toBe('-1,234,567')
  })

  it('preserves the exact digits and scale it was given', () => {
    // Not "5,021.9" and not "5,021.8600": the scale belongs to the stored value.
    expect(groupDigits('5021.8600')).toBe('5,021.8600')
    expect(groupDigits('0.0007')).toBe('0.0007')
  })

  it('returns anything it does not understand unchanged', () => {
    // Silently mangling a figure is worse than showing an odd one, so an unparseable
    // amount passes straight through for a human to notice.
    expect(groupDigits('1e3')).toBe('1e3')
    expect(groupDigits('')).toBe('')
    expect(groupDigits('not-a-number')).toBe('not-a-number')
  })

  it('is immune to the two ways a float actually loses money', () => {
    /*
     * The first version of this test asserted that `String(Number('5021.86'))`
     * contained the artifact, and it passed-by-luck for the wrong reason: JavaScript
     * prints the *shortest* representation that round-trips, so that particular
     * conversion happens to look clean. The damage is not in `String()`. It is:
     *
     *   1. arithmetic -- 0.1 + 0.2 is 0.30000000000000004, and ten 0.1s sum to
     *      0.9999999999999999, so any total computed by adding floats is suspect;
     *   2. scale -- Number('28.40') prints as '28.4', silently dropping the digit the
     *      stored value carried.
     *
     * Both are demonstrated below so the assertions cannot drift into testing the wrong
     * thing again.
     */
    expect(0.1 + 0.2).not.toBe(0.3)
    expect(Array(10).fill(0.1).reduce((total, value) => total + value, 0)).not.toBe(1)

    // The formatter keeps the scale it was given.
    expect(groupDigits('28.40')).toBe('28.40')
    expect(String(Number('28.40'))).toBe('28.4')
  })
})

describe('formatMoney', () => {
  it('renders the currency before the figure', () => {
    expect(formatMoney('5021.86', 'USD')).toBe('USD 5,021.86')
  })

  it('shows a dash for no amount', () => {
    // An empty cell and a zero are different claims, so neither is invented.
    expect(formatMoney(null, 'USD')).toBe('--')
    expect(formatMoney('', 'USD')).toBe('--')
  })

  it('renders without a currency rather than guessing one', () => {
    expect(formatMoney('10.00', null)).toBe('10.00')
  })

  it('renders a zero as a zero', () => {
    expect(formatMoney('0.0000', 'USD')).toBe('USD 0.0000')
  })
})

describe('formatMoneyDetail', () => {
  it('shows the amount when there is one', () => {
    expect(formatMoneyDetail({ amount: '4993.46', currency: 'USD' })).toBe('USD 4,993.46')
  })

  it('shows the reason when there is no amount', () => {
    // A blank would read as a bug, and a reviewer cannot ask about a blank they cannot
    // interpret. The reason is the whole point of a not-assessable impact.
    expect(
      formatMoneyDetail({
        amount: null,
        currency: 'USD',
        not_assessable_reason: 'no rule prices this metric',
      }),
    ).toBe('Not assessable: no rule prices this metric')
  })

  it('distinguishes an absent reason from a zero amount', () => {
    expect(formatMoneyDetail({ amount: null, currency: 'USD' })).toBe('Not assessable')
    expect(formatMoneyDetail({ amount: '0.0000', currency: 'USD' })).toBe('USD 0.0000')
  })
})

describe('formatDifference', () => {
  it('keeps the sign outside the grouped digits', () => {
    expect(formatDifference('-4993.46', 'USD')).toBe('-USD 4,993.46')
    expect(formatDifference('+4993.46', 'USD')).toBe('+USD 4,993.46')
  })

  it('shows an unsigned difference without inventing a sign', () => {
    expect(formatDifference('4993.46', 'USD')).toBe('USD 4,993.46')
  })

  it('shows a zero difference as zero, not as favourable or adverse', () => {
    // Whether a zero difference is good news is a judgement about the dispute. A
    // formatter that coloured it green would be making that call for the reviewer.
    expect(formatDifference('0.0000', 'USD')).toBe('USD 0.0000')
  })

  it('does not compare against zero numerically', () => {
    // "0.10" is not zero as a string comparison against "0" would suggest, and
    // "0.0000" must not be treated as absent.
    expect(formatDifference('0.10', 'USD')).toBe('USD 0.10')
  })

  it('shows a dash when there is no difference', () => {
    expect(formatDifference(null, 'USD')).toBe('--')
  })
})

describe('shortFingerprint', () => {
  it('drops the algorithm prefix', () => {
    expect(shortFingerprint('sha256:0123456789abcdef')).toBe('01234567')
  })

  it('falls back for an unprefixed value', () => {
    expect(shortFingerprint('0123456789abcdef')).toBe('01234567')
  })
})
