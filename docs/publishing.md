# Publishing clips to YouTube Shorts, TikTok and Instagram Reels

The `publisher/` package connects any number of social accounts (several per platform) through
OAuth and publishes rendered clips to many of them at once, each with its own per-platform
title, caption and hashtags.

```
clips (manifest.json) ──► variants (one PostSpec per clip × account, editable)
                              │
                              ▼
                       publish job ──► queue ──► YouTube / TikTok / Instagram / mock connectors
                       (file-backed)    • per-platform + per-account concurrency
                                        • retries with backoff, resumable uploads
                                        • scheduling, per-target status + post URL
```

Nothing is ever posted until you create a publish job. Until each platform's app is registered
and its env vars are set, that platform shows as "not configured" and refuses to connect.

---

## 1. Common setup

| Env var | Purpose |
|---|---|
| `CLIPPER_SECRET_KEY` | Encrypts OAuth tokens at rest (Fernet key or any long passphrase). If unset, a key is generated into `backend/data/publish/secret.key`. That folder carries its own `.gitignore`. **Losing the key means every account has to be reconnected.** |
| `PUBLISHER_REDIRECT_BASE` | Public origin of the backend that the platforms redirect back to. Default `http://localhost:8000`. Callback URL: `<base>/api/publish/oauth/<platform>/callback`. |
| `PUBLISHER_PUBLIC_BASE_URL` | An https origin that reaches this backend. Instagram needs it because it downloads the video from a URL (see §4). |
| `PUBLISHER_UI_URL` | Optional: where the OAuth callback page sends the browser afterwards (e.g. `http://localhost:5173/publish`). |
| `PUBLISHER_PLATFORMS` | Optional allow-list, e.g. `youtube,tiktok`. Add `mock` (or set `PUBLISHER_MOCK=1`) to show the offline test platform. |
| `PUBLISHER_DATA` | Optional: data folder (default `<CLIPPER_DATA>/publish`). |
| `PUBLISHER_WORKERS`, `PUBLISHER_MAX_ATTEMPTS`, `PUBLISHER_RETRY_BASE` | Queue tuning (defaults 8, 5, 20 s). |

**Getting an https URL for local development.** TikTok (web apps) and Meta only accept https
redirect URIs, and Instagram must be able to fetch the video. Run a tunnel to the backend:

```bash
cloudflared tunnel --url http://localhost:8000
```

Set both `PUBLISHER_REDIRECT_BASE` and `PUBLISHER_PUBLIC_BASE_URL` to the `https://….trycloudflare.com`
URL it prints, and register `<that URL>/api/publish/oauth/<platform>/callback` with each platform.
ngrok works the same way. A quick tunnel gets a new URL on every start, so a named tunnel (or a
fixed ngrok domain) saves re-registering. Google is the exception: it accepts
`http://localhost:8000/...` directly.

Install the one extra dependency: `pip install "cryptography>=43"`.

---

## 2. YouTube Shorts (Google Cloud)

1. Go to <https://console.cloud.google.com/> and create a project, e.g. "clipper-publisher".
2. **APIs & Services → Library → YouTube Data API v3 → Enable.**
3. **OAuth consent screen** (Google Auth Platform → Branding / Audience / Data access):
   - User type **External**.
   - Add the scopes `https://www.googleapis.com/auth/youtube.upload` and
     `https://www.googleapis.com/auth/youtube.readonly`.
   - While the app is in **Testing**, add every Google account you will connect as a *test user*.
     Testing-mode refresh tokens **expire after 7 days**, so expect a "reconnect needed" weekly.
     Publish the app ("In production") to avoid that. Sensitive scopes then need Google's verification.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   Authorized redirect URI: `http://localhost:8000/api/publish/oauth/youtube/callback`
   (or `<PUBLISHER_REDIRECT_BASE>/api/publish/oauth/youtube/callback`).
5. Set the env vars:
   ```
   YOUTUBE_CLIENT_ID=…apps.googleusercontent.com
   YOUTUBE_CLIENT_SECRET=…
   ```
   (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` also work.)

**Caveats**
- Quota: `videos.insert` costs **1 unit of the separate "Video Uploads" quota bucket, 100 uploads a
  day by default** (per the current [videos.insert docs](https://developers.google.com/youtube/v3/docs/videos/insert)).
  Older docs said 1,600 units of the general 10,000/day quota. Check
  *IAM & Admin → Quotas* for your project's real numbers. Status polling (`videos.list`) costs 1 unit
  of the general quota.
- The docs now state that uploads from unverified API projects are **not** forced to private, but
  "certain uploads might default to private based on creator account settings". If posts come out
  private, request an API audit through the YouTube API Services audit form.
- Shorts have no API flag. A vertical video of 3 minutes or less is classified as a Short, and the
  publisher also adds `#Shorts`. Longer clips are trimmed to 180 s automatically.
- Each Google account picks one channel on the consent screen. Connect again to add another
  channel (Brand Account).

## 3. TikTok (Login Kit + Content Posting API)

1. Go to <https://developers.tiktok.com/>, choose **Manage apps → Connect an app**, and create an
   app for your organisation or as an individual.
2. Add the products **Login Kit** and **Content Posting API**. In Content Posting API, enable
   **Direct Post**. Inbox (draft) upload works without it.
3. Scopes: `user.info.basic`, `user.info.profile`, `video.publish`, `video.upload`.
4. Platform / redirect URI:
   - **Web** app (default): redirect URI `https://<tunnel>/api/publish/oauth/tiktok/callback`
     (https only, no localhost).
   - **Desktop** app: set `TIKTOK_APP_TYPE=desktop`. TikTok then requires PKCE (sent automatically,
     hex-encoded S256 as TikTok documents) and allows a `http://localhost:8000/...` redirect.
5. Fill in the app details (icon, terms and privacy URLs, demo video), add your TikTok accounts
   under **Sandbox** / target users, and submit for review.
6. Set the env vars:
   ```
   TIKTOK_CLIENT_KEY=…
   TIKTOK_CLIENT_SECRET=…
   # after TikTok approves the Content Posting API audit:
   TIKTOK_AUDITED=1
   ```

**Caveats**
- **Unaudited apps can only post privately (`SELF_ONLY`)**, and at most 5 users can post in 24 h.
  The publisher forces private until `TIKTOK_AUDITED=1`, and the target's message says so.
  Once audited, the privacy level must be one of the creator's `privacy_level_options`.
  The publisher falls back to `SELF_ONLY` otherwise.
- Account option `post_mode: "inbox"` (PATCH `/api/publish/accounts/{id}`) uploads a draft to the
  creator's TikTok inbox to finish in the app, instead of posting directly.
- TikTok's audit expects the posting UI to show the creator's nickname, a privacy picker with no
  default, and the comment/duet/stitch toggles. Keep that in mind for the frontend.
- Access tokens last about 24 h and are refreshed automatically. Refresh tokens last about a year.
- The developer docs returned HTTP 503 while this was written (2026-10-31). Endpoints were taken
  from the indexed doc pages and the long-standing v2 API. Recheck them against
  <https://developers.tiktok.com/doc/content-posting-api-get-started> before going live.

## 4. Instagram Reels (Meta app, Instagram API with Instagram Login)

### How it works (users vs. server owner)

- **Users** only see **Accounts & categories → Connect Instagram**. That opens Meta's login pop-up,
  where they log in with Instagram (or Facebook), pick their Instagram account(s), and press
  **Allow**. Nothing else about Instagram appears in the UI. A platform that isn't configured on
  the server simply doesn't show up.
- **The server owner** configures everything once in `<repo>/.env` (gitignored; real env vars win):

  ```
  META_APP_ID=...            # Meta app → App settings → Basic
  META_APP_SECRET=...        # same page; never commit it
  INSTAGRAM_CONFIG_ID=...    # Use cases → Instagram API → API setup with Unified login → configuration ID
  PUBLISHER_PUBLIC_BASE_URL=https://app.example.com    # production domain
  # or, for local testing, a fixed free ngrok domain started by the backend:
  PUBLISHER_TUNNEL=ngrok
  NGROK_DOMAIN=your-name.ngrok-free.app
  NGROK_AUTHTOKEN=...
  ```

  In Meta's Unified-login setup, register `<public address>/api/publish/oauth/instagram/callback`
  as the Redirect URL. Meta only accepts real https addresses (localhost is rejected), so a fixed
  domain matters: the free localhost.run link gets a new address on every restart.
- New Meta apps can only use **Unified login**: Meta shows "API setup with Instagram login is not
  available for newer apps", so the app must belong to a business portfolio.
  Tokens are system-user tokens that never expire. Clips upload straight from the server
  (`rupload.facebook.com`), so Instagram never downloads anything from the public address.
- Until App Review, only people with a role on the Meta app (Instagram accounts added as testers)
  can connect. For anyone else: Business Verification (done for beautynleafofficial) and Advanced
  Access for `instagram_basic` and `instagram_content_publish` via App Review, then Live mode.
- Accounts must be Instagram **Business or Creator** accounts. Password-based private-API logins
  (instagrapi etc.) are deliberately not supported.

### Manual path

1. Make each Instagram account a **professional account** (Business or Creator) in the Instagram
   app: Settings → Account type and tools. No Facebook Page is needed for this login type.
2. Go to <https://developers.facebook.com/apps/> and create an app of type **Business**, then add
   the product **Instagram → API setup with Instagram business login**.
3. Under **Business login settings**, add the OAuth redirect URI
   `https://<tunnel>/api/publish/oauth/instagram/callback`.
4. Note the **Instagram app ID** and **Instagram app secret** shown there. These are not the Facebook
   app ID.
5. Permissions: `instagram_business_basic`, `instagram_business_content_publish`. In development
   mode, add your accounts as Instagram testers (App roles → Roles → Instagram testers) and accept
   the invite in the Instagram app. For other people's accounts, get Advanced Access through App Review.
6. Set the env vars:
   ```
   INSTAGRAM_CLIENT_ID=<Instagram app ID>
   INSTAGRAM_CLIENT_SECRET=<Instagram app secret>
   PUBLISHER_PUBLIC_BASE_URL=https://<tunnel>
   ```
   Optional: `INSTAGRAM_GRAPH_VERSION` (default `v26.0`).

**Caveats**
- Instagram **downloads the video from a public URL**. The default provider serves each clip at
  `/api/publish/media/<random token>/<file>` for 3 hours, and only clips queued for publishing are
  served. `PUBLISHER_PUBLIC_BASE_URL` must reach that endpoint. To use S3, R2 or a CDN instead,
  set `PUBLISHER_PUBLIC_URL_PROVIDER=yourmodule:YourClass`. The class needs a
  `url_for(path) -> str` method.
- Limit: **100 API-published posts per account per rolling 24 h**. Reels must be 3 s to 15 min.
  Captions can be up to 2,200 characters with at most 30 hashtags.
- Long-lived tokens last 60 days. The publisher refreshes them a week before expiry, even for idle
  accounts (an hourly check). A token that is never refreshed dies and needs reconnecting.
- Facebook Login for Business apps can upload the file directly through `rupload.facebook.com`
  instead of a public URL. That would be a separate `FacebookInstagramConnector`, not built yet.

---

## 5. Using it

Connect from the UI, or open
`GET /api/publish/oauth/<platform>/start?redirect=1` in a browser. You can connect as many accounts
per platform as you like; connecting the same account again just refreshes it.

```http
GET  /api/publish/platforms
GET  /api/publish/accounts
POST /api/publish/variants  {"source": "<job id or video id>", "account_ids": ["youtube-1a2b…", "tiktok-…"],
                             "clip_ids": [1, 2], "use_llm": false}
POST /api/publish/jobs      {"source": "...", "targets": [{"clip_id": 1, "account_id": "...",
                             "post": {"title": "...", "caption": "...", "hashtags": ["#x"], "privacy": "public"}}],
                             "scheduled_at": "2026-11-01T18:00:00+00:00"}
GET  /api/publish/jobs/<id> per-target status: scheduled/queued/uploading/processing/published/failed
```

Account statuses: `connected`, or `reconnect_needed` (refresh failed or access was revoked; run
the start flow again for that platform).

Offline demo, which never touches a real platform:

```bash
.venv/Scripts/python -m publisher.demo
```

Tests: `.venv/Scripts/python -m pytest tests/publisher`

### Categories and autopublish (Clipper Studio)

Clipper Studio builds on these endpoints. Most of the time you won't call them directly:

```http
GET    /api/categories                 categories with their connected accounts
POST   /api/categories                 {"name": "Finance", "account_ids": [...], "approval": "auto"|"manual",
                                        "min_score": 7, "max_clips_per_video": 3, "spacing_minutes": 30,
                                        "hashtags": ["#finance"], "use_llm_captions": false, "rules": ""}
PATCH  /api/categories/<id>            any of the fields above
POST   /api/batches                    {"text": "https://youtu.be/…, finance\n…", "default_category": "finance",
                                        "num_clips": 5}   (or "items": [{"url", "category"}])
POST   /api/jobs/<id>/publish          {"clip_ids"?, "account_ids"?, "scheduled_at"?}  approved clips only
GET    /api/posts                      every clip × account with status, post URL and error
```

A job remembers which (clip, account) pairs it has queued, so the render hook, approving a clip and the
Publish button can never post the same clip to the same account twice. Removing an account from a
category stops future posts only. Anything already queued still goes out unless you cancel it.

## 6. Adding a platform

Subclass `publisher.connectors.base.Connector`, declare `limits`, and implement `auth_url`,
`exchange_code`, `refresh`, `profile`, `upload` (and `status` when the platform processes
asynchronously). Then register the class in `publisher/connectors/__init__.py`. Planned candidates:

- Facebook Reels (Page video API)
- LinkedIn (Posts API + Videos API)
- X (media upload v2 + posts)
