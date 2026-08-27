# manifest.json — two constraints that are easy to break

`manifest.json` cannot hold comments, and Chrome validates parts of it
strictly, so the reasoning lives here instead.

## `content_scripts[].matches` is a security boundary

Any page the content script runs on can ask the extension for the user's class
schedule. Keep the list to the planner's own origins.

**Never widen it to `https://*.vercel.app/*`** — that would let any site hosted
on Vercel read a user's timetable.

If you deploy to a different domain, either edit that list, or just download the
extension from `/api/extension.zip` on the running site: that endpoint injects
the serving origin into `matches` automatically, so the copy you download always
trusts exactly the site you got it from.

## Two things that broke it before

**No `"//"` pseudo-comment keys.** JSON has no comments, and Chrome rejects
`content_scripts` entries carrying unrecognized keys. The symptom is indirect
and confusing: the extension still loads and the popup still opens, but
`chrome.runtime.getManifest().content_scripts` comes back empty, the content
script is never registered on any page, and the planner's one-click import
button never appears because nothing ever announces itself.

**No `"type": "module"` on the service worker.** A module service worker
evaluates its module graph asynchronously, so on a cold start a message can
arrive before `chrome.runtime.onMessage` has been registered — the port closes
and the sender gets *"The message port closed before a response was received."*
`background.js` has no imports, so nothing needed the module type. The popup and
content script also retry once on that specific error, since a cold worker is
the normal case for the first click after a browser restart.
