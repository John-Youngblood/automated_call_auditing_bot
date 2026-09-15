import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles.css';

const container = document.getElementById('root');
if (!container) throw new Error('index.html is missing #root');

// StrictMode double-invokes effects in development. That is deliberate here:
// it surfaces any websocket that fails to clean up after itself.
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
