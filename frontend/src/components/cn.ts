/**
 * The one class-name joiner.
 *
 * Kept in its own module so the interface kit stays a components-only file: a
 * mixed export list defeats React Fast Refresh, which then reloads the whole UI
 * instead of the one component being edited.
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
