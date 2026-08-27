// Service worker: fetch the user's my.harvard calendar using their own browser
// session and hand the raw bytes back. That is the extension's whole job.
//
// Because the extension holds host permissions for my.harvard.edu, this fetch
// carries the user's existing cookies. No password, no MFA replay, no
// credentials stored anywhere -- we simply ride the session the user already
// established by signing in normally.
//
// Deliberately does NOT parse the payload and does NOT talk to any server.
// Parsing lives in the web app (public/calendar.js), so:
//   - the one-click route and the copy-paste route run identical code;
//   - a my.harvard format change is fixed by a redeploy, not by asking every
//     user to update their extension;
//   - this file stays small enough to be obviously safe on review.

const CALENDAR_URL = "https://my.harvard.edu/calendar/load/";

async function fetchCalendar() {
  let res;
  try {
    res = await fetch(CALENDAR_URL, {
      method: "GET",
      credentials: "include",
      headers: { accept: "*/*" },
    });
  } catch (e) {
    throw new Error(`Could not reach my.harvard (${e.message}). Are you online?`);
  }

  const text = await res.text();
  if (!res.ok) {
    throw new Error(`my.harvard returned ${res.status}. Are you signed in?`);
  }
  try {
    return JSON.parse(text);
  } catch {
    // A signed-out request is answered with the HTML login page, not an error.
    throw new Error(
      "my.harvard returned a web page instead of data — you are probably " +
      "signed out. Sign in at my.harvard.edu, then try again.");
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "FETCH_CALENDAR") return;
  fetchCalendar()
    .then((payload) => sendResponse({ ok: true, payload }))
    .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
  return true;   // keep the message channel open for the async response
});
