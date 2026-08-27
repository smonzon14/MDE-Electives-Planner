// Bridge between the planner page and the service worker.
//
// The page cannot fetch my.harvard itself (cross-origin, and it has no business
// holding those cookies), and the service worker cannot reach into the page.
// This content script is the only link, and it runs ONLY on the origins listed
// under `content_scripts.matches` in manifest.json.
//
// That match list is a security boundary, not a convenience: any page it runs on
// can ask the extension for the user's class schedule. Keep it to the planner's
// own origins -- never a wildcard like https://*.vercel.app/*, which would let
// any site hosted there read the user's timetable.

const PAGE = "mde-page";
const EXT = "mde-extension";

function announce() {
  window.postMessage({ source: EXT, type: "MDE_EXT_READY" }, window.location.origin);
}

window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const msg = ev.data;
  if (!msg || msg.source !== PAGE) return;

  // The page asks on boot, in case it started listening after we announced.
  if (msg.type === "MDE_EXT_PING") return announce();
  if (msg.type !== "MDE_FETCH_CALENDAR") return;

  chrome.runtime.sendMessage({ type: "FETCH_CALENDAR" }, (res) => {
    const reply = (payload) =>
      window.postMessage({ source: EXT, ...payload }, window.location.origin);

    // A dead service worker, or an extension update mid-session.
    if (chrome.runtime.lastError || !res) {
      return reply({
        type: "MDE_CALENDAR_ERROR",
        error: chrome.runtime.lastError?.message ||
               "The extension's background worker did not respond. Reload this page.",
      });
    }
    if (!res.ok) return reply({ type: "MDE_CALENDAR_ERROR", error: res.error });
    reply({ type: "MDE_CALENDAR_RESULT", payload: res.payload });
  });
});

// Announce unprompted too, for the case where the page's listener is already up.
announce();
