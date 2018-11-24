// Entry point: wires the match list, live boards and the feed console.

import { flash, periodLabel, renderBoard } from "./board.js";
import { ScoreSocket } from "./socket.js";

const $ = (sel) => document.querySelector(sel);
const boards = new Map();
const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
const socket = new ScoreSocket(wsUrl);

// ---------------------------------------------------------------- connection
socket.addEventListener("status", ({ detail }) => {
  $("#conn").dataset.state = detail.state;
  $("#conn-label").textContent =
    detail.state === "closed" ? `reconnecting (${detail.code})` : detail.state;
});
socket.addEventListener("rtt", ({ detail }) => {
  $("#conn-rtt").textContent = `${detail.ms} ms`;
});
socket.addEventListener("server-error", ({ detail }) => {
  setStatus(`server: ${detail.message}${detail.match_id ? ` (${detail.match_id})` : ""}`);
});
socket.addEventListener("state", ({ detail }) => {
  const board = boards.get(detail.matchId);
  if (!board) return;
  renderBoard(board, detail.state);
  if (detail.event && detail.event.type === "goal") flash(board);
});

// ---------------------------------------------------------------- following
function follow(matchId) {
  if (boards.has(matchId)) return;
  const node = $("#board-template").content.firstElementChild.cloneNode(true);
  node.dataset.matchId = matchId;
  node.querySelector(".close").addEventListener("click", () => unfollow(matchId));
  $("#boards").append(node);
  boards.set(matchId, node);
  socket.subscribe(matchId);
  syncEmptyState();
  persist();
}

function unfollow(matchId) {
  boards.get(matchId)?.remove();
  boards.delete(matchId);
  socket.unsubscribe(matchId);
  syncEmptyState();
  persist();
}

function syncEmptyState() {
  $("#boards-empty").hidden = boards.size > 0;
  for (const btn of document.querySelectorAll("#match-list button")) {
    btn.setAttribute("aria-pressed", String(boards.has(btn.dataset.matchId)));
  }
}

function persist() {
  try {
    localStorage.setItem("followed", JSON.stringify([...boards.keys()]));
  } catch {
    /* storage unavailable: following just won't survive a reload */
  }
}

function restore() {
  try {
    return JSON.parse(localStorage.getItem("followed") ?? "[]");
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------- match list
async function refreshMatches() {
  let matches = [];
  try {
    const res = await fetch("/api/matches");
    matches = (await res.json()).matches;
  } catch {
    return;
  }
  const list = $("#match-list");
  list.replaceChildren(
    ...matches.map((m) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.matchId = m.match_id;
      btn.innerHTML = `<span class="teams"></span><span class="mini-score"></span><span class="meta"></span>`;
      btn.querySelector(".teams").textContent = `${m.home_team} v ${m.away_team}`;
      btn.querySelector(".mini-score").textContent = `${m.score.home}-${m.score.away}`;
      btn.querySelector(".meta").textContent =
        `${periodLabel(m.period)} · ${m.watchers} watching here`;
      btn.addEventListener("click", () =>
        boards.has(m.match_id) ? unfollow(m.match_id) : follow(m.match_id),
      );
      li.append(btn);
      return li;
    }),
  );
  $("#match-empty").hidden = matches.length > 0;

  const select = $("#event-match");
  const current = select.value;
  select.replaceChildren(
    ...matches.map((m) => new Option(`${m.match_id}: ${m.home_team} v ${m.away_team}`, m.match_id)),
  );
  if (current) select.value = current;
  syncEmptyState();
}

// ---------------------------------------------------------------- console
function setStatus(message) {
  $("#console-status").textContent = message;
}

function authHeaders(form) {
  const token = form.querySelector("[name=token]")?.value;
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function postJson(url, body, headers) {
  const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message ?? res.statusText);
  return data;
}

const FIELDS_BY_TYPE = {
  period_change: ["period"],
  goal: ["team", "player"],
  card: ["team", "player", "card"],
  substitution: ["team", "player_off", "player_on"],
};
const OPTIONAL_FIELDS = ["period", "team", "player", "card", "player_off", "player_on"];

function syncEventFields() {
  const type = $("#event-type").value;
  for (const name of OPTIONAL_FIELDS) {
    $(`#event-form [name=${name}]`).hidden = !FIELDS_BY_TYPE[type].includes(name);
  }
}

$("#event-type").addEventListener("change", syncEventFields);

$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const body = Object.fromEntries(new FormData(form));
  if (!body.id) delete body.id;
  try {
    const tokenForm = $("#event-form");
    const state = await postJson("/api/matches", body, authHeaders(tokenForm));
    setStatus(`created ${state.match_id}`);
    form.reset();
    await refreshMatches();
    follow(state.match_id);
  } catch (err) {
    setStatus(`create failed: ${err.message}`);
  }
});

$("#event-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const raw = Object.fromEntries(new FormData(form));
  const event = { type: raw.type, minute: Number(raw.minute || 0) };
  for (const name of FIELDS_BY_TYPE[raw.type]) event[name] = raw[name];
  try {
    const res = await postJson(`/api/matches/${raw.match_id}/events`, event, authHeaders(form));
    setStatus(`accepted #${res.event.seq} (${res.event.type})`);
  } catch (err) {
    setStatus(`rejected: ${err.message}`);
  }
});

// ---------------------------------------------------------------- boot
syncEventFields();
socket.connect();
await refreshMatches();
for (const id of restore()) follow(id);
setInterval(refreshMatches, 10000);
