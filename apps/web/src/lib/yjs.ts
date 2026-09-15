import * as Y from "yjs";

/**
 * Yjs over the relay's brain-doc socket.
 *
 * **The relay does not understand this protocol, and that is the design.** It
 * stores and forwards opaque base64 blobs, never merging and never deciding what
 * is newer. Merging happens here, in the clients, which is exactly why it is safe
 * to relay a document the relay cannot read. See ADR 0007.
 *
 * That decision has a consequence worth stating plainly: **the relay's stored
 * snapshot is not authoritative.** If two clients merge concurrently, the relay's
 * blob is whichever write landed last, which may not be the merged state. The
 * authoritative record is the bus log on the daemon that owns the session, and
 * the doc is a projection that can be rebuilt. A client that treats the relay's
 * snapshot as truth will, one day, silently discard someone's edit.
 *
 * What this provider therefore does on connect is send its own full state, not
 * just accept the snapshot. Both are merged — CRDT merges are commutative — so
 * neither side loses work regardless of which arrived first.
 */

export type ProviderStatus =
  | "idle"
  | "connecting"
  | "syncing"
  | "connected"
  | "reconnecting"
  | "closed"
  | "error";

export interface RelayDocProviderOptions {
  /** `ws://` or `wss://` base, e.g. `ws://127.0.0.1:8787`. */
  url: string;
  room: string;
  /** A room JWT. Sent in the first frame, never in the query string. */
  token: string;
  doc: Y.Doc;
  onStatus?: (status: ProviderStatus, detail?: string) => void;
  onError?: (error: Error) => void;
  /** Bytes. Frames larger than this are refused locally rather than by the relay. */
  maxFrameBytes?: number;
}

interface HelloFrame {
  type: "hello";
  connection: string;
  room: string;
  can_write: boolean;
  snapshot: { update: string; version: number };
  max_frame_bytes: number;
}

type ServerFrame =
  | HelloFrame
  | { type: "update"; from: string; from_subject: string; update: string; version: number }
  | { type: "saved"; version: number }
  | { type: "pong"; at: string }
  | { type: "error"; code: string; message: string };

/** Origin tag for updates this provider applied, so it does not echo them back. */
const RELAY_ORIGIN = Symbol("openburrow.relay");

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 15_000;

// ---------------------------------------------------------------------------
// base64 <-> bytes
//
// `String.fromCharCode.apply` blows the stack on a large array, and a CRDT
// snapshot after an hour of work is easily large enough. Chunked on purpose.
// ---------------------------------------------------------------------------

const CHUNK = 0x8000;

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += CHUNK) {
    const slice = bytes.subarray(offset, offset + CHUNK);
    binary += String.fromCharCode(...slice);
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export class RelayDocProvider {
  readonly doc: Y.Doc;

  private readonly options: Required<Pick<RelayDocProviderOptions, "maxFrameBytes">> &
    RelayDocProviderOptions;

  private socket: WebSocket | null = null;
  private status: ProviderStatus = "idle";
  private attempt = 0;
  private closedByUs = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private handshakeTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly onDocUpdate: (update: Uint8Array, origin: unknown) => void;

  /** What the relay says this connection may do. */
  canWrite = false;
  /** The relay's stored version, as of the last frame we saw. */
  version = 0;

  constructor(options: RelayDocProviderOptions) {
    this.options = { maxFrameBytes: 1_048_576, ...options };
    this.doc = options.doc;

    this.onDocUpdate = (update, origin) => {
      // Updates we applied from the relay must not be sent back. Without this
      // check the two clients bounce the same update between them forever.
      if (origin === RELAY_ORIGIN) return;
      this.send({ type: "update", update: bytesToBase64(update) });
    };
  }

  get currentStatus(): ProviderStatus {
    return this.status;
  }

  connect(): void {
    this.closedByUs = false;
    this.open();
  }

  disconnect(): void {
    this.closedByUs = true;
    this.clearTimers();
    this.doc.off("update", this.onDocUpdate);
    if (this.socket) {
      // 1000 rather than an abrupt close: the relay logs a clean close
      // differently from a dropped socket, and that distinction is how an
      // operator tells "the user navigated away" from "the network failed".
      this.socket.close(1000, "client closed");
      this.socket = null;
    }
    this.setStatus("closed");
  }

  /** Force a reconnect, e.g. after the token is refreshed. */
  reconnect(): void {
    this.clearTimers();
    this.socket?.close(1000, "reconnecting");
    this.socket = null;
    this.attempt = 0;
    this.open();
  }

  // --- internals ---------------------------------------------------------

  private open(): void {
    const { url, room } = this.options;
    const endpoint = `${url.replace(/\/$/, "")}/rooms/${encodeURIComponent(room)}/doc`;

    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");

    let socket: WebSocket;
    try {
      socket = new WebSocket(endpoint);
    } catch (cause) {
      this.fail(cause instanceof Error ? cause : new Error("could not open the socket"));
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      // Authenticate with a first frame rather than a query parameter. The relay
      // supports both, but a token in the URL ends up in access logs, proxy logs
      // and browser history; this path keeps it in the frame body.
      socket.send(JSON.stringify({ type: "auth", token: this.options.token }));
      this.setStatus("syncing");

      // A socket that opens and then says nothing is a socket that will hang the
      // UI at "syncing" forever. The deadline makes that state visible.
      this.handshakeTimer = setTimeout(() => {
        this.fail(new Error("the relay accepted the socket but never sent a hello frame"));
      }, 10_000);
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      this.handleFrame(event.data);
    };

    socket.onerror = () => {
      // The browser gives no detail here on purpose. The close handler carries
      // the code, so this only records that something went wrong.
      this.options.onError?.(new Error("the brain-doc socket errored"));
    };

    socket.onclose = (event: CloseEvent) => {
      this.clearHandshakeTimer();
      if (this.closedByUs) {
        this.setStatus("closed");
        return;
      }
      // 4401/4403 mean the credential is wrong. Retrying will produce the same
      // answer forever, so the provider stops and says so instead of hammering.
      if (event.code === 4401 || event.code === 4403 || event.code === 4404) {
        this.setStatus("error", `relay refused the connection: ${event.reason || event.code}`);
        this.options.onError?.(
          new Error(event.reason || `the relay closed the socket with code ${event.code}`),
        );
        return;
      }
      this.scheduleReconnect();
    };
  }

  private handleFrame(raw: string): void {
    let frame: ServerFrame;
    try {
      frame = JSON.parse(raw) as ServerFrame;
    } catch {
      this.options.onError?.(new Error("the relay sent a frame that is not JSON"));
      return;
    }

    switch (frame.type) {
      case "hello": {
        this.clearHandshakeTimer();
        this.canWrite = frame.can_write;
        this.version = frame.snapshot.version;

        // Apply the relay's snapshot, then send our own state. Merging both is
        // what makes a reconnect safe regardless of which side is ahead.
        if (frame.snapshot.update) {
          try {
            Y.applyUpdate(this.doc, base64ToBytes(frame.snapshot.update), RELAY_ORIGIN);
          } catch (cause) {
            this.options.onError?.(
              cause instanceof Error ? cause : new Error("could not apply the relay snapshot"),
            );
          }
        }

        this.attempt = 0;
        this.setStatus("connected");

        // `off` before `on` so a reconnect does not register the listener twice.
        // Two registrations means every local edit is sent twice, which the relay
        // dedupes for events but not for doc updates — so the document would grow
        // a duplicate of every keystroke's update.
        this.doc.off("update", this.onDocUpdate);
        this.doc.on("update", this.onDocUpdate);
        this.send({
          type: "update",
          update: bytesToBase64(Y.encodeStateAsUpdate(this.doc)),
        });
        return;
      }

      case "update": {
        try {
          Y.applyUpdate(this.doc, base64ToBytes(frame.update), RELAY_ORIGIN);
          this.version = frame.version;
        } catch (cause) {
          this.options.onError?.(
            cause instanceof Error ? cause : new Error("could not apply a remote update"),
          );
        }
        return;
      }

      case "saved":
        this.version = frame.version;
        return;

      case "pong":
        return;

      case "error":
        this.options.onError?.(new Error(`[${frame.code}] ${frame.message}`));
        return;
    }
  }

  private send(frame: Record<string, unknown>): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;

    const encoded = JSON.stringify(frame);
    const size = new TextEncoder().encode(encoded).length;
    if (size > this.options.maxFrameBytes) {
      // Refused locally, with a message that says what to do. The relay would
      // also refuse it, but by then the work is already lost.
      this.options.onError?.(
        new Error(
          `an update of ${size} bytes exceeds the ${this.options.maxFrameBytes}-byte frame limit; ` +
            "the document has grown past what this socket can carry",
        ),
      );
      return;
    }
    this.socket.send(encoded);
  }

  private scheduleReconnect(): void {
    this.attempt += 1;
    // Exponential with jitter. Without the jitter, every client that dropped
    // during the same relay restart reconnects in lockstep and produces a
    // thundering herd on a service that just came back.
    const backoff = Math.min(INITIAL_BACKOFF_MS * 2 ** (this.attempt - 1), MAX_BACKOFF_MS);
    const delay = backoff / 2 + Math.random() * (backoff / 2);

    this.setStatus("reconnecting", `retrying in ${Math.round(delay)}ms`);
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  private fail(error: Error): void {
    this.options.onError?.(error);
    this.setStatus("error", error.message);
    this.clearHandshakeTimer();
    this.socket?.close();
    this.socket = null;
  }

  private setStatus(status: ProviderStatus, detail?: string): void {
    this.status = status;
    this.options.onStatus?.(status, detail);
  }

  private clearHandshakeTimer(): void {
    if (this.handshakeTimer) {
      clearTimeout(this.handshakeTimer);
      this.handshakeTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearHandshakeTimer();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

/**
 * Read a shared text value out of a doc, with a subscription.
 *
 * The brain doc's top-level shape is a `Y.Map` holding named `Y.Text` fields;
 * this narrows to one of them and returns an unsubscribe function, because a
 * React effect that subscribes without returning a teardown is a leak that
 * eventually makes every keystroke slow.
 */
export function observeText(
  doc: Y.Doc,
  key: string,
  onChange: (value: string) => void,
): () => void {
  const map = doc.getMap<Y.Text>("brain");
  let text = map.get(key);
  if (!text) {
    text = new Y.Text();
    map.set(key, text);
  }
  const target = text;

  const emit = () => onChange(target.toString());
  emit();
  target.observe(emit);
  return () => target.unobserve(emit);
}
