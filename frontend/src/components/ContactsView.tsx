import type { ContactFilter, Contacts } from '../hooks/useContacts';
import { formatPhoneNumber, formatShortDate } from '../lib/format';
import type { Contact, ContactListItem } from '../types/events';

interface Props {
  contacts: Contacts;
  onAdd: () => void;
  onEdit: (number: string, contact: Contact) => void;
  onUnblock: (number: string) => void;
  /** Open Call History filtered to this number. */
  onShowCalls: (number: string) => void;
}

const FILTERS: { value: ContactFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'favorite', label: 'Favorites' },
  { value: 'blocked', label: 'Blocked' },
];

/**
 * Every number the operators have named, noted, starred or blocked.
 *
 * Mostly a place to find and undo a block, and to add a number before it ever
 * calls -- everything else can be done from the call itself.
 */
export default function ContactsView({ contacts, onAdd, onEdit, onUnblock, onShowCalls }: Props) {
  const { loading, error, empty } = contacts;

  return (
    <section className="contacts" aria-label="Contacts">
      <header className="contacts__header">
        <div>
          <h2>Contacts</h2>
          <p className="contacts__subtitle">Names, notes, favorites and blocked numbers</p>
        </div>
        <button type="button" className="button button--ghost button--compact" onClick={onAdd}>
          Add number
        </button>
      </header>

      <div className="toolbar">
        <input
          className="input toolbar__search"
          type="search"
          value={contacts.query}
          onChange={(event) => contacts.setQuery(event.target.value)}
          placeholder="Search names, numbers, notes"
          aria-label="Search contacts"
        />
        <span className="toolbar__chips" role="group" aria-label="Show">
          {FILTERS.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              className={`chip${contacts.filter === value ? ' chip--active' : ''}`}
              onClick={() => contacts.setFilter(value)}
              aria-pressed={contacts.filter === value}
            >
              {label}
              <span className="chip__count">{contacts.counts[value]}</span>
            </button>
          ))}
        </span>
      </div>

      {error && (
        <p className="contacts__error" role="alert">
          {error}
        </p>
      )}

      {loading && empty ? (
        <p className="contacts__empty">Loading…</p>
      ) : empty ? (
        <div className="contacts__empty">
          <p>
            No contacts yet. Add a number to give it a name or a note, star it, or block it — or do
            the same from any call.
          </p>
          <button type="button" className="button button--ghost button--compact" onClick={onAdd}>
            Add number
          </button>
        </div>
      ) : contacts.contacts.length === 0 ? (
        <p className="contacts__empty">No contacts match.</p>
      ) : (
        <div className="contacts__scroll">
          <table className="contacts__table">
            <thead>
              <tr>
                <th scope="col">Contact</th>
                <th scope="col">Note</th>
                <th scope="col">Calls</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {contacts.contacts.map((contact) => (
                <ContactRow
                  key={contact.number}
                  contact={contact}
                  onEdit={onEdit}
                  onUnblock={onUnblock}
                  onShowCalls={onShowCalls}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

interface RowProps {
  contact: ContactListItem;
  onEdit: (number: string, contact: Contact) => void;
  onUnblock: (number: string) => void;
  onShowCalls: (number: string) => void;
}

function ContactRow({ contact, onEdit, onUnblock, onShowCalls }: RowProps) {
  const blocked = contact.standing === 'blocked';

  return (
    <tr>
      <td>
        <span className="contacts__name">
          {contact.standing === 'favorite' && (
            <span className="star" role="img" aria-label="Favorite">
              ★
            </span>
          )}
          {contact.name ?? <span className="contacts__unnamed">No name</span>}
        </span>
        <span className="contacts__number">{formatPhoneNumber(contact.number)}</span>
      </td>

      <td className="contacts__note">
        {/* The note doubles as the block reason, so a blocked row shows both. */}
        {blocked && <span className="contacts__blocked">{blockedSummary(contact)}</span>}
        {contact.note && <span>{contact.note}</span>}
      </td>

      <td className="contacts__calls">
        {contact.calls > 0 ? (
          <button type="button" className="link" onClick={() => onShowCalls(contact.number)}>
            {contact.calls} call{contact.calls === 1 ? '' : 's'}
          </button>
        ) : (
          'None yet'
        )}
      </td>

      <td>
        <span className="contacts__actions">
          <button type="button" className="link" onClick={() => onEdit(contact.number, contact)}>
            Edit
          </button>
          {blocked && (
            <button type="button" className="link" onClick={() => onUnblock(contact.number)}>
              Unblock
            </button>
          )}
        </span>
      </td>
    </tr>
  );
}

/** "Blocked Oct 3 · tried 2× since". The count is what says a block is working. */
function blockedSummary(contact: Contact): string {
  const since = contact.standingSince ? ` ${formatShortDate(contact.standingSince)}` : '';
  const tried = contact.blockedAttempts > 0 ? ` · tried ${contact.blockedAttempts}× since` : '';
  return `Blocked${since}${tried}`;
}
