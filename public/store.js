// Local persistence -- the browser is the system of record for personal data.
//
// The server holds the catalog and the policy engine and nothing else: no
// accounts, no `user_key`, no schedules. Everything personal lives here and is
// sent with each request. See the module docstring in server/main.py for why.
//
// Three things are stored:
//   profile   the student's background (drives every eligibility verdict)
//   locked    hard commitments -- imported classes and hand-added blocks
//   plan      course keys being weighed up, per term
//
// The trade-off is no cross-device sync, so export()/import() exist to move
// state between browsers deliberately rather than through an account.

const STORAGE_KEY = "mde.planner.v1";

const EMPTY = () => ({
  version: 1,
  profile: null,
  settings: { buffer_min: 15, term: "" },
  terms: {},          // term -> { locked: [...], plan: [keys] }
});

// A private window, or a browser set to block site data, throws on access
// rather than returning null. Fall back to memory so the app still works for
// the session instead of failing to boot.
let memoryOnly = false;
let mem = null;

function readRaw() {
  if (memoryOnly) return mem;
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    memoryOnly = true;
    return mem;
  }
}

function writeRaw(str) {
  if (!memoryOnly) {
    try {
      localStorage.setItem(STORAGE_KEY, str);
      return;
    } catch {
      // Quota, or storage disabled mid-session. Keep going in memory.
      memoryOnly = true;
    }
  }
  mem = str;
}

let cache = null;

function data() {
  if (cache) return cache;
  const raw = readRaw();
  if (!raw) return (cache = EMPTY());
  try {
    const parsed = JSON.parse(raw);
    cache = migrate(parsed);
  } catch {
    // Corrupt payload: start clean rather than leaving the app unusable.
    cache = EMPTY();
  }
  return cache;
}

function migrate(d) {
  const out = { ...EMPTY(), ...d };
  out.settings = { ...EMPTY().settings, ...(d.settings || {}) };
  out.terms = d.terms || {};
  for (const t of Object.keys(out.terms)) {
    const bucket = out.terms[t] || {};
    out.terms[t] = {
      locked: Array.isArray(bucket.locked) ? bucket.locked : [],
      plan: Array.isArray(bucket.plan) ? bucket.plan : [],
    };
  }
  out.version = 1;
  return out;
}

function persist() {
  writeRaw(JSON.stringify(data()));
}

function bucket(term) {
  const d = data();
  if (!d.terms[term]) d.terms[term] = { locked: [], plan: [] };
  return d.terms[term];
}

// Local ids: the server used to hand these out with the row's rowid. Blocks are
// now client-owned, so they need an identity that survives a reload.
let idSeq = 0;
function localId() {
  idSeq += 1;
  return `b${Date.now().toString(36)}${idSeq.toString(36)}`;
}

export const Store = {
  get storageAvailable() {
    return !memoryOnly;
  },

  // ------------------------------------------------------------- profile ---

  getProfile() {
    return data().profile;
  },

  // Null profile means "not set yet" -- the UI prompts for it. The server
  // defaults to Year 1 Fall / no background when none is sent, so search still
  // works before the student fills it in.
  setProfile(profile) {
    data().profile = profile;
    persist();
  },

  /** The profile shaped for the API, always a valid object. */
  profileForApi() {
    const p = data().profile;
    return {
      year: p?.year ?? 1,
      season: p?.season ?? "Fall",
      seas_background: !!p?.seas_background,
      seas_areas: p?.seas_areas ?? [],
      physical_design_background: !!p?.physical_design_background,
      cs50_status: p?.cs50_status ?? "required",
      completed_codes: p?.completed_codes ?? [],
    };
  },

  // ------------------------------------------------------------ settings ---

  getSetting(key, fallback = null) {
    const v = data().settings[key];
    return v === undefined || v === null ? fallback : v;
  },

  setSetting(key, value) {
    data().settings[key] = value;
    persist();
  },

  // -------------------------------------------------------------- locked ---

  locked(term) {
    return bucket(term).locked;
  },

  /** Blocks shaped for the API -- only the fields the conflict engine needs. */
  lockedForApi(term) {
    return bucket(term).locked.map((i) => ({
      title: i.title || "",
      code: i.code || "",
      section: i.section || "",
      day_mask: i.day_mask,
      start_min: i.start_min,
      end_min: i.end_min,
      start_date: i.start_date || null,
      end_date: i.end_date || null,
      source: i.source || "manual",
      category: i.category || "obligation",
      course_key: i.course_key || "",
    }));
  },

  addLocked(term, item) {
    const b = bucket(term);
    b.locked.push({ ...item, id: item.id || localId() });
    b.locked.sort((a, z) => a.start_min - z.start_min);
    persist();
  },

  removeLocked(term, id) {
    const b = bucket(term);
    const before = b.locked.length;
    b.locked = b.locked.filter((i) => String(i.id) !== String(id));
    persist();
    return before - b.locked.length;
  },

  /** Replace everything imported from my.harvard, leaving manual blocks alone.
   *
   * Mirrors what the old server-side import did: it deleted rows matching
   * (user, term, source='harvard') before inserting. Scoping to `source` is the
   * point -- a re-import must not wipe the obligations someone typed in by hand.
   */
  replaceImported(term, items) {
    const b = bucket(term);
    const manual = b.locked.filter((i) => i.source !== "harvard");
    const imported = items.map((i) => ({ ...i, id: localId(), source: "harvard" }));
    b.locked = [...manual, ...imported].sort((a, z) => a.start_min - z.start_min);
    persist();
    return { imported: imported.length, kept: manual.length };
  },

  clearImported(term) {
    const b = bucket(term);
    const before = b.locked.length;
    b.locked = b.locked.filter((i) => i.source !== "harvard");
    persist();
    return before - b.locked.length;
  },

  // ---------------------------------------------------------------- plan ---

  plan(term) {
    return bucket(term).plan;
  },

  inPlan(term, key) {
    return bucket(term).plan.includes(key);
  },

  togglePlan(term, key) {
    const b = bucket(term);
    const i = b.plan.indexOf(key);
    if (i >= 0) b.plan.splice(i, 1);
    else b.plan.push(key);
    persist();
    return i < 0;   // true if it was added
  },

  // ------------------------------------------------------ export / import ---
  //
  // The account-free substitute for sync: a plain JSON file the student moves
  // between browsers themselves.

  export() {
    return JSON.stringify({ ...data(), exported_at: new Date().toISOString() }, null, 2);
  },

  /** Load an exported file, replacing everything. Throws if it isn't ours. */
  import(text) {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || !("terms" in parsed)) {
      throw new Error("That doesn't look like a planner export.");
    }
    cache = migrate(parsed);
    // Re-key locked blocks: ids from the exporting browser may collide.
    for (const t of Object.keys(cache.terms)) {
      cache.terms[t].locked = cache.terms[t].locked.map((i) => ({ ...i, id: localId() }));
    }
    persist();
    const terms = Object.keys(cache.terms);
    return {
      terms,
      locked: terms.reduce((n, t) => n + cache.terms[t].locked.length, 0),
      plan: terms.reduce((n, t) => n + cache.terms[t].plan.length, 0),
    };
  },

  clear() {
    cache = EMPTY();
    persist();
  },

  isEmpty() {
    const d = data();
    if (d.profile) return false;
    return Object.values(d.terms).every(
      (b) => !b.locked.length && !b.plan.length);
  },
};
