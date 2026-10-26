/** Small display helpers, kept out of the components. */

import type { Caller, CallerHistory } from '../types/events';

export function formatPhoneNumber(raw: string): string {
  // Light touch for US/E.164; anything else is shown as-is rather than mangled.
  const match = /^\+?1?(\d{3})(\d{3})(\d{4})$/.exec(raw.replace(/[^\d+]/g, ''));
  if (!match) return raw;
  return `(${match[1]}) ${match[2]}-${match[3]}`;
}

export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** "Oct 3". */
export function formatShortDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

/**
 * The one name to show for a caller: what the operators called them, then the
 * carrier's caller-ID name, then the number.
 */
export function callerLabel(caller: Caller): string {
  return (
    caller.contact?.name ??
    caller.name ??
    (caller.number ? formatPhoneNumber(caller.number) : 'No caller ID')
  );
}

/** The formatted number, when `callerLabel` shows a name in its place. */
export function numberUnderName(caller: Caller): string | null {
  return caller.number && (caller.contact?.name ?? caller.name)
    ? formatPhoneNumber(caller.number)
    : null;
}

/**
 * The most decision-worthy things about a caller's earlier calls, at most
 * `max` of them. A rejection outranks an appearance, which outranks merely
 * having called; a first-time caller gets nothing rather than "0×".
 */
export function callerFacts(history: CallerHistory, max: number): string[] {
  const facts: string[] = [];
  if (history.timesRejected > 0) facts.push(`Rejected ${history.timesRejected}×`);
  if (history.timesOnAir > 0) {
    facts.push(
      history.lastOnAirAt
        ? `On air ${history.timesOnAir}× · last ${formatShortDate(history.lastOnAirAt)}`
        : `On air ${history.timesOnAir}×`,
    );
  }
  if (history.previousCalls > 0) facts.push(`Called ${history.previousCalls}× before`);
  return facts.slice(0, max);
}

/** Elapsed seconds as m:ss, for a live call timer. */
export function formatElapsed(startedAtIso: string, nowMs: number): string {
  const elapsedSeconds = Math.max(0, Math.floor((nowMs - new Date(startedAtIso).getTime()) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}


/** Whole seconds as m:ss, for a duration the backend already computed. */
export function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

/**
 * Human label for a call status.
 *
 * Status values double as CSS class suffixes, so they are slugs and cannot
 * contain spaces. Everything an operator reads goes through here instead of
 * rendering the raw value, which kept working only as long as every status
 * happened to be one word.
 */
const STATUS_LABELS: Record<string, string> = {
  'on-hold': 'on hold',
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
