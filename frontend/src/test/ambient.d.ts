/**
 * The OpenAPI snapshot arrives as JSON so it can be imported without teaching
 * `tsc` to typecheck several thousand lines of generated literal, and without
 * needing `resolveJsonModule` (or `@types/node`, which this project does not have).
 * Both declarations are pure ambient; no dependency is added.
 */
declare module '*.json' {
  const value: unknown
  export default value
}

declare module '*?raw' {
  const content: string
  export default content
}
