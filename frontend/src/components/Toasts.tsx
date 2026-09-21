import type { Toast } from '../hooks/useToasts';

interface Props {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}

/**
 * Toast stack, bottom-right.
 *
 * Out of the way of the queue, which is the thing an operator is actually
 * reading. The live region is polite rather than assertive: these confirm
 * something the operator just did, and interrupting a screen reader mid-
 * transcript to say "line closed" would be worse than waiting.
 */
export default function Toasts({ toasts, onDismiss }: Props) {
  if (toasts.length === 0) return null;

  return (
    <div className="toasts" role="region" aria-label="Notifications">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast toast--${toast.tone}`}
          // A warning needs a human, so it interrupts; a confirmation waits.
          role={toast.tone === 'warn' ? 'alert' : 'status'}
        >
          <span className="toast__message">{toast.message}</span>
          <button
            type="button"
            className="toast__dismiss"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss notification"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
