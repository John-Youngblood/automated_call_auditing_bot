import { useState } from 'react';

import type { ToastTone } from './useToasts';
import { ApiError, contactsApi } from '../lib/api';
import { formatPhoneNumber } from '../lib/format';
import type { Call, Contact, ContactChangeResult, ContactPatch } from '../types/events';

/** Clearing all three is how the server is told to forget a number. */
const REMOVE: ContactPatch = { name: null, note: null, standing: null };

export interface ContactDialogState {
  open: boolean;
  /** Null while adding, when the operator types the number. */
  number: string | null;
  contact: Contact | null;
  pending: boolean;
  error: string | null;
  onSave: (number: string, patch: ContactPatch) => void;
  onRemove: (number: string) => void;
  onCancel: () => void;
}

export interface BlockDialogState {
  /** The number awaiting confirmation, or null when nothing is. */
  number: string | null;
  /** Their call in the queue right now, which blocking hangs up on. */
  liveCall: Call | null;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export interface ContactActions {
  editContact: (number: string, contact: Contact | null) => void;
  addContact: () => void;
  /** Asks first: blocking hangs up on the caller if they are holding. */
  blockNumber: (number: string) => void;
  unblockNumber: (number: string) => void;
  contactDialog: ContactDialogState;
  blockDialog: BlockDialogState;
}

/**
 * Everything that changes a contact, from anywhere on the dashboard.
 *
 * One contact dialog and one block confirmation for the whole page, opened
 * from the live call, a history row or the contacts list alike, so the three
 * cannot drift into different rules. The change itself comes back over the
 * socket (`contact.changed`, and `call.updated` for live calls), so nothing
 * here edits a list.
 */
export function useContactActions(
  activeCalls: Call[],
  onNotice: (message: string, tone: ToastTone) => void,
): ContactActions {
  const [editing, setEditing] = useState<{ number: string | null; contact: Contact | null } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [blocking, setBlocking] = useState<string | null>(null);
  const [blockPending, setBlockPending] = useState(false);

  function openDialog(number: string | null, contact: Contact | null) {
    setSaveError(null);
    setEditing({ number, contact });
  }

  async function save(number: string, patch: ContactPatch) {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await contactsApi.update(number, patch);
      setEditing(null);
      if (patch.standing === 'blocked') noticeBlocked(number, result);
    } catch (err: unknown) {
      setSaveError(err instanceof ApiError ? err.message : 'Could not reach the server.');
    } finally {
      setSaving(false);
    }
  }

  async function confirmBlock() {
    if (!blocking) return;
    setBlockPending(true);
    try {
      noticeBlocked(blocking, await contactsApi.update(blocking, { standing: 'blocked' }));
    } catch (err: unknown) {
      onNotice(failure(`block ${formatPhoneNumber(blocking)}`, err), 'warn');
    } finally {
      setBlockPending(false);
      setBlocking(null);
    }
  }

  async function unblock(number: string) {
    try {
      await contactsApi.update(number, { standing: null });
      onNotice(`Unblocked ${formatPhoneNumber(number)}`, 'ok');
    } catch (err: unknown) {
      onNotice(failure(`unblock ${formatPhoneNumber(number)}`, err), 'warn');
    }
  }

  // A warning, not a confirmation, when someone could not be hung up on: they
  // are still holding, so it must not expire while the operator looks away.
  function noticeBlocked(number: string, result: ContactChangeResult) {
    if (result.failedCallIds.length > 0) {
      onNotice(
        'Blocked, but couldn’t hang up on their current call — they’re still holding.',
        'warn',
      );
    } else {
      onNotice(`Blocked ${formatPhoneNumber(result.contact?.number ?? number)}`, 'ok');
    }
  }

  return {
    editContact: (number, contact) => openDialog(number, contact),
    addContact: () => openDialog(null, null),
    blockNumber: setBlocking,
    unblockNumber: (number) => void unblock(number),
    contactDialog: {
      open: editing !== null,
      number: editing?.number ?? null,
      contact: editing?.contact ?? null,
      pending: saving,
      error: saveError,
      onSave: (number, patch) => void save(number, patch),
      onRemove: (number) => void save(number, REMOVE),
      onCancel: () => setEditing(null),
    },
    blockDialog: {
      number: blocking,
      liveCall: blocking
        ? (activeCalls.find((call) => call.caller.number === blocking) ?? null)
        : null,
      pending: blockPending,
      onConfirm: () => void confirmBlock(),
      onCancel: () => setBlocking(null),
    },
  };
}

function failure(what: string, err: unknown): string {
  return err instanceof ApiError ? `Could not ${what}: ${err.message}` : `Could not ${what}.`;
}
