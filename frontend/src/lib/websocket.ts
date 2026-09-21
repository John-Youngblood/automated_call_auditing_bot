/**
 * Reconnecting websocket client. Framework-agnostic on purpose -- React
 * concerns live in `hooks/useCallStream.ts`.
 *
 * What a bare `new WebSocket(...)` gets wrong for a screening dashboard:
 *
 * - **It never comes back.** Laptop sleep, wifi handover and server deploys
 *   all close the socket, and a dashboard that silently stops updating is
 *   worse than one that shows an error. So: automatic reconnect with
 *   exponential backoff plus jitter (jitter matters -- without it, every
 *   dashboard in the office reconnects in lockstep after a deploy and
 *   hammers the backend in waves).
 * - **Half-open connections look fine.** A dropped route leaves the socket
 *   `OPEN` with no traffic and no error, sometimes for minutes. The fix is an
 *   application-level heartbeat: ping on a timer, and if nothing at all
 *   arrives within the stall window, treat the connection as dead and
 *   reconnect. Protocol-level pings are invisible to browser JS, so they
 *   cannot serve this purpose.
 * - **Intentional closes get retried.** Navigating away or unmounting must
 *   stop reconnection, or React's StrictMode double-mount leaves a
 *   stray socket reconnecting forever.
 */

export interface ReconnectingSocketOptions {
  url: string;
  /**
   * Sent as the WebSocket subprotocol list, which is how the token reaches the
   * server: browsers cannot set headers on a WebSocket, and a token in the
   * query string would be written to every access log on the way.
   */
  protocols?: string[];
  onMessage: (data: unknown) => void;
  onStatusChange?: (status: 'connecting' | 'open' | 'reconnecting' | 'closed') => void;
  /** Heartbeat cadence. Must be well under the server's idle timeout. */
  heartbeatIntervalMs?: number;
  /** Reconnect if nothing is received for this long. */
  stallTimeoutMs?: number;
  minBackoffMs?: number;
  maxBackoffMs?: number;
}

const DEFAULTS = {
  heartbeatIntervalMs: 20_000,
  stallTimeoutMs: 45_000,
  minBackoffMs: 500,
  maxBackoffMs: 15_000,
};

export class ReconnectingSocket {
  private readonly options: Required<ReconnectingSocketOptions>;
  private socket: WebSocket | null = null;
  private attempt = 0;
  private closedByCaller = false;
  private reconnectTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private stallTimer: number | null = null;

  constructor(options: ReconnectingSocketOptions) {
    this.options = { onStatusChange: () => {}, protocols: [], ...DEFAULTS, ...options };
  }

  connect(): void {
    if (this.closedByCaller) return;
    this.clearTimer('reconnect');

    this.options.onStatusChange(this.attempt === 0 ? 'connecting' : 'reconnecting');

    const socket = new WebSocket(this.options.url, this.options.protocols);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.options.onStatusChange('open');
      this.startHeartbeat();
      this.resetStallTimer();
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      // Any traffic proves liveness, heartbeat replies included.
      this.resetStallTimer();
      try {
        this.options.onMessage(JSON.parse(event.data) as unknown);
      } catch {
        console.warn('[ws] ignoring non-JSON frame');
      }
    };

    // `onclose` fires after `onerror`, so reconnection is scheduled in one
    // place rather than racing between the two handlers.
    socket.onerror = () => {
      if (!this.closedByCaller) console.warn('[ws] socket error');
    };

    socket.onclose = (event: CloseEvent) => {
      this.stopHeartbeat();
      this.clearTimer('stall');
      if (this.closedByCaller) {
        this.options.onStatusChange('closed');
        return;
      }
      console.info(`[ws] closed (code ${event.code}), reconnecting`);
      this.scheduleReconnect();
    };
  }

  send(payload: unknown): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(payload));
    return true;
  }

  /** Close for good. Safe to call more than once. */
  close(): void {
    this.closedByCaller = true;
    this.clearTimer('reconnect');
    this.clearTimer('stall');
    this.stopHeartbeat();

    const socket = this.socket;
    this.socket = null;
    if (!socket) return;

    if (socket.readyState === WebSocket.CONNECTING) {
      // Closing mid-handshake makes every browser log "WebSocket is closed
      // before the connection is established". StrictMode's double-mount hits
      // this on every page load in dev, so detach the handlers and close once
      // the handshake finishes instead of shouting into the console.
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.onopen = () => socket.close(1000, 'client navigating away');
      return;
    }

    // 1000 = normal closure, so the server logs a clean disconnect.
    socket.close(1000, 'client navigating away');
  }

  // -- internals ------------------------------------------------------------
  private scheduleReconnect(): void {
    const { minBackoffMs, maxBackoffMs } = this.options;
    const exponential = Math.min(maxBackoffMs, minBackoffMs * 2 ** this.attempt);
    // Full jitter: spreads a thundering herd of dashboards across the window.
    const delay = Math.random() * exponential;
    this.attempt += 1;
    this.options.onStatusChange('reconnecting');
    this.reconnectTimer = window.setTimeout(() => this.connect(), delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      this.send({ type: 'ping' });
    }, this.options.heartbeatIntervalMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /** Nothing received in the stall window means the socket is dead. */
  private resetStallTimer(): void {
    this.clearTimer('stall');
    this.stallTimer = window.setTimeout(() => {
      console.warn('[ws] no traffic within stall window, forcing reconnect');
      // Closing triggers onclose, which schedules the reconnect.
      this.socket?.close(4000, 'stalled');
    }, this.options.stallTimeoutMs);
  }

  private clearTimer(which: 'reconnect' | 'stall'): void {
    const timer = which === 'reconnect' ? this.reconnectTimer : this.stallTimer;
    if (timer !== null) window.clearTimeout(timer);
    if (which === 'reconnect') this.reconnectTimer = null;
    else this.stallTimer = null;
  }
}

/**
 * Resolve the dashboard websocket URL.
 *
 * Defaults to same-origin so it works through the Vite proxy in dev and nginx
 * in production with no build-time configuration; `VITE_WS_URL` overrides it
 * when the dashboard is hosted apart from the API.
 */
export function resolveWebSocketUrl(path = '/ws/frontend'): string {
  const configured = import.meta.env.VITE_WS_URL;
  if (configured) return configured;
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}${path}`;
}
