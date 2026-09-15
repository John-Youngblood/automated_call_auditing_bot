/** Small display helpers, kept out of the components. */

export function formatPhoneNumber(raw: string | null): string {
  if (!raw) return 'Unknown caller';
  // Light touch for US/E.164; anything else is shown as-is rather than mangled.
  const match = /^\+?1?(\d{3})(\d{3})(\d{4})$/.exec(raw.replace(/[^\d+]/g, ''));
  if (!match) return raw;
  return `(${match[1]}) ${match[2]}-${match[3]}`;
}

export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Elapsed seconds as m:ss, for a live call timer. */
export function formatElapsed(startedAtIso: string, nowMs: number): string {
  const elapsedSeconds = Math.max(0, Math.floor((nowMs - new Date(startedAtIso).getTime()) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function describeLocation(city: string | null, country: string | null): string | null {
  return [city, country].filter(Boolean).join(', ') || null;
}

/** Whole seconds as m:ss, for a duration the backend already computed. */
export function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}
