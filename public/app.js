// MDE Electives Planner -- front end.
//
// Personal state (profile, locked schedule, plan) lives in this browser via
// store.js and is sent with each request. The server is stateless: it owns the
// catalog and the policy engine, and both conflict math and eligibility are
// evaluated there because they need the full 7,600-section catalog.
//
// There is no login. Nothing here identifies the student to the server.

import { Store } from "./store.js";
import { parseCalendar, parsePasted } from "./calendar.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const GRID_DAYS = [1, 2, 3, 4, 5];
const DAY_START = 8 * 60, DAY_END = 22 * 60;
const SLOT = 30, PX_PER_MIN = 1;

const CALENDAR_URL = "https://my.harvard.edu/calendar/load/";

const state = {
  meta: null, policy: null,
  results: [], locked: [], plan: new Map(), hiddenTba: 0,
  preview: null, offset: 0, limit: 100, total: 0,
  electivesThisTerm: 2,
  extension: false,        // set true when the extension announces itself
};

const fmt = (m) => {
  const h = Math.floor(m / 60), mi = m % 60;
  return `${h % 12 || 12}:${String(mi).padStart(2, "0")}${h < 12 ? "am" : "pm"}`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// my.harvard stores detail_url as "/course/COMPSCI2360R/2026-Fall/001".
const MYHARVARD = "https://my.harvard.edu";
const courseUrl = (c) => (c && c.detail_url) ? MYHARVARD + c.detail_url : null;
const extLink = (c, label, cls = "") => {
  const u = courseUrl(c);
  return u
    ? `<a class="${cls} ext" href="${esc(u)}" target="_blank" rel="noopener noreferrer"
         title="Open ${esc(c.code || label)} on my.harvard">${label}<span class="exti">↗</span></a>`
    : `<span class="${cls}">${label}</span>`;
};

const term = () => $("#term").value;
// "2027 Spring" -> "Spring". The catalog term and the profile's semester are
// separate things, and the policy keys off the profile -- see renderSeasonWarn.
const termSeason = () => term().trim().split(/\s+/).pop();
const buffer = () => parseInt($("#buffer").value, 10) || 0;

/** The three pieces of personal state every endpoint needs. */
const personal = () => ({
  profile: Store.profileForApi(),
  locked: Store.lockedForApi(term()),
  plan: Store.plan(term()),
});

async function get(path, params = {}) {
  const url = new URL(path, location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (typeof v === "boolean") url.searchParams.set(k, v ? "true" : "false");
    else if (v !== "" && v != null) url.searchParams.set(k, v);
  });
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail; } catch { /* non-JSON error */ }
    throw new Error(detail || `${path} -> ${res.status}`);
  }
  return res.json();
}

function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 5000);
}

// ------------------------------------------------------------------ boot ---

async function boot() {
  state.meta = await get("/api/meta");
  state.policy = state.meta.policy;

  const terms = state.meta.terms.length
    ? state.meta.terms.map((t) => t.term) : [state.meta.default_term];
  const savedTerm = Store.getSetting("term", "");
  const chosen = terms.includes(savedTerm) ? savedTerm : state.meta.default_term;
  $("#term").innerHTML = terms
    .map((t) => `<option ${t === chosen ? "selected" : ""}>${esc(t)}</option>`).join("");

  $("#buffer").value = String(Store.getSetting("buffer_min", 15));

  $("#school").innerHTML = `<option value="">All schools</option>` +
    state.meta.schools.map((s) => `<option>${esc(s)}</option>`).join("");

  // Only the requirements a course can actively satisfy are useful as filters.
  const filterable = state.policy.requirements.filter(
    (r) => !["outside_harvard", "independent_study"].includes(r.id));
  $("#requirement").innerHTML = `<option value="">Any requirement</option>` +
    filterable.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");

  $("#pAreas").innerHTML = state.policy.seas_subjects.map((s) =>
    `<label><input type="checkbox" value="${s}">${s}</label>`).join("");

  const missing = state.policy.missing_lists || [];
  if (missing.length) {
    const el = $("#listBanner");
    el.hidden = false;
    el.innerHTML = `<b>${missing.length} approved course list(s) not loaded</b> ` +
      `(${missing.map(esc).join(", ")}). Requirements that depend on them are reported ` +
      `as “cannot verify” rather than guessed — fill them in at <code>approved_lists.yaml</code>.`;
  }

  renderMeta();

  if (!Store.storageAvailable) {
    toast("This browser is blocking local storage, so your plan won't be " +
          "remembered after you close the tab.", "warn");
  }

  // Ask the extension to identify itself. It also announces itself unprompted,
  // but the two can race: the content script runs at document_idle and this
  // module is deferred, so whichever is late needs the other's prompt.
  window.postMessage({ source: "mde-page", type: "MDE_EXT_PING" }, location.origin);

  renderGrid();
  await refreshAll();

  // First run: nudge toward the two things that make the tool useful.
  renderOnboarding();
  maybeWelcome();
}

async function refreshAll() {
  renderProfile();
  renderOnboarding();
  loadLocked();
  await Promise.all([loadChanges(), search()]);
  await loadPlan();
}

// ------------------------------------------------------------- onboarding ---
//
// Two things gate usefulness: an imported schedule (nothing to route around
// without it) and a profile (eligibility is wrong without it). The welcome
// modal makes that unmissable once; the banner is the standing reminder, so the
// modal never has to appear twice.

function onboardingTodo() {
  return {
    needsImport: !Store.hasImported(term()),
    needsProfile: !Store.getProfile(),
  };
}

/** Show/hide the reminder banner from state. Never call `.hidden` directly. */
function renderOnboarding() {
  const { needsImport, needsProfile } = onboardingTodo();
  const banner = $("#firstRun");
  banner.hidden = !(needsImport || needsProfile);

  // Tailor the banner: once one step is done, stop asking for it.
  $("#firstRunImport").hidden = !needsImport;
  $("#firstRunProfile").hidden = !needsProfile;
  $("#firstRunText").innerHTML = needsImport && needsProfile
    ? `<b>Two things make this useful.</b> Import your enrolled classes so search
       can route around them, and set your background so eligibility is accurate.
       Everything stays in this browser — there is no account and nothing is uploaded.`
    : needsImport
      ? `<b>Import your enrolled classes</b> so search can route around them.
         Without them, every course looks like it fits.`
      : `<b>Set your background.</b> The MDE rules branch on it, so eligibility
         is a guess until you do.`;
}

/** Open the welcome modal on load, at most once per browser. */
function maybeWelcome() {
  if (!onboardingTodo().needsImport) return;
  if (Store.getSetting("welcomeSeen", false)) return;
  $("#welcomeModal").hidden = false;
}

function dismissWelcome() {
  $("#welcomeModal").hidden = true;
  Store.setSetting("welcomeSeen", true);
}

// ------------------------------------------------------------ catalog meta ---
//
// The header used to spell out sections / cross-listed groups / policy version /
// an ISO timestamp. Only one of those answers a question anyone actually has --
// "is this data current?" -- so that's the line, in words, and the rest moved
// behind the info dot.

const RELATIVE_UNITS = [
  ["year",   365 * 24 * 3600],
  ["month",   30 * 24 * 3600],
  ["week",     7 * 24 * 3600],
  ["day",          24 * 3600],
  ["hour",              3600],
  ["minute",              60],
  ["second",               1],
];

/** "3 hours ago", "yesterday", "just now". Null if the timestamp is unusable. */
function timeAgo(iso) {
  if (!iso) return null;
  const then = new Date(iso);
  if (isNaN(then.getTime())) return null;

  const diff = (then.getTime() - Date.now()) / 1000;   // negative = past
  if (Math.abs(diff) < 45) return "just now";

  const fmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, secs] of RELATIVE_UNITS) {
    if (Math.abs(diff) >= secs || unit === "second") {
      return fmt.format(Math.round(diff / secs), unit);
    }
  }
  return null;
}

function renderMeta() {
  const li = state.meta.last_ingest;
  const stamp = li?.finished_at;
  const ago = timeAgo(stamp);

  $("#metaAgo").textContent = ago ? `Catalog updated ${ago}` : "Catalog date unknown";
  // Exact time on hover, since "2 days ago" is the wrong resolution during
  // shopping week when course times move daily.
  $("#metaAgo").title = stamp ? new Date(stamp).toLocaleString() : "";

  const when = stamp
    ? new Date(stamp).toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : "unknown";
  const rows = [
    [`${(state.meta.terms[0]?.n ?? 0).toLocaleString()} sections`, state.meta.terms[0]?.term || ""],
    [`${state.meta.crosslist_groups} cross-listed groups`, "heuristic"],
    [`Policy rev. ${state.policy.policy_version}`, ""],
    ["Last ingest", when],
  ];
  $("#metaDetails").innerHTML = rows.map(
    ([a, b]) => `<div><b>${esc(a)}</b>${b ? `<span>${esc(b)}</span>` : ""}</div>`).join("");
  $("#metaInfo").hidden = false;
}

function toggleMeta(force) {
  const pop = $("#metaDetails");
  const open = force !== undefined ? force : pop.hidden;
  pop.hidden = !open;
  $("#metaInfo").setAttribute("aria-expanded", String(open));
}

// --------------------------------------------------------------- profile ---

function electivesThisTerm() {
  const p = Store.getProfile();
  const map = state.policy?.electives_by_term || {};
  return map[`${p?.year ?? 1}-${p?.season ?? "Fall"}`] ?? 2;
}

function renderProfile() {
  const p = Store.getProfile();
  const box = $("#profileSummary");
  state.electivesThisTerm = electivesThisTerm();

  if (!p) {
    box.innerHTML = `<span class="muted">Not set — results assume Year 1 Fall with
      no SEAS or design background. <b>Set it</b> to get accurate eligibility.</span>`;
  } else {
    box.innerHTML = `
      <span class="pl"><b>Year ${p.year}, ${esc(p.season)}</b></span>
      <span class="pl">SEAS background: ${p.seas_background
        ? `yes${p.seas_areas.length ? " (" + p.seas_areas.map(esc).join(", ") + ")" : ""}` : "no"}</span>
      <span class="pl">Physical design background: ${p.physical_design_background ? "yes" : "no"}</span>
      <span class="pl">CS50: ${esc(p.cs50_status.replace(/_/g, " "))}</span>
      ${p.completed_codes.length
        ? `<span class="pl muted">${p.completed_codes.length} completed elective(s)</span>` : ""}`;
  }

  const n = state.electivesThisTerm;
  $("#electivesHint").textContent = p
    ? `Year ${p.year} ${p.season}: ${n} elective${n === 1 ? "" : "s"} this term.`
    : "";
  $("#pick").innerHTML = [1, 2, 3, 4].map((v) =>
    `<option ${v === n ? "selected" : ""}>${v}</option>`).join("");
  renderSeasonWarn();
}

/** Flag a profile semester that disagrees with the term being browsed.
 *
 * electives_by_term and cores_by_term are keyed "{year}-{season}" off the
 * PROFILE, not off the selected catalog term. So a Year 2 student browsing
 * Spring 2027 with a Fall profile is told they need 3 electives when the
 * answer is 2, and is shown the wrong core courses. This was invisible while
 * only one term had been ingested; with two, it needs saying out loud rather
 * than silently guessing which one the student meant.
 */
function renderSeasonWarn() {
  const p = Store.getProfile();
  const ts = termSeason();
  const box = $("#seasonWarn");
  const mismatch = p && (ts === "Fall" || ts === "Spring") && p.season !== ts;
  box.hidden = !mismatch;
  if (!mismatch) return;
  $("#seasonWarnText").textContent =
    `You're browsing ${term()}, but your background says ${p.season}. ` +
    `The elective count and core courses shown are for your ${p.season} semester.`;
  $("#seasonFix").textContent = `Switch my background to ${ts}`;
}

async function applySeasonFix() {
  const p = Store.getProfile();
  if (!p) return;
  Store.setProfile({ ...p, season: termSeason() });
  renderProfile();
  await Promise.all([search(), loadPlan()]);
}

function openProfile() {
  const p = Store.getProfile() || {};
  $("#pYear").value = p.year || 1;
  $("#pSeason").value = p.season || "Fall";
  $("#pSeasBg").checked = !!p.seas_background;
  $("#pDesignBg").checked = !!p.physical_design_background;
  $("#pCs50").value = p.cs50_status || "required";
  $("#pCompleted").value = (p.completed_codes || []).join(", ");
  $$("#pAreas input").forEach((i) => { i.checked = (p.seas_areas || []).includes(i.value); });
  $("#pAreasWrap").hidden = !$("#pSeasBg").checked;
  $("#profileModal").hidden = false;
}

async function saveProfile() {
  Store.setProfile({
    year: parseInt($("#pYear").value, 10),
    season: $("#pSeason").value,
    seas_background: $("#pSeasBg").checked,
    seas_areas: $$("#pAreas input").filter((i) => i.checked).map((i) => i.value),
    physical_design_background: $("#pDesignBg").checked,
    cs50_status: $("#pCs50").value,
    completed_codes: $("#pCompleted").value.split(",").map((s) => s.trim()).filter(Boolean),
  });
  $("#profileModal").hidden = true;
  renderOnboarding();
  renderProfile();
  await Promise.all([search(), loadPlan()]);
}

// -------------------------------------------------------------- schedule ---

function loadLocked() {
  state.locked = Store.locked(term());
  renderLocked();
  renderGrid();
}

function renderLocked() {
  const ul = $("#lockedList");
  if (!state.locked.length) {
    ul.innerHTML = `<li style="background:none;color:var(--muted);font-size:12px">
      Nothing locked yet. <b>Import my classes</b> above, or add a block by hand.</li>`;
    return;
  }
  ul.innerHTML = state.locked.map((i) => {
    const custom = i.source === "manual";
    const tag = custom
      ? `<span class="tag">${i.category === "course" ? "outside course" : "added"}</span> ` : "";
    const dates = i.start_date && custom
      ? `<br><span class="tag">${esc(i.start_date)} → ${esc(i.end_date || "")}</span>` : "";
    const days = (i.days || DAYS.filter((_, d) => i.day_mask & (1 << d)));
    return `<li class="${custom ? "custom" : ""}"><span>${tag}${
      extLink(i, esc(i.title || i.code))}
      <br><small style="color:var(--muted)">
      ${days.map((d) => d.slice(0, 3)).join(" ")} ${fmt(i.start_min)}–${fmt(i.end_min)}
      </small>${dates}</span>
      <button class="rm" data-id="${esc(i.id)}" title="Remove">✕</button></li>`;
  }).join("");
  $$("#lockedList .rm").forEach((b) => b.addEventListener("click", async () => {
    Store.removeLocked(term(), b.dataset.id);
    loadLocked();
    await Promise.all([refreshResults(), loadPlan()]);
  }));
}

async function loadChanges() {
  const d = await get("/api/changes", { term: term(), limit: 12 });
  $("#changeList").innerHTML = d.changes.length
    ? d.changes.map((c) => `<li><b>${esc(c.code)} ${esc(c.section)}</b><br>
        <span class="arrow">${esc(c.old_value)} → ${esc(c.new_value)}</span></li>`).join("")
    : `<li style="border:0;color:var(--muted)">No time changes recorded yet.</li>`;
}

// ---------------------------------------------------------------- search ---

function searchBody(extra = {}) {
  return {
    ...personal(),
    term: term(), q: $("#q").value.trim(), school: $("#school").value,
    requirement: $("#requirement").value,
    free_only: $("#freeOnly").checked,
    include_no_credit: $("#includeNoCredit").checked,
    project_based: $("#fProjectBased").checked,
    technical: $("#fTechnical").checked,
    include_tba: $("#includeTba").checked, buffer_min: buffer(),
    ...extra,
  };
}

async function search(append = false) {
  if (!append) state.offset = 0;
  const d = await post("/api/search",
    searchBody({ limit: state.limit, offset: state.offset }));
  state.total = d.total;
  state.electivesThisTerm = d.electives_this_term;
  state.hiddenTba = d.hidden_tba || 0;
  state.results = append ? state.results.concat(d.results) : d.results;
  renderResults();
}

// Re-fetch what's already on screen, keeping the user's scroll depth.
// Needed because plan membership changes the `plan_conflicts` annotation on
// OTHER courses -- re-rendering from the cached results would leave every row
// showing a stale badge until the next search.
async function refreshResults() {
  const shown = Math.max(state.limit, state.results.length);
  const d = await post("/api/search", searchBody({ limit: shown, offset: 0 }));
  state.total = d.total;
  state.hiddenTba = d.hidden_tba || 0;
  state.results = d.results;
  renderResults();
}

function policyBadges(pol) {
  if (!pol) return "";
  // Cores get their own explicit badge; don't also shout "doesn't count".
  if (pol.is_core) return "";
  if (!pol.counts_at_all) return `<span class="badge nocount">doesn't count</span>`;
  const names = Object.fromEntries(state.policy.requirements.map((r) => [r.id, r.name]));
  return pol.satisfies.filter((v) => v.verdict === "yes" || v.verdict === "verify")
    .map((v) => `<span class="badge ${v.verdict === "yes" ? "req" : "reqmaybe"}"
      title="${esc(v.reason)}">${esc(names[v.requirement_id] || v.requirement_id)}${
      v.verdict === "verify" ? " ?" : ""}</span>`).join("");
}

function courseCard(c, inPlan) {
  const times = c.meetings.length
    ? c.meetings.map((m) => `${m.days.map((d) => d.slice(0, 3)).join(" ")} ${fmt(m.start_min)}–${fmt(m.end_min)}`).join(" · ")
    : "No fixed meeting time";
  // Three distinct states, most severe first:
  //   locked clash  -> collides with an enrolled course or a custom block
  //   plan overlap  -> collides with something you're only considering
  //   fits          -> clear
  const clashes = c.conflicts || [];
  const planClashes = c.plan_conflicts || [];
  const fit = !c.meetings.length
    ? `<span class="badge tba">TBA</span>`
    : clashes.length
      ? `<span class="badge clash">clashes: ${esc(clashes.join(", "))}</span>`
      : planClashes.length
        ? `<span class="badge planclash" title="Overlaps a course in your plan — still selectable">overlaps plan: ${esc(planClashes.join(", "))}</span>`
        : `<span class="badge fits">fits</span>`;
  const pol = c.policy || {};
  const lists = [
    pol.is_project_based ? `<span class="badge listed" title="On the MDE approved project-based list (rule 1a)">project-based</span>` : "",
    pol.is_technical ? `<span class="badge listed" title="On the MDE approved technical list (rule 2)">technical</span>` : "",
  ].join("");
  const extra = pol.is_core
    ? `<span class="badge level">core — not an elective</span>`
    : (pol.counts_at_all && !(pol.satisfied_ids || []).length
        ? `<span class="badge nocount">counts toward nothing</span>` : "");
  const warns = (pol.warnings || []).length
    ? `<div class="warns">${pol.warnings.map((w) => `<div>⚠ ${esc(w)}</div>`).join("")}</div>` : "";
  return `
    <li data-key="${esc(c.key)}" class="${
      !clashes.length && planClashes.length ? "has-plan-clash" : ""}">
      <div class="r-top">
        <div>${extLink(c, `${esc(c.subject)} ${esc(c.catalog)}`, "r-code")}
             ${extLink(c, esc(c.title), "r-title")}</div>
        <div>${fit}</div>
      </div>
      <div class="r-meta">
        ${esc(times)} · ${esc(c.school || "—")} · ${esc(c.session || "")}
        ${c.instructors.length ? " · " + esc(c.instructors.join(", ")) : ""}
        <span class="badge level" title="${esc(pol.level_label || "")}">${esc(pol.level_label || "")}</span>
        ${policyBadges(pol)}${lists}${extra}
        ${c.enrolled ? `<span class="badge req">enrolled</span>`
          : `<button class="pin ghost" data-plan="${esc(c.key)}">${
              inPlan ? "Remove" : "Add to plan"}</button>`}
      </div>
      ${warns}
    </li>`;
}

function renderResults() {
  $("#resultCount").textContent =
    `${state.total.toLocaleString()} matching section${state.total === 1 ? "" : "s"}` +
    (state.results.length < state.total ? ` — showing ${state.results.length}` : "");
  $("#loadMore").hidden = state.results.length >= state.total;

  // A school can be entirely untimed early in a planning cycle -- GSD had 153
  // Spring 2027 sections and not one meeting time. Since "include TBA" is off
  // by default, saying nothing here reads as "nothing is offered".
  const n = state.hiddenTba || 0;
  const note = $("#tbaNote");
  note.hidden = !n;
  if (n) {
    note.innerHTML = `${n.toLocaleString()} more course${n === 1 ? "" : "s"} would
      match, but ${n === 1 ? "has" : "have"} no published meeting time yet —
      common before a term's schedule is finalised.
      <button id="tbaShow" class="ghost tiny">Show them</button>`;
    $("#tbaShow").addEventListener("click", () => {
      $("#includeTba").checked = true;
      search();
    });
  }

  const ul = $("#results");
  if (!state.results.length) {
    ul.innerHTML = `<li class="empty" style="border:0;cursor:default">
      Nothing matches. Try widening the filters, or unchecking “only courses that fit”.</li>`;
    return;
  }
  ul.innerHTML = state.results
    .map((c) => courseCard(c, Store.inPlan(term(), c.key))).join("");
  wireCards("#results", state.results);
}

function wireCards(sel, source) {
  $$(`${sel} li`).forEach((li) => {
    const c = source.find((r) => r.key === li.dataset.key);
    if (!c) return;
    li.addEventListener("mouseenter", () => { state.preview = c; renderGrid(); });
    li.addEventListener("mouseleave", () => { state.preview = null; renderGrid(); });
  });
  $$(`${sel} [data-plan]`).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    await togglePlan(b.dataset.plan);
  }));
}

// ------------------------------------------------------------------ plan ---

async function togglePlan(key) {
  Store.togglePlan(term(), key);
  await loadPlan();           // updates state.plan first...
  await refreshResults();     // ...so the re-render reads the new membership
  renderGrid();
}

async function loadPlan() {
  const keys = Store.plan(term());
  const locked = Store.lockedForApi(term());
  // Skip the round trip when there is nothing at all to report on.
  if (!keys.length && !locked.some((l) => l.course_key)) {
    state.plan = new Map();
    $("#planCount").textContent = "0";
    renderPlan();
    $("#planReport").innerHTML = `<p class="hint">Add courses to your plan to see
      the full requirement check.</p>`;
    renderGrid();
    return;
  }
  const d = await post("/api/plan", { ...personal(), term: term(), buffer_min: buffer() });
  state.plan = new Map(d.items.map((c) => [c.key, c]));
  state.electivesThisTerm = d.electives_this_term;
  $("#planCount").textContent = String(keys.length);
  renderPlan();
  renderGrid();
  renderReport(d.report);
}

function renderPlan() {
  const items = Array.from(state.plan.values());
  $("#planList").innerHTML = items.length
    ? items.map((c) => courseCard(c, !c.enrolled)).join("")
    : `<li class="empty" style="border:0;cursor:default">
        No courses in your plan yet. Add them from the course list.</li>`;
  wireCards("#planList", items);
}

function renderReport(r) {
  const box = $("#planReport");
  if (!r) { box.innerHTML = ""; return; }

  const list = (arr, cls, icon) => arr.length
    ? `<ul>${arr.map((s) => `<li class="${cls}">${icon} ${esc(s)}</li>`).join("")}</ul>` : "";

  // Courses that are legal to take but satisfy no requirement are the most
  // actionable thing here -- surface them, don't bury them.
  const unassigned = (r.counts_but_unassigned || []).length
    ? `<div class="rep"><h3 class="bad">Counts toward nothing</h3>
        <ul>${r.counts_but_unassigned.map((c) =>
          `<li class="bad"><b>${esc(c.code)}</b> ${esc(c.title || "")}<br>
             <span class="hint">${esc(c.reason)}</span></li>`).join("")}</ul></div>`
    : "";
  const cores = (r.cores || []).length
    ? `<p class="hint">Cores this term (not electives): ${r.cores.map(esc).join(", ")}</p>` : "";

  box.innerHTML = `
    <div class="rep">
      <h3>Requirement check
        <span class="hint" style="font-weight:400">
          — ${r.counted_total} of ${r.electives_required} electives counted</span></h3>
      ${list(r.issues, "bad", "✗")}
      ${list(r.unverifiable, "unk", "?")}
      ${list(r.satisfied, "good", "✓")}
      ${cores}
      <p class="hint">${esc(r.cs50)}</p>
      <p class="hint">Assigned — GSD: ${r.assignment.gsd.map(esc).join(", ") || "none"} ·
         SEAS: ${r.assignment.seas.map(esc).join(", ") || "none"}
         ${r.assignment.gsd.length || r.assignment.seas.length
           ? "(cross-listed courses are placed where they're most needed)" : ""}</p>
    </div>
    ${unassigned}
    <div class="capgrid">
      ${r.caps.map((c) => `<div class="cap ${c.count > c.max ? "over" : ""}">
        <b>${c.count} / ${c.max}</b>${esc(c.name)}</div>`).join("")}
    </div>`;
}

// ------------------------------------------------------------- week grid ---

function renderGrid() {
  const grid = $("#weekGrid");
  const slots = Math.ceil((DAY_END - DAY_START) / SLOT);

  let html = `<div class="wg-head"></div>` +
    GRID_DAYS.map((d) => `<div class="wg-head">${DAYS[d].slice(0, 3)}</div>`).join("");
  html += `<div>` + Array.from({ length: slots }, (_, i) => {
    const m = DAY_START + i * SLOT;
    return `<div class="wg-time">${m % 60 === 0 ? fmt(m) : ""}</div>`;
  }).join("") + `</div>`;
  for (const d of GRID_DAYS) {
    html += `<div class="wg-col" data-day="${d}">` +
      Array.from({ length: slots }, () => `<div class="wg-slot"></div>`).join("") + `</div>`;
  }
  grid.innerHTML = html;

  const place = (dayIdx, start, end, cls, title, sub, url) => {
    const col = grid.querySelector(`.wg-col[data-day="${dayIdx}"]`);
    if (!col) return;
    const el = document.createElement(url ? "a" : "div");
    if (url) {
      el.href = url;
      el.target = "_blank";
      el.rel = "noopener noreferrer";
      el.title = "Open on my.harvard";
    }
    el.className = `ev ${cls}${url ? " ev-link" : ""}`;
    el.style.top = `${(Math.max(start, DAY_START) - DAY_START) * PX_PER_MIN}px`;
    el.style.height = `${Math.max(16,
      (Math.min(end, DAY_END) - Math.max(start, DAY_START)) * PX_PER_MIN)}px`;
    el.innerHTML = `<b>${esc(title)}</b><small>${sub}</small>`;
    col.appendChild(el);
  };
  const draw = (meetings, cls, title, url) => {
    for (const m of meetings) {
      for (let d = 0; d < 7; d++) {
        if (m.day_mask & (1 << d)) {
          place(d, m.start_min, m.end_min, cls, title,
                `${fmt(m.start_min)}–${fmt(m.end_min)}`, url);
        }
      }
    }
  };

  state.locked.forEach((i) => draw(
    [{ day_mask: i.day_mask, start_min: i.start_min, end_min: i.end_min,
       start_date: i.start_date, end_date: i.end_date }],
    "ev-locked", i.title || i.code, courseUrl(i)));
  // Enrolled courses already appear as locked blocks; drawing them again as
  // plan candidates would paint purple over the crimson and misrepresent them.
  state.plan.forEach((c) => {
    if (c.enrolled) return;
    draw(c.meetings, "ev-pinned", `${c.subject} ${c.catalog}`, courseUrl(c));
  });
  if (state.preview && !state.plan.has(state.preview.key)) {
    draw(state.preview.meetings, "ev-preview",
      `${state.preview.subject} ${state.preview.catalog}`, courseUrl(state.preview));
  }
}

// ---------------------------------------------------------- combinations ---

async function findCombos() {
  const box = $("#combos");
  box.innerHTML = `<p class="empty">Searching…</p>`;
  try {
    const d = await post("/api/combinations", {
      ...personal(),
      term: term(), requirement: $("#requirement").value,
      school: $("#school").value, q: $("#q").value.trim(),
      pick: parseInt($("#pick").value, 10) || 0, buffer_min: buffer(),
      include_no_credit: $("#includeNoCredit").checked,
      project_based: $("#fProjectBased").checked,
      technical: $("#fTechnical").checked,
      limit: 40,
    });
    if (!d.count) {
      box.innerHTML = `<p class="empty">No valid combinations from a pool of ${d.pool_size}
        candidate sections. Loosen a filter or reduce the travel buffer.</p>`;
      return;
    }
    box.innerHTML = `<p class="hint">${d.count} combination(s) of ${d.pick} from
      ${d.pool_size} candidates (capped at 40).</p>` +
      d.combinations.map((combo, i) => `
        <div class="combo"><h4>Option ${i + 1}</h4>
          ${combo.map((c) => `<div class="row">
            ${extLink(c, `${esc(c.code)} ${esc(c.section)}`, "r-code")} ${extLink(c, esc(c.title))}
            <span style="color:var(--muted)"> — ${c.meetings.map((m) =>
              m.days.map((x) => x.slice(0, 3)).join(" ") + " " +
              fmt(m.start_min) + "–" + fmt(m.end_min)).join(" · ")}</span>
            ${policyBadges(c.policy)}
          </div>`).join("")}
        </div>`).join("");
  } catch (e) {
    box.innerHTML = `<p class="empty">Combination search failed: ${esc(e.message)}</p>`;
  }
}

// --------------------------------------------------------------- importing ---
//
// Two routes, one parser. The extension fetches /calendar/load/ with the
// student's own cookies and hands back the raw JSON; the paste box takes the
// same JSON copied by hand. Both land in ingestCalendar().

function openImport() {
  $("#impPaste").value = "";
  $("#impError").hidden = true;
  $("#impResult").hidden = true;
  $("#impUrl").textContent = CALENDAR_URL;
  $("#impOneClick").hidden = !state.extension;
  $("#impNoExt").hidden = state.extension;
  $("#importModal").hidden = false;
}

function impError(msg) {
  const el = $("#impError");
  el.textContent = msg;
  el.hidden = false;
}

/** Resolve parsed items against the catalog, store them, and refresh. */
async function ingestCalendar(items, mode) {
  // A calendar can span terms; importing next spring's classes into this fall's
  // plan would invent conflicts that don't exist.
  const wanted = term();
  const forTerm = items.filter((i) => !i.term || i.term === wanted);
  const skipped = items.length - forTerm.length;
  if (!forTerm.length) {
    throw new Error(
      `Found ${items.length} meeting(s), but none for ${wanted}. ` +
      `Switch the term at the top of the page and import again.`);
  }

  const d = await post("/api/schedule/resolve", { term: wanted, items: forTerm });
  const { imported, kept } = Store.replaceImported(wanted, d.items);

  loadLocked();
  await Promise.all([refreshResults(), loadPlan()]);

  const bits = [`Imported ${imported} class${imported === 1 ? "" : "es"} for ${wanted}.`];
  if (d.matched < imported) {
    bits.push(`${imported - d.matched} couldn't be matched to a catalog row ` +
      `(${d.unmatched.slice(0, 3).map(esc).join(", ")}) — they still block time, ` +
      `but won't be checked against the policy.`);
  }
  if (skipped) bits.push(`${skipped} meeting(s) for other terms were skipped.`);
  if (kept) bits.push(`${kept} hand-added block(s) kept.`);
  if (mode === "heuristic") {
    bits.push("Note: the payload didn't match the expected shape, so a fallback " +
      "parser was used. Double-check the times.");
  }

  const el = $("#impResult");
  el.innerHTML = bits.map((b) => `<div>${b}</div>`).join("");
  el.hidden = false;
  renderOnboarding();
  return { imported };
}

async function importFromPaste() {
  $("#impError").hidden = true;
  try {
    const { items, mode } = parsePasted($("#impPaste").value);
    await ingestCalendar(items, mode);
  } catch (e) {
    impError(e.message);
  }
}

function importFromExtension() {
  $("#impError").hidden = true;
  $("#impOneClick").disabled = true;
  $("#impOneClick").textContent = "Fetching…";

  const done = () => {
    $("#impOneClick").disabled = false;
    $("#impOneClick").textContent = "Import with the extension";
  };

  const timer = setTimeout(() => {
    window.removeEventListener("message", onMessage);
    done();
    impError("The extension didn't respond. Reload the page, or use the " +
             "copy-paste method below.");
  }, 20000);

  async function onMessage(ev) {
    if (ev.source !== window) return;
    const msg = ev.data;
    if (!msg || msg.source !== "mde-extension") return;
    if (msg.type !== "MDE_CALENDAR_RESULT" && msg.type !== "MDE_CALENDAR_ERROR") return;

    clearTimeout(timer);
    window.removeEventListener("message", onMessage);
    done();

    if (msg.type === "MDE_CALENDAR_ERROR") return impError(msg.error);
    try {
      const { items, mode } = parseCalendar(msg.payload);
      if (!items.length) {
        return impError("Connected to my.harvard, but no meetings were " +
          "recognized. Its calendar format may have changed.");
      }
      await ingestCalendar(items, mode);
    } catch (e) {
      impError(e.message);
    }
  }

  window.addEventListener("message", onMessage);
  window.postMessage({ source: "mde-page", type: "MDE_FETCH_CALENDAR" }, location.origin);
}

// The content script announces itself on load. Until then the modal offers only
// the paste route, which needs no install at all.
window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  if (ev.data?.source === "mde-extension" && ev.data.type === "MDE_EXT_READY") {
    state.extension = true;
    $("#impOneClick").hidden = false;
    $("#impNoExt").hidden = true;
  }
});

// ------------------------------------------------------------ backup file ---

function exportData() {
  const blob = new Blob([Store.export()], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `mde-planner-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast("Saved a backup file. Load it in another browser to move your plan.");
}

function importData(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = Store.import(String(reader.result));
      toast(`Loaded ${r.locked} locked block(s) and ${r.plan} plan course(s) ` +
            `across ${r.terms.length} term(s).`);
      $("#buffer").value = String(Store.getSetting("buffer_min", 15));
      await refreshAll();
    } catch (e) {
      toast(`Couldn't load that file: ${e.message}`, "warn");
    }
  };
  reader.readAsText(file);
}

// ----------------------------------------------------------- block editor ---

const blockState = { kind: "obligation", courseKey: "", days: new Set() };
const DAY_SHORT = ["Su", "M", "T", "W", "Th", "F", "S"];

const hhmmToMin = (v) => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v || "");
  return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : null;
};

function openBlock() {
  blockState.kind = "obligation";
  blockState.courseKey = "";
  blockState.days = new Set();
  $("#bTitle").value = "";
  $("#bCourseQ").value = "";
  $("#bCourseResults").innerHTML = "";
  $("#bCoursePicked").textContent = "";
  $("#bFrom").value = ""; $("#bTo").value = "";
  $("#bError").hidden = true;
  $$("#bKind .segbtn").forEach((x) => x.classList.toggle("active", x.dataset.kind === "obligation"));
  $("#bCourseWrap").hidden = true;
  $("#bDays").innerHTML = DAY_SHORT.map((d, i) =>
    `<label><input type="checkbox" value="${i}">${d}</label>`).join("");
  $$("#bDays input").forEach((i) => i.addEventListener("change", () => {
    const v = parseInt(i.value, 10);
    i.checked ? blockState.days.add(v) : blockState.days.delete(v);
  }));
  $("#blockModal").hidden = false;
}

async function searchUntimed() {
  const q = $("#bCourseQ").value.trim();
  const d = await get("/api/courses/untimed", { term: term(), q, limit: 20 });
  $("#bCourseResults").innerHTML = d.results.length
    ? d.results.map((c) =>
        `<li data-key="${esc(c.key)}" data-code="${esc(c.code)}" data-title="${esc(c.title)}">
          <span class="c">${esc(c.code)}</span> ${esc(c.title)}</li>`).join("")
    : `<li style="color:var(--muted);cursor:default">No untimed listings match.</li>`;
  $$("#bCourseResults li[data-key]").forEach((li) =>
    li.addEventListener("click", () => {
      blockState.courseKey = li.dataset.key;
      $("#bTitle").value = `${li.dataset.code} ${li.dataset.title}`;
      $("#bCoursePicked").textContent = `Linked to ${li.dataset.code} — it will count toward the outside-Harvard cap.`;
      $("#bCourseResults").innerHTML = "";
    }));
}

async function saveBlock() {
  const start = hhmmToMin($("#bStart").value);
  const end = hhmmToMin($("#bEnd").value);
  const err = (m) => { $("#bError").textContent = m; $("#bError").hidden = false; };
  $("#bError").hidden = true;

  if (!$("#bTitle").value.trim()) return err("Give the block a title.");
  if (!blockState.days.size) return err("Pick at least one day.");
  if (start == null || end == null) return err("Enter a start and end time.");
  if (end <= start) return err("End time must be after the start time.");
  const from = $("#bFrom").value, to = $("#bTo").value;
  if (from && to && to < from) return err("Last date is before the first date.");

  try {
    // The server shapes and validates the record (and verifies the optional
    // NONH course link) so both entry points store identical objects.
    const d = await post("/api/schedule/block", {
      term: term(), title: $("#bTitle").value.trim(),
      days: Array.from(blockState.days), start_min: start, end_min: end,
      start_date: from || null, end_date: to || null,
      category: blockState.kind, course_key: blockState.courseKey,
    });
    Store.addLocked(term(), d.item);
  } catch (e) {
    return err(e.message);
  }

  $("#blockModal").hidden = true;
  renderOnboarding();
  loadLocked();
  await Promise.all([loadPlan(), refreshResults()]);
}

// ------------------------------------------------------------------ wire ---

let t;
const debounced = () => { clearTimeout(t); t = setTimeout(search, 220); };
$("#q").addEventListener("input", debounced);
["#school", "#requirement", "#freeOnly", "#includeNoCredit", "#fProjectBased",
 "#fTechnical", "#includeTba"]
  .forEach((s) => $(s).addEventListener("change", () => search()));
$("#buffer").addEventListener("change", async () => {
  Store.setSetting("buffer_min", buffer());
  await Promise.all([search(), loadPlan()]);
});
$("#term").addEventListener("change", async () => {
  Store.setSetting("term", term());
  await refreshAll();
});
$("#loadMore").addEventListener("click", () => { state.offset += state.limit; search(true); });
$("#findCombos").addEventListener("click", findCombos);
$("#editProfile").addEventListener("click", openProfile);
$("#cancelProfile").addEventListener("click", () => { $("#profileModal").hidden = true; });
$("#saveProfile").addEventListener("click", saveProfile);
$("#addBlockBtn").addEventListener("click", openBlock);
$("#cancelBlock").addEventListener("click", () => { $("#blockModal").hidden = true; });
$("#saveBlock").addEventListener("click", saveBlock);

// Hover to peek, click to pin open. Pinning matters because the panel holds
// text someone may want to select or read slowly.
let metaPinned = false;
$("#metaInfo").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleMeta();
  metaPinned = !$("#metaDetails").hidden;
});
$("#metaInfo").addEventListener("mouseenter", () => toggleMeta(true));
$(".metawrap").addEventListener("mouseleave", () => { if (!metaPinned) toggleMeta(false); });
document.addEventListener("click", () => { metaPinned = false; toggleMeta(false); });

$("#seasonFix").addEventListener("click", applySeasonFix);

$("#welcomeSkip").addEventListener("click", dismissWelcome);
$("#welcomeImport").addEventListener("click", () => { dismissWelcome(); openImport(); });
$("#welcomeProfile").addEventListener("click", () => { dismissWelcome(); openProfile(); });

$("#importBtn").addEventListener("click", openImport);
$("#firstRunImport").addEventListener("click", openImport);
$("#firstRunProfile").addEventListener("click", openProfile);
$("#closeImport").addEventListener("click", () => { $("#importModal").hidden = true; });
$("#impOneClick").addEventListener("click", importFromExtension);
$("#impPasteGo").addEventListener("click", importFromPaste);
$("#impClear").addEventListener("click", async () => {
  const n = Store.clearImported(term());
  loadLocked();
  await Promise.all([refreshResults(), loadPlan()]);
  toast(`Removed ${n} imported class${n === 1 ? "" : "es"}.`);
});

$("#exportBtn").addEventListener("click", exportData);
$("#importFile").addEventListener("change", (e) => {
  if (e.target.files?.[0]) importData(e.target.files[0]);
  e.target.value = "";
});
$("#resetBtn").addEventListener("click", async () => {
  if (!confirm("Delete your profile, locked schedule and plan from this browser? " +
               "This cannot be undone — export a backup first if you want one.")) return;
  Store.clear();
  await refreshAll();
  toast("Cleared everything stored in this browser.");
});

$$(".modal").forEach((m) => m.addEventListener("click", (e) => {
  if (e.target !== m) return;               // click the backdrop to dismiss
  if (m.id === "welcomeModal") dismissWelcome();
  else m.hidden = true;
}));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#welcomeModal").hidden) dismissWelcome();
  $$(".modal").forEach((m) => { m.hidden = true; });
  metaPinned = false;
  toggleMeta(false);
});

$$("#bKind .segbtn").forEach((b) => b.addEventListener("click", () => {
  blockState.kind = b.dataset.kind;
  blockState.courseKey = "";
  $$("#bKind .segbtn").forEach((x) => x.classList.toggle("active", x === b));
  $("#bCourseWrap").hidden = blockState.kind !== "course";
  $("#bCoursePicked").textContent = "";
  if (blockState.kind === "course") searchUntimed();
}));
let bt;
$("#bCourseQ").addEventListener("input", () => {
  clearTimeout(bt); bt = setTimeout(searchUntimed, 220);
});
$("#pSeasBg").addEventListener("change", () => {
  $("#pAreasWrap").hidden = !$("#pSeasBg").checked;
});

$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".tabpanel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#tab-${t.dataset.tab}`).classList.add("active");
  if (t.dataset.tab === "grid") renderGrid();
}));

// "4 minutes ago" goes stale on a tab left open all afternoon.
setInterval(() => { if (state.meta) renderMeta(); }, 60_000);

boot().catch((e) => { $("#metaAgo").textContent = `Failed to load: ${e.message}`; });
