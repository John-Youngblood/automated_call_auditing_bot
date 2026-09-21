import { useCallback, useRef, useState } from 'react';

export type ToastTone = 'ok' | 'warn';

export interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
}

/** How long a success toast stays before dismissing itself. */
const OK_TIMEOUT_MS = 6000;

/**
 * Cap on visible toasts. An operator clicking repeatedly should not end up
 * with a column of them covering the queue.
 */
const MAX_VISIBLE = 3;

/**
 * Transient confirmations, bottom-right.
 *
 * Success toasts expire on their own; warnings do not. A warning here means
 * something still needs a human — callers we could not hang up are still
 * connected — and a message about that must not disappear while the operator
 * is looking at the queue instead.
 */
export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const push = useCallback(
    (message: string, tone: ToastTone = 'ok') => {
      const id = nextId.current++;
      setToasts((current) => [...current, { id, message, tone }].slice(-MAX_VISIBLE));
      if (tone === 'ok') {
        timers.current.set(id, window.setTimeout(() => dismiss(id), OK_TIMEOUT_MS));
      }
      return id;
    },
    [dismiss],
  );

  return { toasts, push, dismiss };
}
