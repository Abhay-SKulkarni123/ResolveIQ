/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to the backend so the browser sees one origin. Without
// this every fetch would need an absolute URL and CORS would have to be configured for
// whatever host the reviewer happens to be on.
//
// The `test` block is typed by the triple-slash reference above, which merges vitest's
// options into vite's UserConfig. Importing defineConfig from 'vitest/config' instead
// would also work, but vitest 2 bundles its own copy of vite, so the react plugin's
// type and the config's expected plugin type come from two different modules and the
// compiler rejects the combination.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
