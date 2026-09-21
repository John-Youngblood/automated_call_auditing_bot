/**
 * Dashboard session token.
 *
 * One shared password, exchanged for a token the browser holds. Kept in
 * localStorage so a refresh mid-show does not sign the operator out — the
 * tolerable cost of that is a token readable by anything already running on
 * this origin, which on a page with no third-party scripts is nothing new.
 *
 * Tokens die when the backend restarts, so the client has to treat a 401 as
 * "sign in again" rather than an error worth retrying.
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
