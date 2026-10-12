import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { setAuthToken } from './api/client'
import './index.css'

// Token-mode bootstrap. With AUTH_MODE=token the actor headers are ignored and this
// bearer token is the identity; a deployment supplies it out-of-band (a hosting page
// that seeds storage, or an operator pasting a token minted with scripts/issue_token).
// In the default demo mode nothing is stored here and the headers remain the mechanism.
// Storage may be unavailable (private mode, disabled cookies); the demo path does not
// need it, so a failure must not stop the workbench from loading.
try {
  const token = localStorage.getItem('resolveiq.authToken')
  if (token) setAuthToken(token)
} catch {
  // Ignore: no token is the normal case.
}

const container = document.getElementById('root')
if (!container) {
  throw new Error('index.html is missing #root; the bundle has nothing to mount into')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
