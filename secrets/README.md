# secrets/

Per-user credentials and config. **Everything here is gitignored** except the
`*.example.json` templates and this README — your real keys are never committed.

Most of this is optional: the app runs without any of it, and each integration
degrades gracefully when its file is missing. The easiest way to fill these in is
the in-app **⚙ Settings** screen (top-right) — but you can also drop the files in
by hand. Copy each `*.example.json` to the real filename and fill in your values.

| File | What it is | How to get it | Needed for |
|------|-----------|---------------|-----------|
| `settings.json` | Your channel identity + optional Claude API key | Set it in the ⚙ Settings screen (written for you) | The Subscribe CTA / clip-package agent using **your** channel |
| `client_secret.json` | Google **Desktop OAuth client** JSON | [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials → *Create OAuth client ID* → **Desktop app**. Enable the *YouTube Data API v3* and *YouTube Analytics API v2*. | The **YouTube overview** tab (your channel stats) |
| `youtube_token.json` | Cached OAuth token (auto-created) | Created automatically after you click **Connect YouTube** in Settings (or run `scripts/youtube_stats.py` once) | — (generated) |
| `twitch.json` | Twitch app `client_id` + `client_secret` | A free app at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) | **Twitch** clip search (Search → Experimental) |
| `reddit.json` | Reddit script-app `client_id` + `client_secret` | A free "script" app at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) | **Reddit** clip search (Search → Experimental) |

## Claude / AI features

The AI-powered features (experimental clip search, the clip-package agent) use the
local **`claude` CLI** (Claude Code) if it's installed and signed in — no key file
needed. Alternatively, paste an **Anthropic API key** in ⚙ Settings (stored in
`settings.json`); the backend passes it to the `claude` subprocess. If neither is
present, those features simply fall back to non-AI ranking.

⚠️ Never commit real credentials. Only the `*.example.json` files and this README
are tracked; the rest of `secrets/` stays local.
