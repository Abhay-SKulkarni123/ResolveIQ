/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Deliberately restrained. The only colour that carries meaning is amber,
        // for "this run no longer describes the attached evidence".
        ink: { DEFAULT: '#111827', muted: '#6b7280', faint: '#9ca3af' },
        line: '#e5e7eb',
        surface: '#ffffff',
        canvas: '#f9fafb',
        warn: { bg: '#fffbeb', border: '#fcd34d', text: '#92400e' },
        danger: { bg: '#fef2f2', border: '#fecaca', text: '#991b1b' },
      },
    },
  },
  plugins: [],
}
