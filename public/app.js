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
// Weekdays always show; Sat/Sun appear only when something is scheduled there.
// The window is 8am-10pm unless a block falls outside it. This is what makes it
// safe to delete the sidebar list: with fixed Mon-Fri 8-22 bounds, a Saturday
// club meeting or a 7am commute block would be invisible AND unremovable.
const BASE_DAYS = [1, 2, 3, 4, 5];
const BASE_START = 8 * 60, BASE_END = 22 * 60;
const SLOT = 30;
const NEW_BLOCK_MIN = 90;       // default length for a click-to-create block
// A half-hour row never shrinks below this or the blocks stop being readable;
// never grows past this or a short day wastes the pane.
const SLOT_H_MIN = 13, SLOT_H_MAX = 30;

let gridDays = [...BASE_DAYS];
let dayStart = BASE_START, dayEnd = BASE_END;
let pxPerMin = 1;
let lastSlotH = 0;   // last good row height, reused if a measurement fails

const CALENDAR_URL = "https://my.harvard.edu/calendar/load/";

const state = {
  meta: null, policy: null,
  results: [], locked: [], plan: new Map(), hiddenTba: 0,
  preview: [], offset: 0, limit: 100, total: 0,
  electivesThisTerm: 2,
  extension: false,        // set true when the extension announces itself
  slots: [],               // per-slot combination filters
  comboOffset: 0, comboLimit: 20, comboTotal: 0, comboRan: false,
  comboPage: [],           // the combinations currently on screen
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

// --------------------------------------------------------------- loading ---

/** Placeholder rows shown while a list is populated for the first time. */
function skeletonCards(n, lines = 3) {
  return Array.from({ length: n }, () =>
    `<li class="skelcard">${Array.from({ length: lines }, (_, i) =>
      `<div class="skel skel-line" style="width:${[70, 45, 30][i] ?? 40}%"></div>`
    ).join("")}</li>`).join("");
}

/** Run an async action with the button showing a spinner in place of its label.
 *
 * The label goes transparent rather than being swapped out, so the button keeps
 * its width and the row does not reflow while the request is in flight.
 */
async function withBusy(btn, fn) {
  if (btn) { btn.classList.add("btn-busy"); btn.disabled = true; }
  try {
    return await fn();
  } finally {
    // The list is usually re-rendered underneath us, replacing the button; only
    // restore one that is still in the document.
    if (btn && btn.isConnected) { btn.classList.remove("btn-busy"); btn.disabled = false; }
  }
}

/** Dim a list that is being refreshed in place, so stale rows aren't clickable. */
function setRefreshing(sel, on) {
  $(sel)?.classList.toggle("is-refreshing", on);
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

  $("#requirement").innerHTML = requirementOptions();

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
  $("#results").innerHTML = skeletonCards(6);
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

/** Options for a "requirement it satisfies" select.
 *
 * Only rules 1 and 2 are requirements. The others are `kind: maximum` -- caps
 * on how many of a category count toward the nine. A course that only hits a
 * cap satisfies nothing, so listing "FAS courses" or "Other Harvard schools"
 * as requirement filters said something untrue about them. Driven off `kind`
 * so a new rule in mde_policy.yaml lands in the right group by itself.
 */
function requirementOptions(selected = "") {
  const mins = state.policy.requirements.filter((r) => r.kind === "minimum");
  const sel = (v) => (v === selected ? " selected" : "");
  return `<option value=""${sel("")}>All courses</option>` +
    `<option value="minimums"${sel("minimums")}>Any requirement (${
      mins.map((r) => esc(r.short || r.name)).join(" or ")})</option>` +
    mins.map((r) => `<option value="${r.id}"${sel(r.id)}>${esc(r.name)}</option>`).join("");
}

function electivesThisTerm() {
  const p = Store.getProfile();
  const map = state.policy?.electives_by_term || {};
  return map[`${p?.year ?? 1}-${p?.season ?? "Fall"}`] ?? 2;
}

const FILTER_TABS = new Set(["list"]);

function renderFilterBarVisibility() {
  const active = document.querySelector(".tab.active")?.dataset.tab;
  $("#filterBar").hidden = !FILTER_TABS.has(active);
}

function renderProfileChip() {
  const p = Store.getProfile();
  $("#fbProfileText").textContent = p
    ? `Year ${p.year} ${p.season}` +
      (p.seas_background ? ` · SEAS${p.seas_areas.length ? " " + p.seas_areas.join("/") : ""}` : "") +
      (p.physical_design_background ? " · design" : "")
    : "background not set";
  $("#fbProfile").classList.toggle("unset", !p);
}

function renderProfile() {
  const p = Store.getProfile();
  const box = $("#profileSummary");
  state.electivesThisTerm = electivesThisTerm();
  renderProfileChip();

  const n = state.electivesThisTerm;

  if (!p) {
    box.innerHTML = `<div class="bgempty">Not set — results assume <b>Year 1 Fall</b>
      with no SEAS or design background. Eligibility is a guess until you set it.</div>`;
  } else {
    const fact = (label, value, flag = false) =>
      `<div class="bgfact${flag ? " flag" : ""}"><span class="bgl">${esc(label)}</span>
        <b>${value}</b></div>`;
    box.innerHTML = [
      fact("Program term", `Year ${p.year} · ${esc(p.season)}`),
      fact("Electives this term", String(n)),
      fact("SEAS background", p.seas_background
        ? `Yes${p.seas_areas.length ? ` · ${p.seas_areas.map(esc).join(", ")}` : ""}`
        : "No"),
      fact("Physical design", p.physical_design_background ? "Yes" : "No"),
      fact("CS50", esc(p.cs50_status.replace(/_/g, " ")),
           p.cs50_status === "required"),
      fact("Completed electives", p.completed_codes.length
        ? String(p.completed_codes.length) : "None"),
    ].join("");
  }

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
  renderGrid();
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
    project_based: $("#fProjectBased").checked,
    technical: $("#fTechnical").checked,
    include_tba: $("#includeTba").checked, buffer_min: buffer(),
    ...extra,
  };
}

async function search(append = false) {
  if (!append) state.offset = 0;
  // Nothing on screen yet -> skeletons. Something on screen -> dim it, because
  // replacing readable results with skeletons on every filter tweak flickers.
  if (!append && !state.results.length) $("#results").innerHTML = skeletonCards(6);
  else setRefreshing("#results", true);
  $("#resultCount").innerHTML = `${$("#resultCount").textContent} <span class="spin"></span>`;
  let d;
  try {
    d = await post("/api/search",
      searchBody({ limit: state.limit, offset: state.offset }));
  } finally {
    setRefreshing("#results", false);
  }
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
  setRefreshing("#results", true);
  let d;
  try {
    d = await post("/api/search", searchBody({ limit: shown, offset: 0 }));
  } finally {
    setRefreshing("#results", false);
  }
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
  const reqs = Object.fromEntries(state.policy.requirements.map((r) => [r.id, r]));
  return pol.satisfies.filter((v) => v.verdict === "yes" || v.verdict === "verify")
    .map((v) => {
      const r = reqs[v.requirement_id] || {};
      // Only rules 1 (GSD) and 2 (SEAS) are minimums you have to satisfy. Rules
      // 4-7 are ceilings: the course counts toward the 9 and eats cap headroom,
      // but it satisfies nothing, so it must not wear the same green as a real
      // requirement. Driven off `kind` rather than an id list so a new rule in
      // mde_policy.yaml is styled correctly without touching this.
      const isCap = r.kind === "maximum";
      const cls = isCap ? "capped" : (v.verdict === "yes" ? "req" : "reqmaybe");
      const label = esc(r.short || r.name || v.requirement_id)
        + (isCap && r.max_courses ? ` \u00b7 max ${r.max_courses}` : "")
        + (v.verdict === "verify" ? " ?" : "");
      // A pill must fit on one line, so it shows the short form; the full
        // requirement name joins the reason in the tooltip.
        const tip = `${r.name || v.requirement_id}\n${v.reason}`;
        return `<span class="badge ${cls}" title="${esc(tip)}">${label}</span>`;
    }).join("");
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
  // No badge when it fits: "clashes" and "overlaps" are the exceptions, so
  // their absence already says the course is clear. A "fits" badge on almost
  // every row was noise that made the real warnings harder to spot.
  const fit = !c.meetings.length
    ? `<span class="badge tba">TBA</span>`
    : clashes.length
      ? `<span class="badge clash">clashes: ${esc(clashes.join(", "))}</span>`
      : planClashes.length
        ? `<span class="badge planclash" title="Overlaps a course in your plan — still selectable">overlaps: ${esc(planClashes.join(", "))}</span>`
        : "";
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
      <div class="r-head">${extLink(c, `${esc(c.subject)} ${esc(c.catalog)}`, "r-code")}
        ${extLink(c, esc(c.title), "r-title")}</div>
      <div class="r-meta">
        ${esc(times)} · ${esc(c.school || "—")} · ${esc(c.session || "")}
        ${c.instructors.length ? " · " + esc(c.instructors.join(", ")) : ""}
      </div>
      <div class="r-foot">
        <div class="r-badges">
          ${fit}
          <span class="badge level" title="${esc(pol.level_label || "")}">${
            esc(pol.level_short || pol.level_label || "")}</span>
          ${policyBadges(pol)}${lists}${extra}
        </div>
        <div class="r-actions">
          ${c.enrolled ? `<span class="badge req">enrolled</span>`
            : `<button class="pin ghost" data-plan="${esc(c.key)}">${
                inPlan ? "Remove" : "Add to plan"}</button>`}
        </div>
      </div>
      ${warns}
    </li>`;
}

function renderResults() {
  // Also clears the in-flight spinner appended by search().
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
      common before a term's schedule is finalized.
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
    li.addEventListener("mouseenter", () => { setPreview([c]); });
    li.addEventListener("mouseleave", () => { setPreview([]); });
  });
  $$(`${sel} [data-plan]`).forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    await togglePlan(b.dataset.plan, b);
  }));
}

// ------------------------------------------------------------------ plan ---

/** The course object for a key, from whatever is already on screen. */
function knownCourse(key) {
  return state.plan.get(key)
    || state.results.find((c) => c.key === key)
    || state.comboPage.flat().find((c) => c.key === key)
    || null;
}

async function togglePlan(key, btn = null) {
  const t = term();
  const added = Store.togglePlan(t, key);

  // Draw the change immediately. The clicked row already carries the course's
  // meetings, so the calendar does not have to wait on two network round trips
  // to show something the browser already knows. The fetches below then
  // reconcile with the server's view (policy verdicts, conflict labels).
  const course = knownCourse(key);
  if (added && course) state.plan.set(key, { ...course, in_plan: 1, enrolled: false });
  else if (!added) state.plan.delete(key);
  $("#planCount").textContent = String(Store.plan(t).length);
  renderGrid();

  await withBusy(btn, async () => {
    // Independent requests: renderResults reads plan membership from Store,
    // not from the /api/plan response, so these never needed to be sequential.
    await Promise.all([loadPlan(), refreshResults()]);
  });
  // A combination's button reflects plan membership, so adding one of its
  // courses from elsewhere has to update it.
  if (state.comboPage.length) renderCombos();
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
    $("#planReport").innerHTML = `<div class="notofficial">
      <b>This is a self-check, not an approval.</b> The MDE program office
      reviews and approves every elective selection.</div>
      <p class="hint">Add courses to your plan to see the full policy check.</p>`;
    renderGrid();
    return;
  }
  if (!state.plan.size) {
    $("#planList").innerHTML = skeletonCards(2);
    $("#planReport").innerHTML = `<div class="skelcard">
      <div class="skel skel-line" style="width:40%"></div>
      <div class="skel skel-line" style="width:80%"></div>
      <div class="skel skel-line" style="width:65%"></div></div>`;
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

  const needsReview = (r.unverifiable || []).length;
  box.innerHTML = `
    <div class="notofficial">
      <b>This is a self-check, not an approval.</b> The MDE program office
      reviews and approves every elective selection. This tool reads the written
      policy and can be wrong — cross-listings and SEAS-faculty judgements in
      particular are inferred, not verified. Confirm anything that matters with
      your program manager before you enroll.
      ${needsReview ? `<span class="no-flag">${needsReview} item${
        needsReview === 1 ? "" : "s"} below already need their confirmation.</span>` : ""}
    </div>
    <div class="rep">
      <h3>Policy self-check
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

// ---------------------------------------------------------- schedule pane ---
//
// One DOM, two presentations: a sticky companion column when there is room,
// and a slide-out drawer when there isn't. The drawer is the only mode that
// needs state, since the wide layout is always visible.

// Read from CSS so the breakpoint lives in one place. Media queries cannot use
// custom properties, so styles.css still repeats the number in its @media --
// but at least JS can never disagree with it.
const DRAWER_MAX = parseInt(
  getComputedStyle(document.documentElement).getPropertyValue("--drawer-max"), 10) || 1380;

const drawerQuery = window.matchMedia(`(max-width: ${DRAWER_MAX}px)`);
const drawerMode = () => drawerQuery.matches;

function setDrawer(open) {
  if (!drawerMode()) return;
  $("#schedulePane").classList.toggle("open", open);
  $("#scheduleScrim").hidden = !open;
  $("#scheduleToggle").setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("drawer-open", open);
  // The grid measures itself for click-to-create, and getBoundingClientRect
  // returns zeroes while the pane is off-screen -- so redraw once it is up.
  if (open) renderGrid();
}

/** Show these courses as blue preview blocks. Empty clears the preview. */
function setPreview(courses) {
  state.preview = (courses || []).filter(Boolean);
  renderGrid();
}

function renderScheduleCount() {
  const n = state.locked.length +
            [...state.plan.values()].filter((c) => !c.enrolled).length;
  $("#scheduleCount").textContent = String(n);
}

// ------------------------------------------------------------- week grid ---

/** Everything drawn on the grid, with what removing it should do. */
function gridItems() {
  const items = [];
  for (const i of state.locked) {
    items.push({
      kind: "locked", label: i.title || i.code, url: courseUrl(i),
      removeAttr: `data-rm-locked="${esc(i.id)}"`,
      meetings: [{ day_mask: i.day_mask, start_min: i.start_min, end_min: i.end_min }],
      detail: i.start_date && i.source === "manual"
        ? `${i.start_date} → ${i.end_date || ""}` : "",
    });
  }
  state.plan.forEach((c) => {
    // Enrolled courses already appear as locked blocks; drawing them again as
    // plan candidates would paint purple over the crimson and misrepresent them.
    if (c.enrolled) return;
    items.push({
      kind: "pinned", label: `${c.subject} ${c.catalog}`, url: courseUrl(c),
      removeAttr: `data-rm-plan="${esc(c.key)}"`, meetings: c.meetings, detail: "",
    });
  });
  return items;
}

function computeGridBounds(items) {
  const days = new Set(BASE_DAYS);
  let lo = BASE_START, hi = BASE_END;
  for (const it of items) {
    for (const m of it.meetings) {
      for (let d = 0; d < 7; d++) if (m.day_mask & (1 << d)) days.add(d);
      if (m.start_min < lo) lo = Math.floor(m.start_min / 60) * 60;
      if (m.end_min > hi) hi = Math.ceil(m.end_min / 60) * 60;
    }
  }
  gridDays = [...days].sort((a, b) => a - b);
  dayStart = Math.max(0, lo);
  dayEnd = Math.min(24 * 60, hi);
}

/** Pick a row height so the whole pane fits the viewport without scrolling.
 *
 * At a fixed 1px per minute a normal day is 840px plus headers -- taller than
 * most viewports, so the calendar had to be scrolled separately from the page.
 *
 * Three things make this fiddlier than it looks:
 *
 * - **Units.** :root carries `zoom`, so getBoundingClientRect() and
 *   innerHeight are in *visual* pixels, while offsetTop/offsetHeight and the
 *   value written to --wg-slot-h are *CSS* pixels that then get multiplied by
 *   the zoom. Everything below is CSS pixels, with innerHeight converted once.
 * - **Where the pane starts.** In the two-pane layout the pane begins below the
 *   topbar, not at the top of the viewport. Ignoring that offset oversized the
 *   grid by exactly that much and pushed the pane ~80px below the fold.
 *   offsetTop is used rather than getBoundingClientRect() because it reports
 *   the static position, unaffected by `position: sticky` having engaged.
 * - **No magic constants.** The space taken by the heading, legend and
 *   collapsed summary is measured as pane-minus-grid, which is independent of
 *   the row height being solved for.
 */
function fitScale(slots) {
  const pane = $("#schedulePane");
  const grid = $("#weekGrid");
  const zoom = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  const viewportCss = window.innerHeight / zoom;

  // Space the pane spends on everything that isn't the calendar, measured
  // relative to the pane's own box. NOT pane.offsetHeight - grid.offsetHeight:
  // as a drawer the pane is height:100%, so that difference is leftover space
  // rather than chrome, and the calendar would be sized from its own slack.
  const wrap = $(".grid-wrap");
  const kids = [...pane.children];
  const last = kids[kids.length - 1];
  const padBottom = parseFloat(getComputedStyle(pane).paddingBottom) || 0;
  const above = wrap.offsetTop;                     // includes padding-top
  const below = Math.max(0, (last.offsetTop + last.offsetHeight)
                            - (wrap.offsetTop + wrap.offsetHeight));
  const chromeCss = above + below + padBottom;
  const headerCss = $("#weekGrid .wg-head")?.offsetHeight || 29;

  // As a drawer the pane is fixed to the full viewport height, so it starts at
  // zero; as a column it starts where the layout puts it.
  //
  // Measured from `main`, NOT from the pane: Chrome's offsetTop on a sticky
  // element tracks its *stuck* position, so reading it from the pane returned
  // 91 at the top of the page and 1012 once scrolled. Every re-render while
  // scrolled -- hovering a course row triggers one -- then computed a negative
  // budget and resized the calendar. `main` is not positioned, so its offset is
  // the static layout position and does not move with scroll.
  //
  // This deliberately sizes for the un-stuck case, leaving room for the header.
  // Once stuck the pane has ~80px more to play with, but using it would mean
  // the calendar changed height as you scrolled, which is worse than being
  // slightly shorter than it could be.
  let topCss = 0;
  if (!drawerMode()) {
    const main = pane.parentElement;
    topCss = parseFloat(getComputedStyle(main).paddingTop) || 0;
    for (let el = main; el; el = el.offsetParent) topCss += el.offsetTop;
  }

  const padCss = 14;      // breathing room below the pane
  const budgetCss = viewportCss - topCss - padCss - chromeCss - headerCss;
  // A nonsensical measurement (mid-transition, pane not laid out yet) must not
  // produce a *different* height than the normal path -- that is what made the
  // jump visible. Reuse the last good value instead.
  if (!Number.isFinite(budgetCss) || budgetCss < 80) return lastSlotH || 22;
  lastSlotH = Math.max(SLOT_H_MIN, Math.min(SLOT_H_MAX, Math.floor(budgetCss / slots)));
  return lastSlotH;
}

function renderGrid() {
  const grid = $("#weekGrid");
  const items = gridItems();
  computeGridBounds(items);
  const slots = Math.ceil((dayEnd - dayStart) / SLOT);
  const slotH = fitScale(slots);
  pxPerMin = slotH / SLOT;
  grid.style.setProperty("--wg-slot-h", `${slotH}px`);

  grid.style.setProperty("--wg-cols", String(gridDays.length));
  let html = `<div class="wg-head"></div>` +
    gridDays.map((d) => `<div class="wg-head">${DAYS[d].slice(0, 3)}</div>`).join("");
  html += `<div>` + Array.from({ length: slots }, (_, i) => {
    const m = dayStart + i * SLOT;
    return `<div class="wg-time">${m % 60 === 0 ? fmt(m) : ""}</div>`;
  }).join("") + `</div>`;
  for (const d of gridDays) {
    html += `<div class="wg-col" data-day="${d}">` +
      Array.from({ length: slots }, () => `<div class="wg-slot"></div>`).join("") +
      `<div class="wg-ghost" hidden></div></div>`;
  }
  grid.innerHTML = html;

  const place = (dayIdx, start, end, cls, title, sub, url, removeAttr, detail) => {
    const col = grid.querySelector(`.wg-col[data-day="${dayIdx}"]`);
    if (!col) return;
    const el = document.createElement("div");
    el.className = `ev ${cls}`;
    el.style.top = `${(Math.max(start, dayStart) - dayStart) * pxPerMin}px`;
    el.style.height = `${Math.max(14,
      (Math.min(end, dayEnd) - Math.max(start, dayStart)) * pxPerMin)}px`;
    // In a ~70px-wide side-pane column the title wraps or clips, so the full
    // text has to be recoverable on hover.
    const full = `${title} · ${sub.replace(/<[^>]*>/g, " ")}${detail ? ` · ${detail}` : ""}`;
    const inner = url
      ? `<a class="ev-body ev-link" href="${esc(url)}" target="_blank"
            rel="noopener noreferrer" title="${esc(full)} — open on my.harvard">`
      : `<span class="ev-body" title="${esc(full)}">`;
    el.innerHTML = inner +
      `<b>${esc(title)}</b><small>${sub}${detail ? `<br>${esc(detail)}` : ""}</small>` +
      (url ? `</a>` : `</span>`) +
      (removeAttr ? `<button class="ev-x" ${removeAttr}
          title="Remove" aria-label="Remove ${esc(title)}">×</button>` : "");
    col.appendChild(el);
  };

  const draw = (it, cls) => {
    for (const m of it.meetings) {
      for (let d = 0; d < 7; d++) {
        if (m.day_mask & (1 << d)) {
          place(d, m.start_min, m.end_min, cls, it.label,
                `${fmt(m.start_min)}–${fmt(m.end_min)}`, it.url, it.removeAttr, it.detail);
        }
      }
    }
  };

  for (const it of items) draw(it, it.kind === "locked" ? "ev-locked" : "ev-pinned");
  // Anything already in the plan is drawn purple above; previewing it again in
  // blue would paint over it and misrepresent it.
  for (const pv of state.preview) {
    if (!pv || state.plan.has(pv.key)) continue;
    draw({ label: `${pv.subject} ${pv.catalog}`, url: courseUrl(pv),
           meetings: pv.meetings, removeAttr: "", detail: "" }, "ev-preview");
  }

  wireGridRemoval();
  wireGridCreate();
  renderLockedOverflow(items);
  renderScheduleCount();
}

function wireGridRemoval() {
  $$("#weekGrid [data-rm-locked]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      Store.removeLocked(term(), b.dataset.rmLocked);
      loadLocked();
      await Promise.all([refreshResults(), loadPlan()]);
    }));
  $$("#weekGrid [data-rm-plan]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      await togglePlan(b.dataset.rmPlan);
    }));
}

/** Click an empty slot to create a block there; hover shows where it would go. */
function wireGridCreate() {
  const minutesAt = (col, clientY) => {
    const r = col.getBoundingClientRect();
    const raw = dayStart + ((clientY - r.top) / (r.height / (dayEnd - dayStart)));
    return Math.max(dayStart, Math.min(dayEnd - SLOT,
      Math.round(raw / SLOT) * SLOT));      // snap to the half hour
  };

  $$("#weekGrid .wg-col").forEach((col) => {
    const ghost = col.querySelector(".wg-ghost");
    col.addEventListener("mousemove", (e) => {
      if (e.target.closest(".ev")) { ghost.hidden = true; return; }
      const start = minutesAt(col, e.clientY);
      ghost.hidden = false;
      ghost.style.top = `${(start - dayStart) * pxPerMin}px`;
      ghost.style.height = `${NEW_BLOCK_MIN * pxPerMin}px`;
      ghost.textContent = `+ ${fmt(start)}`;
    });
    col.addEventListener("mouseleave", () => { ghost.hidden = true; });
    col.addEventListener("click", (e) => {
      if (e.target.closest(".ev")) return;   // clicking a course is not "add here"
      const start = minutesAt(col, e.clientY);
      openBlock({ day: Number(col.dataset.day), start,
                  end: Math.min(dayEnd, start + NEW_BLOCK_MIN) });
    });
  });
}

/** Anything the grid cannot represent still needs to be visible and removable. */
function renderLockedOverflow(items) {
  const box = $("#lockedOverflow");
  const orphans = items.filter((it) => it.meetings.every((m) => !m.day_mask));
  if (!orphans.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `<b>Not shown on the grid</b> (no meeting days recorded):` +
    `<ul>${orphans.map((o) => `<li>${esc(o.label)}` +
      (o.removeAttr ? ` <button class="ghost tiny" ${o.removeAttr}>Remove</button>` : "") +
      `</li>`).join("")}</ul>`;
  wireGridRemoval();
}

// ---------------------------------------------------------- combinations ---

const SLOT_DEFAULT = () => ({
  q: "", school: "", requirement: "",
  project_based: false, technical: false,
});

function slotCount() {
  return parseInt($("#pick").value, 10) || state.electivesThisTerm || 2;
}

/** Grow or shrink the slot list, preserving filters already set. */
function syncSlots() {
  const n = slotCount();
  const saved = Store.getSetting("combo_slots", null);
  if (!state.slots.length && Array.isArray(saved) && saved.length) {
    state.slots = saved.map((x) => ({ ...SLOT_DEFAULT(), ...x }));
  }
  while (state.slots.length < n) state.slots.push(SLOT_DEFAULT());
  state.slots.length = n;
  Store.setSetting("combo_slots", state.slots);
}

function renderSlotCards(poolSizes = null) {
  syncSlots();
  const reqOptions = (sel) => requirementOptions(sel);
  const schoolOptions = (sel) => `<option value="">All schools</option>` +
    state.meta.schools.map((x) =>
      `<option ${x === sel ? "selected" : ""}>${esc(x)}</option>`).join("");

  $("#slotCards").innerHTML = state.slots.map((sl, i) => `
    <div class="slotcard">
      <div class="sc-head">
        <h4>Elective ${i + 1}</h4>
        ${poolSizes && poolSizes[i] != null
          ? `<span class="sc-pool">${poolSizes[i].toLocaleString()} candidate${poolSizes[i] === 1 ? "" : "s"}</span>`
          : ""}
      </div>
      <label class="fb-field">Keyword
        <input data-slot="${i}" data-f="q" value="${esc(sl.q)}"
               placeholder="title, code, instructor…"></label>
      <label class="fb-field">Requirement
        <select data-slot="${i}" data-f="requirement">${reqOptions(sl.requirement)}</select></label>
      <label class="fb-field">School
        <select data-slot="${i}" data-f="school">${schoolOptions(sl.school)}</select></label>
      <label class="check"><input type="checkbox" data-slot="${i}" data-f="project_based"
        ${sl.project_based ? "checked" : ""}> Project-based <span class="rule">1a</span></label>
      <label class="check"><input type="checkbox" data-slot="${i}" data-f="technical"
        ${sl.technical ? "checked" : ""}> Technical <span class="rule">2</span></label>
    </div>`).join("");

  $$("#slotCards [data-slot]").forEach((el) => {
    const commit = () => {
      const sl = state.slots[Number(el.dataset.slot)];
      sl[el.dataset.f] = el.type === "checkbox" ? el.checked : el.value;
      Store.setSetting("combo_slots", state.slots);
      // Only auto-rerun once the user has asked for results at least once;
      // before that, an unfiltered two-slot search is a lot of work to do
      // for someone who is still setting up.
      if (state.comboRan) debouncedCombos();
    };
    el.addEventListener(el.tagName === "INPUT" && el.type !== "checkbox" ? "input" : "change", commit);
  });
}

/** Is every course of this combination already in the plan? */
const comboInPlan = (combo) => combo.every((c) => Store.inPlan(term(), c.key));

function renderCombos() {
  const box = $("#combos");
  box.innerHTML = state.comboPage.map((combo, i) => {
    const inPlan = comboInPlan(combo);
    return `
      <div class="combo" data-combo="${i}">
        <div class="combo-head">
          <h4>Option ${state.comboOffset + i + 1}</h4>
          <button class="ghost tiny ${inPlan ? "danger" : "addcombo"}" data-combo-plan="${i}">
            ${inPlan ? "Remove from plan" : "Add all to plan"}</button>
        </div>
        ${combo.map((c, si) => `<div class="row">
          <span class="slotno">${si + 1}</span>
          ${extLink(c, `${esc(c.subject)} ${esc(c.catalog)}`, "r-code")} ${extLink(c, esc(c.title))}
          <span style="color:var(--muted)"> — ${c.meetings.map((m) =>
            m.days.map((x) => x.slice(0, 3)).join(" ") + " " +
            fmt(m.start_min) + "–" + fmt(m.end_min)).join(" · ")}</span>
          ${policyBadges(c.policy)}
        </div>`).join("")}
      </div>`;
  }).join("");

  // Hovering an option previews the whole set, so you can see the shape of the
  // week it produces rather than one course at a time.
  $$("#combos .combo").forEach((el) => {
    const combo = state.comboPage[Number(el.dataset.combo)];
    el.addEventListener("mouseenter", () => setPreview(combo));
    el.addEventListener("mouseleave", () => setPreview([]));
  });

  $$("#combos [data-combo-plan]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      await toggleComboInPlan(state.comboPage[Number(b.dataset.comboPlan)], b);
    }));
}

/** Add or remove every course of a combination at once.
 *
 * Adding is a union rather than a replacement: nothing the student already
 * chose is silently discarded. The plan report is what tells them if the total
 * now exceeds the term's elective count -- guessing here would either destroy
 * work or hide the overflow.
 */
async function toggleComboInPlan(combo, btn = null) {
  if (!combo) return;
  const t = term();
  const removing = comboInPlan(combo);
  let n = 0;
  for (const c of combo) {
    if (removing === Store.inPlan(t, c.key)) {
      Store.togglePlan(t, c.key);
      if (removing) state.plan.delete(c.key);
      else state.plan.set(c.key, { ...c, in_plan: 1, enrolled: false });
      n++;
    }
  }
  setPreview([]);
  $("#planCount").textContent = String(Store.plan(t).length);
  renderGrid();      // instant feedback; the fetches below reconcile

  await withBusy(btn, () => Promise.all([loadPlan(), refreshResults()]));
  renderCombos();

  const total = Store.plan(t).length;
  const want = state.electivesThisTerm;
  toast(removing
    ? `Removed ${n} course${n === 1 ? "" : "s"}. Plan now has ${total}.`
    : `Added ${n} course${n === 1 ? "" : "s"}. Plan now has ${total} of ${want} ` +
      `elective${want === 1 ? "" : "s"} for this term.` +
      (total > want ? " That's more than this term needs — check My plan." : ""),
    !removing && total > want ? "warn" : "ok");
}

let comboTimer;
const debouncedCombos = () => {
  clearTimeout(comboTimer);
  comboTimer = setTimeout(() => findCombos(true), 400);
};

async function findCombos(resetPage = true) {
  if (resetPage) state.comboOffset = 0;
  syncSlots();
  const box = $("#combos");
  box.innerHTML = `<ul class="skelwrap">${skeletonCards(4, 3)}</ul>`;
  $("#comboPager").hidden = true;

  try {
    const d = await post("/api/combinations", {
      ...personal(),
      term: term(), buffer_min: buffer(),
      slots: state.slots.map((sl, i) => ({ ...sl, label: `Elective ${i + 1}` })),
      limit: state.comboLimit, offset: state.comboOffset,
    });
    state.comboRan = true;
    state.comboTotal = d.total;
    renderSlotCards(d.slots.map((s) => s.pool_size));

    const empty = d.slots.find((s) => s.pool_size === 0);
    if (!d.total) {
      box.innerHTML = `<p class="empty">${empty
        ? `No candidates at all for <b>${esc(empty.label)}</b> — that slot's filters
           match nothing with a meeting time. Loosen them.`
        : `Every pairing collides. Loosen a slot's filters, or reduce the
           ${buffer()}-minute travel buffer.`}</p>`;
      $("#comboSummary").hidden = true;
      return;
    }

    $("#comboSummary").hidden = false;
    $("#comboSummary").innerHTML =
      `<b>${d.total.toLocaleString()}${d.truncated ? "+" : ""}</b> combination${d.total === 1 ? "" : "s"} from ` +
      d.slots.map((s) => `${s.pool_size.toLocaleString()}`).join(" × ") + " candidates" +
      (d.truncated ? ` — stopped counting at ${d.total.toLocaleString()}; narrow a slot to see them all.` : "");

    state.comboPage = d.combinations;
    renderCombos();

    const from = state.comboOffset + 1;
    const to = state.comboOffset + d.combinations.length;
    $("#comboPager").hidden = d.total <= state.comboLimit;
    $("#comboRange").textContent = `${from}–${to} of ${d.total.toLocaleString()}${d.truncated ? "+" : ""}`;
    $("#comboPrev").disabled = state.comboOffset === 0;
    $("#comboNext").disabled = to >= d.total;
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
  renderExtState();
  $("#importModal").hidden = false;
}

/** Show either the one-click button or the install instructions, never both. */
function renderExtState() {
  $("#impOneClick").hidden = !state.extension;
  $("#impInstall").hidden = state.extension;
  $("#impExtBadge").hidden = !state.extension;
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
    renderExtState();
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

function openBlock(prefill = null) {
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

  // Clicking an empty slot should land in the editor already describing that
  // slot -- otherwise the click saved nothing over pressing "+ Block".
  const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  if (prefill) {
    blockState.days.add(prefill.day);
    $$("#bDays input").forEach((i) => {
      if (Number(i.value) === prefill.day) i.checked = true;
    });
    $("#bStart").value = hhmm(prefill.start);
    $("#bEnd").value = hhmm(prefill.end);
  } else {
    $("#bStart").value = "18:00";
    $("#bEnd").value = "19:30";
  }

  $("#blockModal").hidden = false;
  $("#bTitle").focus();
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
["#school", "#requirement", "#freeOnly", "#fProjectBased",
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
$("#findCombos").addEventListener("click", () => findCombos(true));
$("#pick").addEventListener("change", () => {
  renderSlotCards();
  if (state.comboRan) findCombos(true);
});
$("#comboPrev").addEventListener("click", () => {
  state.comboOffset = Math.max(0, state.comboOffset - state.comboLimit);
  findCombos(false);
});
$("#comboNext").addEventListener("click", () => {
  state.comboOffset += state.comboLimit;
  findCombos(false);
});
$("#editProfile").addEventListener("click", openProfile);
$("#cancelProfile").addEventListener("click", () => { $("#profileModal").hidden = true; });
$("#saveProfile").addEventListener("click", saveProfile);
$("#gridAddBlock").addEventListener("click", () => openBlock());
$("#scheduleToggle").addEventListener("click", () =>
  setDrawer(!$("#schedulePane").classList.contains("open")));
$("#scheduleClose").addEventListener("click", () => setDrawer(false));
$("#scheduleScrim").addEventListener("click", () => setDrawer(false));
// Crossing the breakpoint with the drawer open would leave the scrim and the
// body scroll-lock stranded over a layout that no longer has a drawer.
let fitTimer;
window.addEventListener("resize", () => {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(renderGrid, 120);
});
drawerQuery.addEventListener("change", (e) => {
  if (!e.matches) {
    $("#schedulePane").classList.remove("open");
    $("#scheduleScrim").hidden = true;
    document.body.classList.remove("drawer-open");
  }
  renderGrid();
});
$("#fbProfile").addEventListener("click", openProfile);
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
  const anyModal = $$(".modal").some((m) => !m.hidden);
  $$(".modal").forEach((m) => { m.hidden = true; });
  // Escape closes the topmost thing only: a modal opened from the drawer
  // should not also dismiss the drawer underneath it.
  if (!anyModal) setDrawer(false);
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
  renderFilterBarVisibility();
  if (t.dataset.tab === "combos" && !$("#slotCards").children.length) renderSlotCards();
}));
renderFilterBarVisibility();

// "4 minutes ago" goes stale on a tab left open all afternoon.
setInterval(() => { if (state.meta) renderMeta(); }, 60_000);

boot().catch((e) => { $("#metaAgo").textContent = `Failed to load: ${e.message}`; });
