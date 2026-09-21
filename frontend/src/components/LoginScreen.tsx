import { useState } from 'react';

import { ApiError, sessionApi } from '../lib/api';

interface Props {
  onSignedIn: () => void;
}

/** One shared password. No accounts, no reset flow — see backend sessions.py. */
export default function LoginScreen({ onSignedIn }: Props) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await sessionApi.logIn(password);
      onSignedIn();
    } catch (err: unknown) {
      setError(
        err instanceof ApiError && err.needsSignIn
          ? 'That password is not right.'
          : 'Could not reach the server.',
      );
      setPassword('');
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="login">
      <form className="login__card" onSubmit={submit}>
        <img className="login__logo" src="/h3_logo.png" alt="The H3 Podcast" />
        <h1 className="login__title">Call Screener</h1>

        <label className="login__label" htmlFor="password">
          Dashboard password
        </label>
        <input
          id="password"
          className="login__input"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          autoFocus
          disabled={pending}
        />

        {error && (
          <p className="login__error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="button button--accept" disabled={pending || !password}>
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
