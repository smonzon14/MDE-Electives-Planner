// my.harvard /calendar/load/ -> the planner's meeting shape.
//
// This is the single parser for both import routes. The extension deliberately
// does not parse: it fetches the endpoint with the user's own cookies and hands
// back the raw JSON, and this module -- shipped fresh with the page -- turns it
// into blocks. So the one-click path and the copy-paste path run identical
// code, and my.harvard changing its payload needs a redeploy, not a new
// extension release for every user.
//
// Real payload shape:
//   { events: [ { title, startTime:"14:15:00", endTime, daysOfWeek:[1,3,5],
//                 item: { start:"2026-09-02", end:"2026-12-03", courseUrl,
//                         code:"STU 1231 ", session, classNumber, location } } ] }

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

// Accepts "1:30pm", "1:30 PM", "13:30", "01:30:00" -> minutes past midnight.
export function parseTime(value) {
  if (value == null) return null;
  const s = String(value).trim();
  let m = s.match(/^(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?$/i);
  if (!m) {
    m = s.match(/(\d{1,2}):(\d{2})\s*([ap])\.?m\.?/i);
    if (!m) return null;
  }
  let hour = parseInt(m[1], 10);
  const min = parseInt(m[2], 10);
  const mer = (m[3] || "").toLowerCase();
  if (mer.startsWith("p") && hour !== 12) hour += 12;
  if (mer.startsWith("a") && hour === 12) hour = 0;
  if (hour > 23 || min > 59) return null;
  return hour * 60 + min;
}

export function dayMaskFrom(value) {
  if (value == null) return 0;
  let mask = 0;

  if (Array.isArray(value)) {
    for (const v of value) mask |= dayMaskFrom(v);
    return mask;
  }
  if (typeof value === "number") return 1 << value;

  const s = String(value).trim();
  // Full or abbreviated day names.
  DAY_NAMES.forEach((name, i) => {
    if (new RegExp(`\\b${name}\\b`, "i").test(s)) mask |= 1 << i;
  });
  if (mask) return mask;

  // Compact codes: "MWF", "TuTh", "MoWeFr".
  const compact = [
    ["Su", 0], ["Mo", 1], ["Tu", 2], ["We", 3], ["Th", 4], ["Fr", 5], ["Sa", 6],
  ];
  let rest = s;
  for (const [abbr, idx] of compact) {
    const re = new RegExp(abbr, "gi");
    if (re.test(rest)) { mask |= 1 << idx; rest = rest.replace(re, ""); }
  }
  if (mask) return mask;

  for (const ch of s) {
    const i = { U: 0, M: 1, T: 2, W: 3, R: 4, F: 5, S: 6 }[ch.toUpperCase()];
    if (i !== undefined) mask |= 1 << i;
  }
  return mask;
}

function pick(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] != null && obj[k] !== "") return obj[k];
  }
  return null;
}

// /course/STU1231/2026-Fall/001 -> {code, term, section}
function parseCourseUrl(url) {
  const m = /^\/course\/([^/]+)\/([^/]+)\/([^/]+)$/.exec(url || "");
  if (!m) return null;
  return { code: m[1], term: m[2].replace(/-/g, " "), section: m[3] };
}

function parseTimeFromISO(v) {
  if (!v || typeof v !== "string") return null;
  const d = new Date(v);
  if (isNaN(d.getTime())) return null;
  return d.getHours() * 60 + d.getMinutes();
}

function dayMaskFromISO(v) {
  if (!v || typeof v !== "string") return 0;
  const d = new Date(v);
  if (isNaN(d.getTime())) return 0;
  return 1 << d.getDay();
}

// Exact parser for the real shape. Three details it depends on:
//
//   - `daysOfWeek` is FullCalendar's convention (0 = Sunday), which is exactly
//     the planner's bitmask order, so the mask is a direct shift.
//   - Use `item.start` / `item.end`, NEVER `startRecur` / `endRecur`. The recur
//     fields are display bounds -- every event carries the same 2026-11-30
//     endRecur while the real term ends 2026-12-03/04 -- so trusting them
//     shortens every course by about a month and breaks partial-term conflict
//     detection.
//   - `item.courseUrl` is the reliable join key back into the catalog;
//     `item.code` arrives as "STU 1231 " with stray spaces.
function parseEvents(data) {
  const events = Array.isArray(data?.events) ? data.events : null;
  if (!events) return null;

  const items = [];
  for (const ev of events) {
    const it = ev.item || {};
    const startMin = parseTime(ev.startTime ?? it.startTime);
    const endMin = parseTime(ev.endTime ?? it.endTime);
    if (startMin == null || endMin == null || endMin <= startMin) continue;

    let mask = 0;
    if (Array.isArray(ev.daysOfWeek)) {
      for (const d of ev.daysOfWeek) {
        if (Number.isInteger(d) && d >= 0 && d <= 6) mask |= 1 << d;
      }
    }
    // "…|Mon Wed Fri" is the authoritative fallback if daysOfWeek is absent.
    if (!mask && typeof it.schedule === "string") {
      mask = dayMaskFrom(it.schedule.split("|").pop());
    }
    if (!mask) continue;

    const fromUrl = parseCourseUrl(it.courseUrl);
    const code = fromUrl?.code || String(it.code || "").replace(/\s+/g, "");

    items.push({
      title: String(ev.title || it.code || "Class").slice(0, 200),
      code: code.slice(0, 40),
      section: (fromUrl?.section || it.section || "").slice(0, 40),
      term: fromUrl?.term || it.term || "",
      day_mask: mask,
      start_min: startMin,
      end_min: endMin,
      raw_time: `${ev.startTime} - ${ev.endTime}`,
      start_date: it.start || null,
      end_date: it.end || null,
      session: it.session || "",
      class_number: String(it.classNumber || ""),
      location: it.location || "",
      instructor: it.instructorName || "",
    });
  }
  return items;
}

// Fallback: walk an arbitrary JSON tree and collect anything that looks like a
// class meeting. Kept so a shape change degrades instead of breaking outright.
function parseHeuristic(data) {
  const items = [];
  const seen = new Set();

  function visit(node) {
    if (node == null || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(visit); return; }

    const start = pick(node, ["start", "startTime", "start_time", "startDate", "begin", "startsAt"]);
    const end = pick(node, ["end", "endTime", "end_time", "endDate", "finish", "endsAt"]);
    const startMin = parseTime(start) ?? parseTimeFromISO(start);
    const endMin = parseTime(end) ?? parseTimeFromISO(end);

    if (startMin != null && endMin != null && endMin > startMin) {
      const dayRaw = pick(node, ["days", "day", "daysOfWeek", "meetingDays", "dayOfWeek", "weekday"]);
      let mask = dayMaskFrom(dayRaw);
      if (!mask) mask = dayMaskFromISO(start);

      const title = pick(node, ["title", "name", "courseTitle", "className", "subject", "description"]) || "Class";
      const code = pick(node, ["code", "courseCode", "catalogNumber", "classCode", "course"]) || "";
      const section = pick(node, ["section", "sectionCode", "classSection"]) || "";

      const dedupe = `${title}|${mask}|${startMin}|${endMin}`;
      if (mask && !seen.has(dedupe)) {
        seen.add(dedupe);
        items.push({
          title: String(title).slice(0, 200),
          code: String(code).replace(/\s+/g, "").slice(0, 40),
          section: String(section).slice(0, 40),
          term: "",
          day_mask: mask,
          start_min: startMin,
          end_min: endMin,
          raw_time: `${start} - ${end}`,
          start_date: null,
          end_date: null,
          session: "",
          class_number: "",
          location: "",
          instructor: "",
        });
      }
    }
    Object.values(node).forEach(visit);
  }

  visit(data);
  return items;
}

/** Parse a /calendar/load/ payload. Exact path first, heuristic as a net. */
export function parseCalendar(data) {
  const exact = parseEvents(data);
  if (exact && exact.length) return { items: exact, mode: "exact" };
  return { items: parseHeuristic(data), mode: "heuristic" };
}

/** Parse pasted text. Signed-out users paste the login page, so say so. */
export function parsePasted(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) throw new Error("Nothing pasted.");
  if (/^\s*</.test(trimmed)) {
    throw new Error(
      "That looks like a web page, not JSON — you were probably signed out. " +
      "Sign in to my.harvard, reload the calendar URL, and copy again.");
  }
  let json;
  try {
    json = JSON.parse(trimmed);
  } catch {
    throw new Error("That isn't valid JSON. Copy the whole response, from { to }.");
  }
  const { items, mode } = parseCalendar(json);
  if (!items.length) {
    throw new Error(
      "Connected, but no meetings were recognized in that payload. " +
      "my.harvard may have changed its format.");
  }
  return { items, mode };
}

export { DAY_NAMES };
