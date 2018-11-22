// Resilient WebSocket client for the scoreboard protocol.
//
// - Reconnects with exponential backoff and jitter.
// - Sends application-level pings; browsers answer protocol pings for us but
//   cannot tell when the *server* has gone silent, so we track that ourselves.
// - Remembers subscriptions and replays them after a reconnect.
// - Drops stale updates and requests a resync when it detects a version gap.

const MAX_BACKOFF_MS = 15000;

export class ScoreSocket extends EventTarget {
  constructor(url) {
    super();
    this.url = url;
    this.subscriptions = new Set();
    this.versions = new Map();
    this.attempt = 0;
    this.ws = null;
    this.pingTimer = null;
    this.lastSeen = 0;
    this.pingInterval = 15000;
    this.idleTimeout = 45000;
  }

  connect() {
    this.#emit("status", { state: "connecting" });
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.attempt = 0;
      this.lastSeen = Date.now();
      this.#emit("status", { state: "open" });
      for (const id of this.subscriptions) this.#send({ op: "subscribe", match_id: id });
    });
    ws.addEventListener("message", (e) => this.#onMessage(e.data));
    ws.addEventListener("close", (e) => this.#onClose(e));
  }

  subscribe(matchId) {
    this.subscriptions.add(matchId);
    this.#send({ op: "subscribe", match_id: matchId });
  }

  unsubscribe(matchId) {
    this.subscriptions.delete(matchId);
    this.versions.delete(matchId);
    this.#send({ op: "unsubscribe", match_id: matchId });
  }

  #send(payload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  #emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  #onMessage(raw) {
    this.lastSeen = Date.now();
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    switch (msg.type) {
      case "hello":
        this.pingInterval = msg.ping_interval * 1000;
        this.idleTimeout = msg.idle_timeout * 1000;
        this.#startPinging();
        break;
      case "pong":
        if (typeof msg.ts === "number") this.#emit("rtt", { ms: Date.now() - msg.ts });
        break;
      case "snapshot":
        this.#accept(msg, true);
        break;
      case "event":
        this.#accept(msg, false);
        break;
      case "error":
        this.#emit("server-error", msg);
        break;
      default:
        break;
    }
  }

  #accept(msg, isSnapshot) {
    if (!this.subscriptions.has(msg.match_id)) return;
    const known = this.versions.get(msg.match_id) ?? 0;
    if (msg.version < known || (!isSnapshot && msg.version === known)) return;
    if (!isSnapshot && known && msg.version > known + 1) {
      // Some updates were shed by the server; the state in this message is
      // still complete, but ask for a snapshot so the timeline is exact.
      this.#send({ op: "resync", match_id: msg.match_id });
    }
    this.versions.set(msg.match_id, msg.version);
    this.#emit("state", { matchId: msg.match_id, state: msg.state, event: msg.event ?? null });
  }

  #startPinging() {
    clearInterval(this.pingTimer);
    const every = Math.max(1000, this.pingInterval / 2);
    this.pingTimer = setInterval(() => {
      if (Date.now() - this.lastSeen > this.idleTimeout) {
        // Server went quiet: force a reconnect instead of waiting for TCP.
        this.ws.close(4000, "client idle timeout");
        return;
      }
      this.#send({ op: "ping", ts: Date.now() });
    }, every);
  }

  #onClose(event) {
    clearInterval(this.pingTimer);
    this.versions.clear();
    this.#emit("status", { state: "closed", code: event.code, reason: event.reason });
    const base = Math.min(MAX_BACKOFF_MS, 500 * 2 ** this.attempt);
    const delay = base / 2 + Math.random() * (base / 2);
    this.attempt += 1;
    setTimeout(() => this.connect(), delay);
  }
}
