// The dashboard of `bdfr serve` (served at /): reads GET /status, /jobs, /jobs/<id>, /watchlist, /retries, /folders
// and /log with the token from this browser's storage. It only reads what serve already holds, so it never makes serve
// ask Arctic Shift for anything. Everything from the server is shown as text (textContent), never as HTML.
"use strict";

const TOKEN_KEY = "bdfr-dashboard-token";
const STATE_KEY = "bdfr-dashboard-state";
const POLL_MS = 10000;
const WATCHLIST_MS = 60000;
const LEVELS = { TRACE: 9, DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const COUNT_LABELS = ["new", "existing", "duplicate", "skipped", "failed", "unavailable"];

const $ = (id) => document.getElementById(id);

function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Private windows or blocked storage: the token is kept for this page only
  }
}

let token = storageGet(TOKEN_KEY);
const saved = (() => {
  try {
    return JSON.parse(storageGet(STATE_KEY) || "{}") || {};
  } catch {
    return {};
  }
})();
const state = {
  tab: saved.tab || "overview",
  status: null,
  jobs: [],
  watchlist: null,
  watchlistAt: 0,
  retries: null,
  folders: null,
  log: null,
  logJob: null,
  openJobs: new Set(),
  jobDetails: {},
  watchSort: saved.watchSort || { key: "name", asc: true },
  folderSort: saved.folderSort || { key: "name", asc: true },
};

function saveState() {
  storageSet(STATE_KEY, JSON.stringify({ tab: state.tab, watchSort: state.watchSort, folderSort: state.folderSort }));
}

// Builds an element; strings and numbers become text nodes, so nothing is parsed as HTML
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function fill(node, ...children) {
  node.replaceChildren();
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : String(child));
  }
}

function postLink(id, text) {
  return el("a", { href: `https://old.reddit.com/comments/${encodeURIComponent(id)}`, target: "_blank", rel: "noreferrer noopener" }, text || id);
}

function userLink(name) {
  return el("a", { href: `https://old.reddit.com/user/${encodeURIComponent(name)}/submitted`, target: "_blank", rel: "noreferrer noopener" }, name);
}

// --- Times ---

function absolute(seconds) {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function span(seconds) {
  seconds = Math.abs(seconds);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)} h`;
  if (seconds < 86400 * 60) return `${Math.round(seconds / 86400)} d`;
  if (seconds < 86400 * 730) return `${Math.round(seconds / (86400 * 30))} mo`;
  return `${(seconds / (86400 * 365)).toFixed(1)} y`;
}

function relative(seconds) {
  if (!seconds) return "";
  const diff = seconds - Date.now() / 1000;
  return diff >= 0 ? `in ${span(diff)}` : `${span(diff)} ago`;
}

// A relative time with the exact one on hover
function when(seconds, empty = "–") {
  if (!seconds) return el("span", { class: "muted" }, empty);
  return el("span", { title: absolute(seconds) }, relative(seconds));
}

function duration(job) {
  if (!job.started) return "";
  return span((job.finished || Date.now() / 1000) - job.started);
}

// --- Requests ---

class Unauthorised extends Error {}

async function api(path) {
  const response = await fetch(path, { headers: { "X-BDFR-Token": token || "" }, cache: "no-store" });
  if (response.status === 401) throw new Unauthorised();
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function showBanner(message) {
  const banner = $("banner");
  banner.hidden = !message;
  banner.textContent = message || "";
}

function showLogin(message) {
  $("login").hidden = false;
  $("tabs").hidden = true;
  $("logout").hidden = true;
  for (const view of document.querySelectorAll(".view")) view.hidden = true;
  $("login-error").textContent = message || "";
  $("token").focus();
}

async function refresh(force = false) {
  if (!token) return showLogin();
  try {
    const tasks = [api("/status").then((s) => (state.status = s))];
    if (force || state.tab === "overview") tasks.push(api("/jobs").then((j) => (state.jobs = j.jobs)));
    const watchlistDue = Date.now() - state.watchlistAt > WATCHLIST_MS;
    if (force || state.tab === "watchlist" || (state.tab === "overview" && watchlistDue)) {
      tasks.push(
        api("/watchlist").then((w) => {
          state.watchlist = w;
          state.watchlistAt = Date.now();
        }),
      );
    }
    if (state.tab === "retries") tasks.push(api("/retries").then((r) => (state.retries = r)));
    if (state.tab === "log" && (force || $("log-auto").checked)) tasks.push(loadLog());
    if (state.tab === "overview") {
      for (const id of state.openJobs) tasks.push(loadJob(id));
    }
    await Promise.all(tasks);
    $("login").hidden = true;
    $("tabs").hidden = false;
    $("logout").hidden = false;
    showBanner(null);
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    render();
  } catch (e) {
    if (e instanceof Unauthorised) {
      showLogin(token ? "That token was not accepted." : "");
      return;
    }
    showBanner(`Can't reach bdfr serve: ${e.message}. Showing the last data.`);
  }
}

async function loadJob(id) {
  try {
    state.jobDetails[id] = await api(`/jobs/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof Unauthorised) throw e;
    state.jobDetails[id] = { error: e.message, results: {} };
  }
}

async function loadLog() {
  state.log = await api(`/log?lines=${encodeURIComponent($("log-lines").value)}`);
}

async function loadFolders() {
  fill($("folders"), el("tr", {}, el("td", { class: "empty" }, "Reading hash stores…")));
  try {
    state.folders = await api("/folders");
    renderFolders();
  } catch (e) {
    if (e instanceof Unauthorised) return showLogin("That token was not accepted.");
    fill($("folders"), el("tr", {}, el("td", { class: "empty error" }, e.message)));
  }
}

// --- Rendering ---

function render() {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("active", button.dataset.tab === state.tab);
  }
  for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== `view-${state.tab}`;
  if (state.tab === "overview") renderOverview();
  if (state.tab === "watchlist") renderWatchlist();
  if (state.tab === "retries") renderRetries();
  if (state.tab === "log") renderLog();
}

function tile(label, value, sub) {
  return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), el("div", { class: "sub" }, sub || " "));
}

function counts(c, total) {
  const parts = COUNT_LABELS.filter((label) => c && c[label]).map((label) => el("span", { class: `n-${label}` }, `${c[label]} ${label}`));
  if (total) parts.push(el("span", { class: "muted" }, `of ${total}`));
  return el("span", { class: "counts" }, parts.length ? parts : el("span", { class: "muted" }, "–"));
}

function stateBadge(job) {
  const kind = { done: "ok", failed: "bad", running: "info", queued: "" }[job.state] || "";
  return el("span", { class: `badge ${kind}` }, job.state);
}

function source(job) {
  const parts = [];
  if (job.user) parts.push(el("span", {}, "u/", userLink(job.user)));
  else if (job.retry) parts.push(`${job.total} failed posts`);
  else parts.push(`${job.total} posts`);
  if (job.retry) parts.push(el("span", { class: "badge warn" }, "retry"));
  else if (job.scheduled) parts.push(el("span", { class: "badge info" }, "scheduled"));
  return parts;
}

function renderOverview() {
  const s = state.status || {};
  const w = state.watchlist;
  const users = (w && w.users) || [];
  const running = state.jobs.find((job) => job.state === "running");
  const failedDay = state.jobs.filter((job) => job.state === "failed" && job.finished > Date.now() / 1000 - 86400);
  const retries = s.retries || {};
  fill(
    $("tiles"),
    tile("Running", running ? running.folder : "idle", running ? source(running) : ""),
    tile("Queued", s.queued ?? "–", s.scheduled ? `${s.scheduled} scheduled` : ""),
    tile("Failed jobs (24 h)", failedDay.length, `of ${state.jobs.filter((j) => j.finished > Date.now() / 1000 - 86400).length} finished`),
    tile("Retry list", retries.waiting ?? "–", s.retries ? `${retries.given_up} given up` : "off"),
    tile(
      "Watched users",
      w ? (w.enabled ? users.length : "off") : "…",
      w && w.enabled ? `${users.filter((u) => u.hibernating).length} hibernating · ${(w.gone || []).length + users.filter((u) => u.gone).length} gone` : "",
    ),
    tile("Up", s.started ? span(Date.now() / 1000 - s.started) : "–", `v${s.version || "?"}${s.notify ? " · notify on" : ""}`),
  );

  const alerts = [];
  if (w && w.error) alerts.push(el("div", { class: "alert bad" }, "Watchlist: ", w.error));
  const erroring = users.filter((u) => u.last_error);
  if (erroring.length) {
    alerts.push(
      el(
        "div",
        { class: "alert" },
        `Last archive check failed for ${erroring.length} user${erroring.length > 1 ? "s" : ""}: `,
        erroring.slice(0, 8).map((u, i) => [i ? ", " : "", el("span", { title: u.last_error }, u.name)]),
        erroring.length > 8 ? ` and ${erroring.length - 8} more` : "",
      ),
    );
  }
  fill($("alerts"), alerts);

  fill($("current"), running ? currentCard(running) : null);
  renderJobs();
}

function currentCard(job) {
  const done = COUNT_LABELS.reduce((sum, label) => sum + (job.counts[label] || 0), 0);
  const card = el(
    "div",
    { class: "card" },
    el("h3", {}, "Running: ", job.folder, " ", el("span", { class: "muted mono" }, job.id)),
    el("div", {}, source(job), " · started ", when(job.started), " · ", duration(job)),
    el("div", {}, counts(job.counts, job.total)),
  );
  if (job.total) {
    const bar = el("div");
    bar.style.width = `${Math.min(100, (100 * done) / job.total)}%`;
    card.append(el("div", { class: "progress" }, bar));
  }
  return card;
}

function renderJobs() {
  const failedOnly = $("jobs-failed").checked;
  const hideScheduled = $("jobs-hide-scheduled").checked;
  const jobs = state.jobs.filter((job) => (!failedOnly || job.state === "failed") && (!hideScheduled || !job.scheduled));
  const rows = [];
  for (const job of jobs) {
    const open = state.openJobs.has(job.id);
    rows.push(
      el(
        "tr",
        { class: "clickable", onclick: () => toggleJob(job.id) },
        el("td", {}, when(job.created)),
        el("td", {}, stateBadge(job)),
        el("td", {}, job.folder),
        el("td", {}, source(job)),
        el("td", {}, counts(job.counts, job.total)),
        el("td", { class: "num" }, duration(job)),
        el("td", { class: "wrap error" }, job.error || ""),
      ),
    );
    if (open) rows.push(el("tr", { class: "details" }, el("td", { colspan: 7 }, jobDetails(job))));
  }
  fill(
    $("jobs"),
    el("thead", {}, el("tr", {}, ["Queued", "State", "Folder", "Source", "Posts", "Took", "Error"].map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 7, class: "empty" }, "No jobs since serve started"))),
  );
}

function jobDetails(job) {
  const details = state.jobDetails[job.id];
  const head = el(
    "div",
    {},
    el("span", { class: "mono" }, job.id),
    " · queued ",
    absolute(job.created),
    job.started ? [" · started ", absolute(job.started)] : "",
    job.finished ? [" · finished ", absolute(job.finished)] : "",
    " · ",
    el("a", { href: "#", onclick: (e) => (e.preventDefault(), e.stopPropagation(), showJobLog(job.id)) }, "log lines"),
  );
  if (!details) return [head, el("div", { class: "muted" }, "Loading…")];
  if (details.error && !details.results) return [head, el("div", { class: "error" }, details.error)];
  const byLabel = {};
  for (const [id, label] of Object.entries(details.results || {})) (byLabel[label] = byLabel[label] || []).push(id);
  const groups = ["failed", "unavailable", "new", "duplicate", "existing", "skipped"]
    .filter((label) => byLabel[label])
    .map((label) =>
      el(
        "div",
        {},
        el("strong", {}, `${label} (${byLabel[label].length})`),
        el("div", { class: "posts-list mono" }, byLabel[label].slice(0, 300).map((id) => postLink(id))),
        byLabel[label].length > 300 ? el("div", { class: "muted" }, `and ${byLabel[label].length - 300} more`) : "",
      ),
    );
  return [head, groups.length ? groups : el("div", { class: "muted" }, "No posts reported")];
}

function toggleJob(id) {
  if (state.openJobs.has(id)) {
    state.openJobs.delete(id);
    renderJobs();
    return;
  }
  state.openJobs.add(id);
  renderJobs();
  loadJob(id)
    .then(renderJobs)
    .catch(() => showLogin("That token was not accepted."));
}

function showJobLog(id) {
  state.logJob = id;
  switchTab("log");
}

// Sortable table headers: columns are [label, key, class]
function sortHeader(columns, sort, onSort) {
  return el(
    "thead",
    {},
    el(
      "tr",
      {},
      columns.map(([label, key, cls]) =>
        el(
          "th",
          {
            class: [cls, key ? "sortable" : "", sort.key === key ? "sorted" : "", sort.key === key && sort.asc ? "asc" : ""].filter(Boolean).join(" "),
            onclick: key ? () => onSort(key) : null,
          },
          label,
        ),
      ),
    ),
  );
}

function sortBy(rows, sort, value) {
  return rows.slice().sort((a, b) => {
    let x = value(a, sort.key);
    let y = value(b, sort.key);
    if (x === y) return 0;
    if (x === null || x === undefined || x === "") return 1;
    if (y === null || y === undefined || y === "") return -1;
    if (typeof x === "string") return sort.asc ? x.localeCompare(y) : y.localeCompare(x);
    return sort.asc ? x - y : y - x;
  });
}

function toggleSort(sort, key) {
  if (sort.key === key) sort.asc = !sort.asc;
  else Object.assign(sort, { key, asc: key === "name" || key === "folder" || key === "next_check" });
  saveState();
}

function userStatus(u) {
  const badges = [];
  if (u.alias_of) badges.push(el("span", { class: "badge", title: "Old account: never checked" }, `alias of ${u.alias_of}`));
  if (u.gone) badges.push(el("span", { class: "badge bad" }, u.gone));
  if (u.hibernating) badges.push(el("span", { class: "badge", title: "No new posts in hibernate_days: no archive checks" }, "hibernating"));
  if (u.last_error) badges.push(el("span", { class: "badge bad", title: u.last_error }, "check failed"));
  if (!badges.length) badges.push(el("span", { class: "badge ok" }, "active"));
  return badges;
}

function archiveLabel(u) {
  if (u.archive === "all") return el("span", { class: "badge info", title: "Whole archive every check" }, "all");
  if (u.archive) return el("span", { class: "badge info" }, u.full_archive_done ? "weekly" : "first read");
  return el("span", { class: "muted", title: "Fed by the userscript only" }, "off");
}

function renderWatchlist() {
  const w = state.watchlist;
  if (!w) return fill($("watch"), el("tr", {}, el("td", { class: "empty" }, "Loading…")));
  if (!w.enabled) {
    fill($("watch-settings"), "");
    fill($("watch-gone"), "");
    return fill($("watch"), el("tr", {}, el("td", { class: "empty" }, "serve has no watchlist")));
  }
  const settings = Object.entries(w).filter(([key]) => !["users", "gone", "enabled", "error"].includes(key));
  fill($("watch-settings"), settings.map(([key, value], i) => [i ? " · " : "", `${key} ${value}`]), w.error ? el("div", { class: "error" }, w.error) : "");

  const filter = $("watch-filter").value.trim().toLowerCase();
  const users = w.users.filter((u) => !filter || `${u.name} ${u.folder} ${u.alias_of || ""} ${u.last_error || ""}`.toLowerCase().includes(filter));
  const statusRank = (u) => (u.last_error ? 0 : u.gone ? 1 : u.hibernating ? 3 : u.alias_of ? 4 : 2);
  const sorted = sortBy(users, state.watchSort, (u, key) => (key === "status" ? statusRank(u) : key === "name" || key === "folder" ? u[key].toLowerCase() : u[key]));
  const columns = [
    ["User", "name"],
    ["Folder", "folder"],
    ["Archive", null],
    ["Newest post", "newest_post"],
    ["Last check", "last_success"],
    ["Next check", "next_check"],
    ["Status", "status"],
  ];
  fill(
    $("watch"),
    sortHeader(columns, state.watchSort, (key) => (toggleSort(state.watchSort, key), renderWatchlist())),
    el(
      "tbody",
      {},
      sorted.length
        ? sorted.map((u) =>
            el(
              "tr",
              {},
              el("td", {}, userLink(u.name)),
              el("td", {}, u.folder),
              el("td", {}, archiveLabel(u)),
              el("td", {}, when(u.newest_post)),
              el("td", {}, when(u.last_success), u.last_error ? el("div", { class: "error wrap" }, u.last_error) : ""),
              el("td", {}, u.archive && !u.alias_of && !u.hibernating && !u.gone ? when(u.next_check, "due") : el("span", { class: "muted" }, "–")),
              el("td", {}, userStatus(u)),
            ),
          )
        : el("tr", {}, el("td", { colspan: 7, class: "empty" }, filter ? "No matching users" : "No users in watchlist.yaml")),
    ),
  );

  const gone = (w.gone || []).filter((g) => !filter || g.name.toLowerCase().includes(filter));
  fill(
    $("watch-gone"),
    gone.length
      ? [
          el("h2", {}, `Suspended or deleted (${gone.length})`),
          el("p", { class: "muted" }, "Left out of the userscript's sync until their recheck time."),
          el(
            "div",
            { class: "table-wrap" },
            el(
              "table",
              { class: "data" },
              el("thead", {}, el("tr", {}, el("th", {}, "User"), el("th", {}, "Reported"), el("th", {}, "Recheck"))),
              el("tbody", {}, gone.map((g) => el("tr", {}, el("td", {}, userLink(g.name)), el("td", {}, el("span", { class: "badge bad" }, g.gone)), el("td", {}, when(g.recheck))))),
            ),
          ),
        ]
      : null,
  );
}

function renderRetries() {
  const r = state.retries;
  if (!r) return fill($("retries"), el("tr", {}, el("td", { class: "empty" }, "Loading…")));
  const filter = $("retry-filter").value.trim().toLowerCase();
  const posts = r.posts.filter((p) => !filter || `${p.folder} ${p.id} ${p.title || ""} ${p.error || ""} ${p.author || ""}`.toLowerCase().includes(filter));
  fill(
    $("retries"),
    el("thead", {}, el("tr", {}, ["Folder", "Post", "Tries", "Last failure", "Next try", "Reason"].map((h) => el("th", {}, h)))),
    el(
      "tbody",
      {},
      posts.length
        ? posts.map((p) =>
            el(
              "tr",
              {},
              el("td", {}, p.folder),
              el("td", { class: "wrap" }, postLink(p.id, p.id), p.title ? el("div", { class: "muted" }, p.title) : ""),
              el("td", { class: "num" }, p.attempts),
              el("td", {}, when(p.failed)),
              el("td", {}, p.pending ? el("span", { class: "badge info" }, "queued") : p.next_try ? when(p.next_try) : el("span", { class: "badge bad" }, "given up")),
              el("td", { class: "wrap error" }, p.error || ""),
            ),
          )
        : el("tr", {}, el("td", { colspan: 6, class: "empty" }, r.enabled ? (filter ? "No matching posts" : "Nothing to retry") : "serve keeps no retry list")),
    ),
  );
}

function renderFolders() {
  const f = state.folders;
  if (!f) return;
  if (f.error) return fill($("folders"), el("tr", {}, el("td", { class: "empty error" }, f.error)));
  const columns = [
    ["Folder", "name"],
    ["Store", "store"],
    ["Files", "files", "num"],
    ["Links", "links", "num"],
    ["Duplicates", "duplicates", "num"],
    ["Unavailable", "unavailable", "num"],
    ["Incomplete", "incomplete", "num"],
    ["Retrying", "retries", "num"],
  ];
  const sorted = sortBy(f.folders, state.folderSort, (row, key) => (key === "name" ? row.name.toLowerCase() : row[key]));
  const cell = (value, cls) => el("td", { class: `num ${cls || ""}` }, value ?? "–");
  fill(
    $("folders"),
    sortHeader(columns, state.folderSort, (key) => (toggleSort(state.folderSort, key), renderFolders())),
    el(
      "tbody",
      {},
      sorted.length
        ? sorted.map((row) =>
            el(
              "tr",
              {},
              el("td", {}, row.name),
              el("td", {}, row.store === "error" ? el("span", { class: "badge bad", title: row.error }, "unreadable") : row.store || el("span", { class: "muted" }, "none")),
              cell(row.files),
              cell(row.links),
              cell(row.duplicates),
              cell(row.unavailable),
              cell(row.incomplete, row.incomplete ? "error" : ""),
              cell(row.retries, row.retries ? "error" : ""),
            ),
          )
        : el("tr", {}, el("td", { colspan: 8, class: "empty" }, "No folders yet")),
    ),
  );
}

function renderLog() {
  const l = state.log;
  const chip = $("log-job");
  chip.hidden = !state.logJob;
  chip.textContent = state.logJob ? `job ${state.logJob}` : "";
  if (!l) return fill($("log"), el("div", { class: "muted line" }, "Loading…"));
  $("log-path").textContent = l.path ? `${l.path}${l.error ? ` (${l.error})` : ""}` : "serve isn't logging to a file";
  const minimum = Number($("log-level").value);
  const filter = $("log-filter").value.trim().toLowerCase();
  const entries = l.entries.filter(
    (e) =>
      (e.level === null || (LEVELS[e.level] ?? 20) >= minimum) &&
      (!state.logJob || e.job === state.logJob) &&
      (!filter || `${e.job || ""} ${e.post || ""} ${e.message}`.toLowerCase().includes(filter)),
  );
  const box = $("log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  fill(
    box,
    entries.length
      ? entries.map((e) =>
          el(
            "div",
            { class: "line" },
            el("span", { class: "t" }, e.time || ""),
            el("span", { class: `l-${e.level}` }, e.level || ""),
            el(
              "span",
              { class: `l-${e.level}` },
              e.job ? el("span", { class: "ctx", title: "Show only this job", onclick: () => ((state.logJob = e.job), renderLog()) }, `job ${e.job}`) : "",
              e.job && e.post ? ", " : "",
              e.post ? postLink(e.post, `post ${e.post}`) : "",
              e.job || e.post ? ": " : "",
              e.message,
            ),
          ),
        )
      : el("div", { class: "muted line" }, "No matching lines"),
  );
  if (atBottom || !box.dataset.scrolled) {
    box.scrollTop = box.scrollHeight;
    box.dataset.scrolled = "1";
  }
}

// --- Events ---

function switchTab(tab) {
  state.tab = tab;
  saveState();
  render();
  if (tab === "folders" && !state.folders) loadFolders();
  refresh(tab === "log" || tab === "watchlist");
}

$("tabs").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-tab]");
  if (button) switchTab(button.dataset.tab);
});

$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  storageSet(TOKEN_KEY, token);
  $("token").value = "";
  refresh(true);
});

$("logout").addEventListener("click", () => {
  token = null;
  storageSet(TOKEN_KEY, null);
  showLogin();
});

$("jobs-failed").addEventListener("change", renderJobs);
$("jobs-hide-scheduled").addEventListener("change", renderJobs);
$("watch-filter").addEventListener("input", renderWatchlist);
$("retry-filter").addEventListener("input", renderRetries);
$("folders-load").addEventListener("click", loadFolders);
$("log-level").addEventListener("change", renderLog);
$("log-filter").addEventListener("input", renderLog);
$("log-lines").addEventListener("change", () => refresh(true));
$("log-job").addEventListener("click", () => ((state.logJob = null), renderLog()));

// Polls only while the tab is visible, and at once when it comes back
setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

if (state.tab === "folders") loadFolders();
refresh(true);
