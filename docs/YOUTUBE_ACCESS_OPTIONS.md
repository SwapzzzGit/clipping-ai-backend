# YouTube download blocks: options and recommendation

Date: 5 Nov 2026. Scope: the "Sign in to confirm you're not a bot" block that stops yt-dlp from downloading a
video or its captions (`clipper/download.py`, `clipper/transcript.py`).

## Short answer

- No technique makes the paste-a-YouTube-link route reliable. YouTube's bot check is an anti-automation control.
  Every workaround we found does one of three things: uses a logged-in account, generates client attestation
  tokens, or hides our IP address. Those are circumvention, which you asked us not to do.
- YouTube's Terms also restrict automated access and downloading (see below). The link route depends on both.
- The sustainable route is video you have rights to: your own uploads (already built), your own YouTube videos,
  and licensed or Creative Commons material.
- Recommendation: make upload the main input. Keep YouTube links as best-effort with a clear failure message, and
  settle the download policy (decision 1 in the architecture document) before any commercial use.

## What the block is

yt-dlp reports "Sign in to confirm you're not a bot" when YouTube refuses an unauthenticated request from this
machine. Community threads show many users hit it with no cookies at all, and no maintainer fix exists. Our code
already stops retries on this message and fails the job with a clear explanation. It does not try to get around it.

## Options

| # | Option | Reliable? | YouTube's terms | Verdict |
|---|---|---|---|---|
| 1 | Upload the file (built, item 2) | Yes | Fine: it is your file | **Main path** |
| 2 | Your own videos: YouTube Studio's per-video download, or Google Takeout (your original uploads) | Yes, but manual | Fine: your content | **Use for your channel.** Quality is disputed (see notes) |
| 3 | Your own captions through the YouTube Data API (OAuth) | Yes, for your own videos | Allowed | **Good add-on.** Captions for other people's videos are not available this way |
| 4 | Licensed, Creative Commons or public-domain videos | Yes | Allowed where the licence permits | **Good for sourcing.** Record the licence for each source |
| 5 | Written approval from YouTube for automated access | Yes, if granted | Terms allow automated access with written approval | Slow, and unlikely for a clipping tool. Only if you want scale |
| 6 | Cookies from a logged-in browser (a yt-dlp option) | Sometimes | Automated access; risk of account restriction | **Not recommended.** We will not build it without your explicit decision and legal review |
| 7 | Proof-of-origin token providers (bgutil, getpot plugins) | Sometimes | Tokens prove the request comes from a genuine client; generating them to pass the check is circumvention | **Not recommended.** We will not build it |
| 8 | Residential or other proxies | Sometimes | Hides our IP from YouTube; circumvention | **Not recommended** |
| 9 | Keep yt-dlp updated; lower download concurrency | Reduces blocks, does not remove them | Changes nothing | Hygiene only |

## How other open-source projects handle it

- **Pinchflat** (self-hosted yt-dlp wrapper). Third-party guides suggest a cookies file and lower concurrency. Its own
  wiki says yt-dlp no longer recommends YouTube cookies because they can cause IP bans, and advises against using the
  feature until more is known. That page was last edited in March 2025, so it may be out of date.
- **yt-dlp community** (issue 10128). Users report the block with and without cookies. Some say a VPN helped; others
  say it did not. No authoritative fix.
- **yt-dlp PO Token Guide.** It documents attestation tokens for "genuine client" requests. It does not describe bot
  detection as the purpose.

The pattern: the projects that keep working tend to be the ones that stop depending on live YouTube downloads for their
core feature, or that accept the account and IP risk of cookies. We should not adopt the second.

## What YouTube's Terms say

From the fetched copy of [YouTube's Terms of Service](https://www.youtube.com/t/terms), under "Your Use of the Service":

- **Item 3:** no access to the Service "using any automated means (such as robots, botnets or scrapers)", except
  public search engines that follow robots.txt, or access YouTube has approved in writing in advance.
- **Item 1:** no downloading, reproducing or other use of any part of the Service or its Content without authorization.

This applies to yt-dlp use on YouTube links whether or not the bot check appears. The block is the enforcement signal.
This is not legal advice. Get a review before any commercial use.

## What this means for the app

- The paste-a-link feature depends on automated access. Transcripts are fetched with yt-dlp too, so they are affected
  the same way.
- The official caption route (`captions.download`) works only for a user who can edit the video. The
  [official reference](https://developers.google.com/youtube/v3/docs/captions/download) says the method "requires the
  user to have permission to edit the video". It does not help with other people's videos.
- Architecture decision 1 already recommends uploads plus owner-rights content only. This report supports it.

## Proposed implementation (no circumvention)

1. **Upload first.** Make the upload the default in the job form. Label the YouTube field "best effort, may be blocked".
2. **Fail clearly on a block.** Keep the current behaviour: stop, explain, and offer "finish with an uploaded file".
   Do not retry against YouTube automatically.
3. **Own-channel import (optional).** Use the YouTube Data API with OAuth to list your own videos and fetch their captions.
   The publisher already has a YouTube OAuth connector, but captions may need an extra scope. Check before building.
4. **Record the source of each job.** Store where the video came from (upload, your channel, licensed source) and its
   licence, so posting can check rights.
5. **Do not build** cookies, proof-of-origin tokens or proxies, unless you decide otherwise and get a legal review first.

## Implemented

- A job that YouTube blocks is marked `blocked`. Its page shows "YouTube blocked this download" with the steps: get the
  video from a source you have rights to, upload it, and confirm you have the rights.
- `POST /api/jobs/{id}/source` continues the same job with the uploaded file. It accepts only a job YouTube blocked,
  requires the rights confirmation, keeps the YouTube link as `original_url`, and picks the moments again from the file.
- Retry is hidden for a blocked job, so the app does not keep asking YouTube for the same video. Nothing retries
  against YouTube automatically.
- The new-job form opens on "Upload from this PC". The YouTube tab is labelled best effort.
- Not built: the own-channel caption import (item 3 above), and cookies, proof-of-origin tokens or proxies (items 6 to 8).

Tests: `backend/tests/test_blocked_source.py`. Full suite: 154 passed.

## Notes on disputed points

- **Studio download quality.** One guide says the download keeps the original quality. Another says it can be smaller,
  often 720p. Check on one of your own videos before relying on it.
- **Google Takeout.** Search results describe Takeout as giving back the original files. Those results mostly date from
  2012. Check takeout.google.com.
- **Terms wording.** The text above came from a summary of the live page. Read the page yourself before relying on the
  exact wording.

## Sources

Fetched and read:
- [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [YouTube Terms of Service](https://www.youtube.com/t/terms)
- [YouTube Data API: captions.download](https://developers.google.com/youtube/v3/docs/captions/download)

From search summaries (not opened):
- [yt-dlp issue 10128: "Sign in to confirm you're not a bot"](https://github.com/yt-dlp/yt-dlp/issues/10128)
- [Pinchflat cookies wiki](https://github.com/kieraneglin/pinchflat/wiki/Cookies)
- [Pinchflat YouTube cookies wiki](https://github.com/kieraneglin/pinchflat/wiki/YouTube-Cookies)
- [yt-dlp Extractors wiki (mirror)](https://lab.trianta.dev/mirrors/yt-dlp.yt-dlp/wiki/Extractors)
- [YouTube Help: download videos you've uploaded](https://support.google.com/youtube/answer/56100)
- [TechCrunch: Google Takeout adds YouTube original files (2012)](https://techcrunch.com/?p=660785)
