import type { Call } from '../types/events';

interface Props {
  call: Call;
}

/** Below this, the transcription is shaky enough to warn the operator. */
const LOW_CONFIDENCE = 0.6;

/**
 * What the caller said when asked why they are calling.
 *
 * Arrives complete in one update, so there is no scrolling, no auto-follow and
 * no partial state — the call is either still being described or the whole
 * thing is here.
 */
export default function TranscriptPanel({ call }: Props) {
  const stillSpeaking = call.status === 'screening';
  const lowConfidence =
    call.transcriptConfidence !== null && call.transcriptConfidence < LOW_CONFIDENCE;

  return (
    <div className="transcript">
      {stillSpeaking ? (
        <p className="transcript__pending">
          <span className="transcript__dots" aria-hidden="true" />
          Greeting is playing. Waiting for the caller to describe why they’re calling…
        </p>
      ) : call.transcript ? (
        <>
          {lowConfidence && (
            <p className="transcript__warning" role="status">
              {/* Explicit spaces: JSX drops the whitespace around a newline
                  that sits next to an expression. */}
              {'Low transcription confidence'}
              {call.transcriptConfidence !== null &&
                ` (${Math.round(call.transcriptConfidence * 100)}%)`}
              {' — read with caution.'}
            </p>
          )}
          <blockquote className="transcript__quote">{call.transcript}</blockquote>
        </>
      ) : call.recovered ? (
        <p className="transcript__recovered" role="status">
          Recovered after a restart — this caller is still on hold, but what they said was
          lost. Ask them again when you take the call.
        </p>
      ) : (
        <p className="transcript__empty">
          The caller didn’t say anything. They may have hung up, or been silent.
        </p>
      )}
    </div>
  );
}
