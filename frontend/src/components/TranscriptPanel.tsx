import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import type { TranscriptLine } from '../types/events';

interface Props {
  lines: TranscriptLine[];
  /** Resets scroll behaviour when the operator switches calls. */
  callId: string;
  live: boolean;
}

/** Treat "within this many pixels of the bottom" as pinned. */
const STICK_THRESHOLD_PX = 48;

/**
 * Live transcript with sticky auto-scroll.
 *
 * Follows new text automatically, but stops the moment the operator scrolls up
 * to re-read something -- yanking the view back mid-sentence is the fastest way
 * to make a live transcript unusable. Scrolling back to the bottom re-arms it.
 */
export default function TranscriptPanel({ lines, callId, live }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  // Re-pin when the selected call changes.
  useEffect(() => {
    setPinned(true);
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [callId]);

  // Layout effect, so the scroll happens in the same frame as the new line and
  // the user never sees a flash of the pre-scroll position.
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node && pinned) node.scrollTop = node.scrollHeight;
  }, [lines, pinned]);

  const handleScroll = () => {
    const node = scrollRef.current;
    if (!node) return;
    const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
    setPinned(distanceFromBottom <= STICK_THRESHOLD_PX);
  };

  return (
    <div className="transcript">
      <div
        className="transcript__scroll"
        ref={scrollRef}
        onScroll={handleScroll}
        // Announce new text to screen readers without stealing focus.
        role="log"
        aria-live="polite"
        aria-label="Live call transcript"
        tabIndex={0}
      >
        {lines.length === 0 ? (
          <p className="transcript__empty">
            {live ? 'Listening…' : 'No transcript for this call.'}
          </p>
        ) : (
          lines.map((line) => (
            <p
              key={line.segmentId}
              className={`transcript__line${line.isFinal ? '' : ' transcript__line--interim'}`}
            >
              {line.speaker && <span className="transcript__speaker">{line.speaker}</span>}
              <span className="transcript__text">{line.text}</span>
              {/* Interim text is still being revised; the cursor signals that
                  it may change under the reader. */}
              {!line.isFinal && <span className="transcript__cursor" aria-hidden="true" />}
            </p>
          ))
        )}
      </div>

      {!pinned && (
        <button
          type="button"
          className="transcript__jump"
          onClick={() => {
            setPinned(true);
            const node = scrollRef.current;
            if (node) node.scrollTop = node.scrollHeight;
          }}
        >
          Jump to latest ↓
        </button>
      )}
    </div>
  );
}
