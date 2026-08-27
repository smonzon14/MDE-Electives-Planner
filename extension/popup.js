// The popup is a fallback, not the main path: the planner page drives the
// import through content.js. This offers two escape hatches -- open the app,
// and copy the raw calendar JSON for the paste box if the bridge isn't working.

const $ = (id) => document.getElementById(id);

function setStatus(msg, cls = "") {
  $("status").textContent = msg;
  $("status").className = cls;
}

/** Derive the app URL from the manifest so it isn't configured in two places. */
function appUrl() {
  const matches = chrome.runtime.getManifest().content_scripts?.[0]?.matches || [];
  const pick = matches.find((m) => m.startsWith("https://")) || matches[0];
  return pick ? pick.replace(/\*$/, "") : null;
}

$("open").addEventListener("click", () => {
  const url = appUrl();
  if (!url) return setStatus("No planner URL is configured in the manifest.", "err");
  chrome.tabs.create({ url });
  window.close();
});

$("copy").addEventListener("click", () => {
  $("copy").disabled = true;
  setStatus("Fetching your calendar…");

  chrome.runtime.sendMessage({ type: "FETCH_CALENDAR" }, async (res) => {
    $("copy").disabled = false;

    if (chrome.runtime.lastError || !res) {
      return setStatus(chrome.runtime.lastError?.message ||
                       "No response from the extension worker.", "err");
    }
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
});
