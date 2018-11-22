// Rendering of match state into DOM. Pure functions of (element, state).

const ICONS = {
  goal: "⚽",
  own_goal: "⚽",
  yellow: "🟨",
  red: "🟥",
  substitution: "⇄",
};

const PERIOD_LABELS = {
  pre_match: "Pre-match",
  first_half: "1st half",
  half_time: "Half-time",
  second_half: "2nd half",
  extra_time_break: "Before extra time",
  extra_time_first: "Extra time 1",
  extra_time_half_time: "ET half-time",
  extra_time_second: "Extra time 2",
  penalties: "Penalties",
  full_time: "Full-time",
};

export function periodLabel(period) {
  return PERIOD_LABELS[period] ?? period;
}

export function timelineEntries(state) {
  const entries = [];
  for (const g of state.goals) {
    const tags = [g.penalty && "pen", g.own_goal && "og"].filter(Boolean).join(", ");
    entries.push({
      minute: g.minute,
      team: g.team,
      icon: ICONS.goal,
      text: tags ? `${g.player} (${tags})` : g.player,
      cancelled: g.cancelled,
    });
  }
  for (const c of state.cards) {
    entries.push({
      minute: c.minute,
      team: c.team,
      icon: ICONS[c.colour],
      text: c.second_yellow ? `${c.player} (second yellow)` : c.player,
    });
  }
  for (const s of state.substitutions) {
    entries.push({
      minute: s.minute,
      team: s.team,
      icon: ICONS.substitution,
      text: `${s.player_on} for ${s.player_off}`,
    });
  }
  return entries.sort((a, b) => b.minute - a.minute);
}

function text(el, selector, value) {
  el.querySelector(selector).textContent = value;
}

export function renderBoard(el, state) {
  text(el, ".competition", state.competition || "Friendly");
  const period = el.querySelector(".period");
  const minute = state.status === "live" ? ` · ${state.minute}'` : "";
  period.textContent = periodLabel(state.period) + minute;
  period.classList.toggle("live", state.status === "live");
  text(el, ".team.home", state.home_team);
  text(el, ".team.away", state.away_team);
  text(el, ".home-score", String(state.score.home));
  text(el, ".away-score", String(state.score.away));
  text(el, ".version", `v${state.version}`);

  const list = el.querySelector(".timeline");
  list.replaceChildren(
    ...timelineEntries(state).map((entry) => {
      const li = document.createElement("li");
      li.className = entry.team + (entry.cancelled ? " cancelled" : "");
      for (const [cls, value] of [
        ["min", `${entry.minute}'`],
        ["icon", entry.icon],
        ["text", entry.text],
      ]) {
        const span = document.createElement("span");
        span.className = cls;
        span.textContent = value;
        li.append(span);
      }
      return li;
    }),
  );
}

export function flash(el) {
  el.classList.remove("flash");
  // Force reflow so the animation restarts.
  void el.offsetWidth;
  el.classList.add("flash");
}
