const data = JSON.parse(document.getElementById("transcript-data").textContent);
const state = { query: "", dense: false, expandDetails: false, collapsedSessions: new Set() };
const sessionById = new Map(data.sessions.map(session => [session.id, session]));
const DETAIL_GROUPS = [
  { id: "commands", label: "Commands" },
  { id: "results", label: "Results" },
  { id: "patches", label: "Patches" },
  { id: "web", label: "Web" },
  { id: "thinking", label: "Thinking" },
  { id: "status", label: "Status" },
  { id: "tools", label: "Other tools" }
];
const detailGroupById = new Map(DETAIL_GROUPS.map(group => [group.id, group]));

function unique(values) {
  return Array.from(new Set(values.filter(Boolean))).sort();
}

function isCoreEvent(event) {
  return event.kind === "message" && (event.role === "user" || event.role === "assistant");
}

function detailGroupForEvent(event) {
  if (isCoreEvent(event)) return "core";
  const title = String(event.title || "").toLowerCase();
  if (event.kind === "command") return "commands";
  if (event.kind === "file_edit") return "patches";
  if (event.kind === "tool_result" && title.includes("web")) return "web";
  if (event.kind === "tool_use" && title.includes("web")) return "web";
  if (event.kind === "tool_result") return "results";
  if (event.kind === "thinking") return "thinking";
  if (event.kind === "status") return "status";
  return "tools";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));
}

function makeCheckboxes(containerId, name, values, labeler = value => value) {
  const container = document.getElementById(containerId);
  container.innerHTML = values.map(value => `
    <label class="chip" title="${escapeHtml(value)}">
      <input type="checkbox" name="${name}" value="${escapeHtml(value)}" checked>
      <span>${escapeHtml(labeler(value))}</span>
    </label>
  `).join("");
  container.querySelectorAll("input").forEach(input => input.addEventListener("change", () => {
    render();
  }));
}

function selectedValues(name) {
  return new Set(Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(input => input.value));
}

function initFilters() {
  makeCheckboxes("provider-filter", "provider", unique(data.sessions.map(s => s.provider)));
  makeCheckboxes("day-filter", "day", unique(data.events.map(e => e.day)));
  makeCheckboxes("repo-filter", "repo", unique(data.sessions.map(s => s.repo)), value => value.split("/").filter(Boolean).slice(-2).join("/") || value);
  const detailGroups = DETAIL_GROUPS
    .map(group => group.id)
    .filter(id => data.events.some(event => detailGroupForEvent(event) === id));
  makeCheckboxes("detail-filter", "detail", detailGroups, value => detailGroupById.get(value)?.label || value);

  document.getElementById("search-input").addEventListener("input", event => {
    state.query = event.target.value.toLowerCase().trim();
    render();
  });
  document.getElementById("dense-toggle").addEventListener("change", event => {
    state.dense = event.target.checked;
    render();
  });
  document.getElementById("expand-details-toggle").addEventListener("change", event => {
    state.expandDetails = event.target.checked;
    applyDetailExpansion();
  });
}

function filteredEvents() {
  const providers = selectedValues("provider");
  const days = selectedValues("day");
  const repos = selectedValues("repo");
  const details = selectedValues("detail");
  return data.events.filter(event => {
    const session = sessionById.get(event.session_id);
    if (!session) return false;
    if (!providers.has(event.provider)) return false;
    if (!days.has(event.day)) return false;
    if (!repos.has(session.repo)) return false;
    if (!isCoreEvent(event) && !details.has(detailGroupForEvent(event))) return false;
    if (state.query) {
      const haystack = [
        event.title,
        event.body,
        event.role,
        event.kind,
        session.label,
        session.cwd,
        session.repo
      ].join("\n").toLowerCase();
      if (!haystack.includes(state.query)) return false;
    }
    return true;
  });
}

function render() {
  document.body.classList.toggle("dense", state.dense);
  const events = filteredEvents();
  const activeSessionIds = unique(events.map(event => event.session_id));
  const activeSessions = data.sessions.filter(session => activeSessionIds.includes(session.id));
  document.getElementById("summary").textContent =
    `${events.length} events on page ${data.page} of ${data.total_pages} · ${activeSessions.length} sessions on this page`;
  const app = document.getElementById("app");
  if (!events.length) {
    app.innerHTML = `<div class="empty">No events match the current filters.</div>`;
    return;
  }
  app.innerHTML = renderLanes(activeSessions, events);
  wireLaneHeaders();
  wireExpandButtons();
}

function renderLanes(sessions, events) {
  if (!sessions.length) {
    return `<div class="empty">No sessions have events on this page.</div>`;
  }
  const laneCols = sessions
    .map(session => state.collapsedSessions.has(session.id) ? "28px" : "minmax(min(100%, 320px), 800px)")
    .join(" ");
  const columns = `112px ${laneCols}`;
  const byMinute = new Map();
  events.forEach(event => {
    if (!byMinute.has(event.display_minute)) byMinute.set(event.display_minute, []);
    byMinute.get(event.display_minute).push(event);
  });
  const minutes = Array.from(byMinute.keys()).sort();
  const headers = `<div class="corner-cell"></div>${sessions.map(session => {
    const collapsed = state.collapsedSessions.has(session.id);
    const classes = `lane-header ${escapeHtml(session.provider)}${collapsed ? " collapsed" : ""}`;
    const toggleIcon = collapsed
      ? `<span class="lane-toggle-icon" aria-hidden="true">›</span>`
      : `<span class="lane-toggle-icon" aria-hidden="true">‹</span>`;
    const title = collapsed
      ? toggleIcon
      : `<div class="session-title">${escapeHtml(session.label)}</div>
         <div class="session-meta">${escapeHtml(session.cwd)}</div>
         ${toggleIcon}`;
    const aria = collapsed ? "false" : "true";
    return `
    <button type="button" class="${classes}" data-session-id="${escapeHtml(session.id)}" aria-expanded="${aria}" title="${escapeHtml(session.label)}${collapsed ? " (click to expand)" : " (click to collapse)"}">
      ${title}
    </button>
  `;
  }).join("")}`;
  const rows = minutes.map(minute => {
    const minuteEvents = byMinute.get(minute);
    const cells = sessions.map(session => {
      const collapsed = state.collapsedSessions.has(session.id);
      if (collapsed) {
        return `<div class="lane-cell lane-cell-collapsed"></div>`;
      }
      const cellEvents = minuteEvents.filter(event => event.session_id === session.id);
      return `<div class="lane-cell">${cellEvents.map(renderEventCard).join("")}</div>`;
    }).join("");
    return `<div class="time-cell">${escapeHtml(minute)}</div>${cells}`;
  }).join("");
  return `<div class="timeline-scroll"><div class="lane-grid" style="grid-template-columns: ${columns}">${headers}${rows}</div></div>`;
}

function wireLaneHeaders() {
  document.querySelectorAll(".lane-header[data-session-id]").forEach(header => {
    header.addEventListener("click", () => {
      const sessionId = header.dataset.sessionId;
      if (state.collapsedSessions.has(sessionId)) {
        state.collapsedSessions.delete(sessionId);
      } else {
        state.collapsedSessions.add(sessionId);
      }
      render();
    });
  });
}

function renderEventCard(event) {
  const preKinds = new Set(["command", "tool_use", "tool_result", "file_edit", "status"]);
  const isCore = isCoreEvent(event);
  const detailGroup = detailGroupForEvent(event);
  const expanded = isCore || state.expandDetails;
  const body = preKinds.has(event.kind)
    ? `<pre>${escapeHtml(event.body)}</pre>`
    : (event.body_html || escapeHtml(event.body));
  const classes = [
    "event-card",
    isCore ? "core-event" : "detail-event",
    expanded ? "detail-expanded" : "detail-collapsed",
    `detail-${detailGroup}`,
    event.role,
    event.kind,
    event.provider,
    event.is_error ? "error" : ""
  ].join(" ");
  const detailsToggle = isCore ? "" : `<button class="expand" type="button" aria-expanded="${expanded ? "true" : "false"}">${expanded ? "Hide details" : "Show details"}</button>`;
  return `
    <article class="${classes}" id="${escapeHtml(event.id)}">
      <div class="event-head">
        <div class="event-title">${escapeHtml(event.title)}</div>
        <div class="event-actions">
          <a class="event-time" href="#${escapeHtml(event.id)}">${escapeHtml(event.display_time.split(" ")[1] || event.display_time)}</a>
          ${detailsToggle}
        </div>
      </div>
      <div class="event-body"${expanded ? "" : " hidden"}>${body}</div>
    </article>
  `;
}

function setDetailCardExpanded(card, expanded) {
  const body = card.querySelector(".event-body");
  const button = card.querySelector(".expand");
  if (!body || !button) return;
  card.classList.toggle("detail-collapsed", !expanded);
  card.classList.toggle("detail-expanded", expanded);
  body.hidden = !expanded;
  button.textContent = expanded ? "Hide details" : "Show details";
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function applyDetailExpansion() {
  document.querySelectorAll(".event-card.detail-event").forEach(card => {
    setDetailCardExpanded(card, state.expandDetails);
  });
}

function wireExpandButtons() {
  document.querySelectorAll(".event-card.detail-event").forEach(card => {
    const button = card.querySelector(".expand");
    if (!button) return;
    setDetailCardExpanded(card, state.expandDetails);
    button.addEventListener("click", () => {
      setDetailCardExpanded(card, card.classList.contains("detail-collapsed"));
    });
  });
}

initFilters();
render();
