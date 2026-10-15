/**
 * Dashboard session token.
 *
 * One shared password, exchanged for a token the browser holds. Kept in
 * localStorage so a refresh mid-show does not sign the operator out — the
 * tolerable cost of that is a token readable by anything already running on
 * this origin, which on a page with no third-party scripts is nothing new.
 *
 * Tokens die when the backend restarts, so the client has to treat a 401 as
 * "sign in again" rather than an error worth retrying. Both the REST client
 * and the socket report it through `expireSession`, which drops the token and
 * tells the app to show the sign-in screen.
 */

const STORAGE_KEY = 'callScreener.token';

/** Wrapped: localStorage throws in a private window with site data blocked. */
export function readToken(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeToken(token: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Non-fatal: the session still works until this tab is closed.
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to do; the caller is signing out either way.
  }
}

let sessionExpiredListener: (() => void) | null = null;

/** Register the app's sign-out. Returns an unsubscribe for effect cleanup. */
export function onSessionExpired(listener: () => void): () => void {
  sessionExpiredListener = listener;
  return () => {
    if (sessionExpiredListener === listener) sessionExpiredListener = null;
  };
}

/** The server no longer knows this token: forget it and go back to sign-in. */
export function expireSession(): void {
  clearToken();
  sessionExpiredListener?.();
}
