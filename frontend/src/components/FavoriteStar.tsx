interface Props {
  isFavorite: boolean;
  /** Disabled when there is no number to attach a contact to. */
  disabled?: boolean;
  pending?: boolean;
  onToggle: () => void;
  label: string;
}

/**
 * Star toggle. One click, no confirmation — starring is trivially reversible,
 * unlike blocking, so a dialog would just be friction on the common action.
 */
export default function FavoriteStar({
  isFavorite,
  disabled = false,
  pending = false,
  onToggle,
  label,
}: Props) {
  return (
    <button
      type="button"
      className={`star${isFavorite ? ' star--on' : ''}`}
      onClick={onToggle}
      disabled={disabled || pending}
      aria-pressed={isFavorite}
      title={
        disabled
          ? 'Caller ID was withheld, so there is nothing to save'
          : isFavorite
            ? `Remove ${label} from favourites`
            : `Add ${label} to favourites`
      }
    >
      <span aria-hidden="true">{isFavorite ? '★' : '☆'}</span>
      <span className="sr-only">
        {isFavorite ? `${label} is a favourite` : `Add ${label} to favourites`}
      </span>
    </button>
  );
}
