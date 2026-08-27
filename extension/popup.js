// The popup is a fallback, not the main path: the planner page drives the
// import through content.js. This offers two escape hatches -- open the app,
// and copy the raw calendar JSON for the paste box if the bridge isn't working.

const $ = (id) => document.getElementById(id);

// Kept in sync with content_scripts.matches in manifest.json. Read from the
// manifest when possible, but never *depend* on it: a manifest entry Chrome
// rejects silently yields an empty list, and "no planner URL configured" is a
// useless thing to tell someone whose extension is otherwise fine.
const DEFAULT_APP_URL = "https://mde-electives-planner.vercel.app/";

function setStatus(msg, cls = "") {
  $("status").textContent = msg;
  $("status").className = cls;
}

function appUrl() {
  let matches = [];
  try {
    matches = chrome.runtime.getManifest().content_scripts?.[0]?.matches ?? [];
  } catch {
    matches = [];
  }
  const pick = matches.find((m) => m.startsWith("https://")) || matches[0];
  return pick ? pick.replace(/\*$/, "") : DEFAULT_APP_URL;
}

/** Ask the service worker for the calendar, retrying a cold start once.
 *
 * A dormant service worker is the normal case for the first click after a
 * browser restart. Chrome is supposed to wake it and deliver the message, but
 * the port can close first -- surfacing as "The message port closed before a
 * response was received." One retry after a short delay covers it.
 */
function requestCalendar(retriesLeft = 1) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "FETCH_CALENDAR" }, (res) => {
      const err = chrome.runtime.lastError;
      if (err) {
        const cold = /message port closed|Receiving end does not exist/i.test(err.message || "");
        if (cold && retriesLeft > 0) {
          setTimeout(() => requestCalendar(retriesLeft - 1).then(resolve), 350);
          return;
        }
        resolve({ ok: false, error: err.message || "No response from the extension worker." });
        return;
      }
      resolve(res ?? { ok: false, error: "The extension worker returned nothing." });
    });
  });
}

$("open").addEventListener("click", () => {
  chrome.tabs.create({ url: appUrl() });
  window.close();
});

$("copy").addEventListener("click", async () => {
  $("copy").disabled = true;
  setStatus("Fetching your calendar…");

  const res = await requestCalendar();
  $("copy").disabled = false;

  if (!res.ok) return setStatus(res.error, "err");

  const text = JSON.stringify(res.payload);
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    return setStatus("Fetched it, but the clipboard was blocked. Reopen the " +
                     "popup and try again.", "err");
  }
  const n = Array.isArray(res.payload?.events) ? res.payload.events.length : 0;
  setStatus(`Copied ${(text.length / 1024).toFixed(1)} KB` +
            (n ? ` (${n} calendar entries)` : "") +
            ". Paste it into the planner's import box.", "ok");
});
