<div align="center">

<h1>Multi-Platform Watermark-Free Downloader</h1>

<b>Douyin · Bilibili · TikTok · Xiaohongshu</b><br>
Paste a link — get the <b>watermark-free video</b>, <b>full-resolution photo sets</b> and <b>Live Photos</b>

[简体中文](./README.md) | **English**

<a href="../../releases/latest"><img src="https://img.shields.io/github/v/release/gdSHAY/douyin-wm-downloader?style=flat-square&label=Release&color=ff4d6d" alt="Release"></a>
<a href="../../releases"><img src="https://img.shields.io/github/downloads/gdSHAY/douyin-wm-downloader/total?style=flat-square&label=Downloads&color=ffd166" alt="Downloads"></a>
<a href="../../stargazers"><img src="https://img.shields.io/github/stars/gdSHAY/douyin-wm-downloader?style=flat-square&label=Stars&color=06d6a0" alt="Stars"></a>
<a href="../../forks"><img src="https://img.shields.io/github/forks/gdSHAY/douyin-wm-downloader?style=flat-square&label=Forks&color=4cc9f0" alt="Forks"></a>
<a href="../../issues"><img src="https://img.shields.io/github/issues/gdSHAY/douyin-wm-downloader?style=flat-square&label=Issues" alt="Issues"></a>

<br>

<a href="#-download--install"><img src="https://img.shields.io/badge/platform-Windows-0078d4?style=flat-square&logo=windows&logoColor=white" alt="Windows"></a>
<a href="#-download--install"><img src="https://img.shields.io/badge/platform-Android-3ddc84?style=flat-square&logo=android&logoColor=white" alt="Android"></a>
<img src="https://img.shields.io/badge/Python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white" alt="Python">
<img src="https://img.shields.io/badge/license-all%20rights%20reserved-8b8b8b?style=flat-square" alt="License">

</div>

---

A **local-first** multi-platform video downloader. Paste a link and it detects the platform, resolves the **watermark-free** video / photo set / Live Photo, and lets you preview and save it.

The UI is a web page, but the **server runs on your own machine** (`127.0.0.1`). Nothing goes through a third-party server — your links, your downloads, and any optional Bilibili credentials stay on your device.

> ⚠️ **Three platforms have prerequisites** — read [Platform prerequisites](#️-platform-prerequisites-important) first:
> **TikTok requires your own proxy in mainland China** (without one, nothing can be fetched);
> **Bilibili needs ffmpeg for a playable "finished" file** (bundled in the Windows build);
> **Xiaohongshu requires the full link from Share → Copy Link in the app.**

## 🎬 What it looks like

<div align="center">
<img src="./docs/screenshot-result-zh.png" width="880" alt="Parse result">
<br>
<sub>Paste a Douyin link → platform auto-detected, best available quality selected (720P H.265), download buttons ready</sub>
</div>

<br>

<div align="center">
<img src="./docs/screenshot-home-zh.png" width="880" alt="Initial UI">
<br>
<sub>Initial UI: switch platforms with the top bar, or just paste a link and let the app detect it</sub>
</div>

<div align="center">
<img src="./docs/screenshot-tiktok-photo-zh.png" width="880" alt="TikTok photo post result">
<br>
<sub>TikTok <b>photo post (Photo Mode)</b>: the whole set of watermark-free originals listed at once — download individually or zip them in one click</sub>
</div>

<sub>The UI is currently Chinese-only; this README is bilingual.</sub>

**"Watermark-free" is a real, measurable difference** — not marketing. Below: the same Xiaohongshu post. Left is the platform's playback stream, right is the original CDN link this tool resolves. Inside the yellow box (identical coordinates), the left has a platform badge and the right is clean:

<div align="center">
<img src="./docs/watermark-compare.png" width="620" alt="With watermark vs without">
</div>

<details>
<summary>How this comparison was verified</summary>

Both source frames are `540×960`. Counting bright pixels (RGB all > 225) in the bottom-right region (`x 461–524, y 911–941`):

| Frame | Bright pixels |
| --- | --- |
| Platform playback stream | **1241** (the "小红书" badge) |
| Original CDN link | **0** |

The originals are in the repository history and the scan is a few lines of PIL — reproduce it yourself.
</details>

## 📥 Download & install

Grab a build from the [**Releases**](../../releases/latest) page. Both bundles **ship their own Python runtime** — no environment setup needed.

### Windows (recommended)

| | |
| --- | --- |
| File | `multipldl-1.0.8-win64.zip` (≈ 77 MB, ≈ 166 MB unpacked) |
| Dependencies | **None** — Python 3.13 and ffmpeg are bundled |

1. **Extract the whole folder** (⚠️ do *not* drag the `.exe` out on its own — `_internal/` is part of it)
2. Double-click `多平台无水印下载站.exe`
3. Your browser opens `http://127.0.0.1:8787`. Closing the black console window stops the server.

> **Antivirus flags it?** PyInstaller-packed binaries are commonly false-positived by 360 / Huorong / Tencent PC Manager. Add the folder to your allow-list. **If that's not acceptable, don't use it — never disable your antivirus to run it.**

### Android

| | |
| --- | --- |
| File | `multipldl-1.0.8-arm64-v8a-debug.apk` (23.8 MB) |
| ABI | `arm64-v8a` (virtually every phone since 2017) |
| Signing | **debug-signed** — you must allow installs from unknown sources; no release signing yet |

Same UI as the desktop build (shared front-end) and the same parsing capability. On Android, Bilibili's DASH muxing uses the system `MediaMuxer`, so **ffmpeg is not required**.

### Run from source

```bash
git clone https://github.com/gdSHAY/douyin-wm-downloader.git
cd douyin-wm-downloader
pip install -r requirements.txt
python server.py
```

Then open `http://127.0.0.1:8787`. (`python server.py --no-browser` skips auto-opening the browser.)

If port 8787 is taken the server moves on to 8788 and so on — trust the address it prints.

## ✨ What you can get per platform

| Platform | Video | Photo set | Max quality | Also |
| --- | --- | --- | --- | --- |
| **Douyin** | ✅ watermark-free mp4 | ✅ originals | up to 1440P (**capped by the post itself**) | Live Photos (still + motion), cover, quality picker |
| **Bilibili** | ✅ DASH-muxed mp4 | — | 480P anonymous / up to 4K signed in | multi-part, MP3 audio, danmaku XML, cover, local-mux fallback |
| **TikTok** | ✅ watermark-free mp4 | ✅ watermark-free photos | **up to 4K 60fps** | photo posts (Photo Mode) with one-click zip, auto-picks by resolution → fps → bitrate, cover, original audio |
| **Xiaohongshu** | ✅ watermark-free mp4 | ✅ hi-res originals | originals / source video | Live Photos, download-all-as-ZIP |

Switch platforms with the top bar, or just paste a link and let the app detect it. Switching clears the input and result (so platforms never get mixed up); history is kept per platform.

## ⚙️ Three steps

1. **Pick a platform** (or paste a link and let it detect)
2. **Paste** — share short links, full web links, or the entire Chinese share text copied from the app all work
3. **Parse** → preview → download

**Accepted link formats**

| Platform | Accepted forms |
| --- | --- |
| Douyin | `https://v.douyin.com/xxxxx/`, `https://www.douyin.com/video/<id>`, `https://www.douyin.com/note/<id>`, or the whole share message |
| Bilibili | `https://www.bilibili.com/video/BV...`, `https://b23.tv/xxxxx`, including `?p=2` for multi-part |
| TikTok | `https://www.tiktok.com/@user/video/<id>`, `https://www.tiktok.com/@user/photo/<id>`, short links `https://www.tiktok.com/t/xxxxx/` and `https://vm.tiktok.com/xxxxx/` |
| Xiaohongshu | **must** be the full link from Share → Copy Link (contains `xsec_token`) — a bare note ID is rejected by the platform |

## ⚠️ Platform prerequisites (important)

These are **platform limitations, not tool limitations**, each with a recorded measurement.

### TikTok needs a proxy in mainland China

Measured: DNS resolves, but TCP 443 times out. **Without a proxy nothing can be fetched** — not "slow", not "flaky": guaranteed failure.

Open Settings → TikTok proxy (top-right), enter your local proxy (for Clash / FlClash that is usually `http://127.0.0.1:7890`), then click *Save & test* until it reports "TikTok reachable".

> On an **overseas server** this flips: direct connection works, leave the proxy empty.

### Bilibili needs ffmpeg for a playable file

Bilibili serves high quality as **separate DASH video and audio streams**. Delivering a single playable mp4 means muxing server-side, which needs ffmpeg.

- **The Windows build bundles ffmpeg** — works out of the box.
- Running from source: install ffmpeg and make sure it is on `PATH`, or set `FFMPEG_PATH`.
- **If ffmpeg is missing you never get a silent file by accident** — the UI degrades to "download video track / audio track separately" and prints the mux command.

### Bilibili quality depends on your account

| Quality | Requirement |
| --- | --- |
| 360P / 480P | no login (**measured ceiling while anonymous = 480P**) |
| 720P / 1080P / 1080P60 / 4K | requires login (`SESSDATA`) |

Click **⚙ Account settings** (top-right) and paste either the full exported `cookies.txt` or `SESSDATA=xxx`. The value is validated against Bilibili *before* being written, so a truncated cookie cannot clobber a working config.

> 🔒 `SESSDATA` is equivalent to your account session. It is only used to request the qualities your account may watch, and is **never transmitted anywhere else**.
> The settings panel only ever returns a masked value (like `3bee…IIEC`) plus a length; no endpoint returns the plaintext.
> That is also why `.gitignore` excludes `bili_config.json` — **never commit it**.

**Measured clarification (2026-09)**: using an ordinary account whose membership had long expired, the same video yielded 6 qualities including 4K / 1080P60 / 1080P / 720P; anonymously the same link gave only 480P + 360P. So "4K needs Premium" is **not true**. This tool trusts the tracks actually present in `dash.video` and ignores the `vipStatus` field.

### Xiaohongshu requires `xsec_token`

A bare note ID is rejected by the platform (measured: "the page you are looking for is gone", empty embedded data). **Always use Share → Copy Link from the app.**

### Douyin: "why is there no higher quality?"

Two causes, check in order:

1. **The post's own ceiling.** A large share of Douyin portrait videos are 720P; only some newer posts have 1080P / 2K / 4K. The quality list comes from the platform's own response — if the source is 720p, no tool can invent 4K.
2. **The fetch channel was risk-controlled down a tier.** Douyin has two paths: the **signed HD channel** (multiple selectable qualities) and the **unsigned fallback channel** (a single "default"). The UI labels which one you got. Datacenter IPs — especially overseas ones — are downgraded more often.

The fallback channel still yields a **watermark-free** video, just with no quality choice. For the specific reason, open DevTools and read `hd_error` in the `/api/parse` response — the backend states plainly whether it was "empty response (possibly rate-limited)" or a network timeout.

## 🧱 Built with

| | |
| --- | --- |
| Backend | Python + [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn (single local process) |
| Front-end | one plain HTML/JS file, **no build step**, port injected by the backend at render time |
| Networking | `requests`, `curl_cffi` (Xiaohongshu verifies TLS fingerprints), `gmssl` (Douyin signing) |
| Media | ffmpeg (Bilibili muxing, bundled on Windows) / Android `MediaMuxer` |
| Packaging | PyInstaller (Windows) / Buildozer + python-for-android (Android, built in CI) |

## 🗂 Project layout

```
server.py              FastAPI backend: routes, parse orchestration, download proxy, saving
static/index.html      Front-end (single file, no build)
douyin_parser.py       Douyin parsing (incl. risk-control downgrade detection)
douyin_hd.py           Douyin HD channel: quality selection
douyin_abogus.py       Douyin a_bogus signature
bili_parser.py         Bilibili parsing (qualities, parts, danmaku)
dash_muxer.py          Bilibili DASH muxing (drives ffmpeg)
tiktok_parser.py       TikTok parsing (video qualities + photo posts + CDN retry)
xhs_parser.py          Xiaohongshu parsing (originals / Live Photos)
disk_saver.py          Server-side saving: job queue, progress, cancel
runtime_paths.py       Path helper (source vs exe vs APK runtime shapes)
requirements.txt       Dependency list
使用说明.txt            Quick-start sheet shipped inside the ZIP
docs/                  Screenshots used by this README
```

## 🔌 REST API

The UI is optional — the server is a plain HTTP API. Once `python server.py` is running, call it directly.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/parse` | Parse a link: title, cover, qualities, direct URLs |
| `GET` | `/api/download` | Proxied download (adds Referer / UA to avoid 403) |
| `GET` | `/api/bili/tracks` | Available Bilibili video/audio tracks and qualities |
| `GET` | `/api/bili/download` | Bilibili muxed output (requires ffmpeg) |
| `GET` | `/api/bili/danmaku` | Danmaku XML |
| `POST` | `/api/save` | Server-side download to disk (with progress) |
| `GET` | `/api/proxy` | Current TikTok proxy probe result |

Interactive docs: open `http://127.0.0.1:8787/docs` while the server is running.

## ❓ FAQ

<details>
<summary><b>The window flashes and closes / the browser never opens</b></summary>

Do not drag the `.exe` out on its own — it **must sit next to `_internal/`**. Also check the address printed in the window: if 8787 is busy it moves to 8788 and up.
</details>

<details>
<summary><b>Bilibili downloads have no audio</b></summary>

ffmpeg was not found, so you received a separated video track. Use the **Windows ZIP build** (ffmpeg bundled), or install ffmpeg / set `FFMPEG_PATH` when running from source. The UI says "video track / audio track separately" in that case.
</details>

<details>
<summary><b>TikTok parses fine but every download is 403 / 502</b></summary>

Read the status code and the parenthesised note in the error — it states which path was used:

- **403 with `webapp-prime` in the host** → that media host was rejected by a TikTok edge node. Recent builds automatically retry the other direct URLs at the same quality; if it still fails, the current proxy node is blocked — switch nodes.
- **502 with `tiktokcdn-us.com` / `tiktokv.com`** → the host is fine, your proxy failed to forward. Make sure its routing rules cover TikTok's CDN domains, or temporarily switch to global mode.
- **The note says "direct"** → the proxy never took effect. Go to Settings → TikTok proxy and click *Save & test*.
</details>

<details>
<summary><b>Are my settings kept between runs?</b></summary>

Yes. They live in `data/` next to the program (or `bili_config.json` / `tiktok_config.json` when running from source). Don't delete them — copy the whole folder to another machine and your settings come along.
</details>

<details>
<summary><b>Downloads are slow</b></summary>

Top quality can be several hundred MB — that is normal. Pick one tier lower in the quality dropdown to speed things up considerably.
</details>

## 📄 Legal & disclaimer

- Copyright of all videos and images belongs to **the original creators and the platforms**.
- Use this for **personal study and technical exchange only**; not for commercial use or infringement. Delete downloaded content within **24 hours**.
- Downloading or redistributing others' work may violate platform terms of service and applicable law. **You bear the risk.**
- This tool circumvents no paywall: what you can fetch is determined by the platform and your account's entitlements.

## 📮 Contact

| | |
| --- | --- |
| Bug reports | [Issues](../../issues) |
| Repository | <https://github.com/gdSHAY/douyin-wm-downloader> |

## ⭐ Star history

<a href="https://star-history.com/#gdSHAY/douyin-wm-downloader&Timeline">
  <img src="https://api.star-history.com/svg?repos=gdSHAY/douyin-wm-downloader&type=Timeline" alt="Star History" width="620">
</a>

## 📄 License

This repository ships **no open-source license** (all rights reserved). You are free to download and use the released builds; to reuse the code in another project or commercially, contact the author via Issues first.

<div align="center">
<sub>This README is bilingual — switch with the links at the top: <a href="./README.md">简体中文</a> / English.</sub>
</div>
