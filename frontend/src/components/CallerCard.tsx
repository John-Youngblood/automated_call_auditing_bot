import { callerFacts, formatShortDate } from '../lib/format';
import type { Caller, Contact } from '../types/events';

interface Props {
  /** The caller's number; the card is not shown for withheld ones. */
  number: string;
  caller: Caller;
  onShowCalls: (number: string) => void;
  onEditContact: (number: string, contact: Contact | null) => void;
}

/**
 * What the dashboard already knows about this number: how their earlier calls
 * went, and what an operator noted about them. Between the caller's name and
 * what they just said, because it changes how both read.
 *
 * The counts were taken when the call rang, so they describe the calls before
 * this one.
 */
export default function CallerCard({ number, caller, onShowCalls, onEditContact }: Props) {
  const { contact, history } = caller;
  const facts = callerFacts(history, 3);
  if (history.lastCalledAt) facts.push(`last call ${formatShortDate(history.lastCalledAt)}`);

  return (
    <section className="caller-card" aria-label="About this caller">
      <p className="caller-card__facts">
        {facts.length > 0 ? facts.join(' · ') : 'First time calling'}
      </p>
      {contact?.note && <p className="caller-card__note">{contact.note}</p>}
      <p className="caller-card__links">
        {history.previousCalls > 0 && (
          <button type="button" className="link" onClick={() => onShowCalls(number)}>
            See all calls
          </button>
        )}
        <button type="button" className="link" onClick={() => onEditContact(number, contact)}>
          {contact ? 'Edit contact' : 'Add contact'}
        </button>
      </p>
    </section>
  );
}
