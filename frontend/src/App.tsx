import { useCallback, useState } from 'react';

import Dashboard from './components/Dashboard';
import LoginScreen from './components/LoginScreen';
import { clearToken, readToken } from './lib/auth';

export default function App() {
  const [signedIn, setSignedIn] = useState(() => readToken() !== null);

  /**
   * Called when the API or the socket reports 401 — the token was dropped by
   * then, usually because the backend restarted and forgot every session.
   */
  const signOut = useCallback(() => {
    clearToken();
    setSignedIn(false);
  }, []);

  if (!signedIn) return <LoginScreen onSignedIn={() => setSignedIn(true)} />;
  return <Dashboard onSignedOut={signOut} />;
}
