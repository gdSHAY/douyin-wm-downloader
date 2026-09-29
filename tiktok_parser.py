# -*- coding: utf-8 -*-
"""TikTok 解析：直连网页端接口，不需要 X-Bogus / msToken 之类签名。

本模块的每条结论都来自 2026-09-20 经本机代理（http://127.0.0.1:7890）的真实实测：

1. **短链解析**：`https://www.tiktok.com/t/XXXX/` → 301 →
   `https://www.tiktok.com/@user/video/<19位id>`。`vm./vt.tiktok.com` 同理。

2. **两个数据源，各有取舍**：
   - `/player/api/v1/items?item_ids=<id>&aid=1988` → `items[0].video_info.profiles[]`
     结构最规整，每条含 `gear_name / bitrate / fps / codec_type / hdr_type` 与
     `play_addr{width,height,data_size,url_list[3]}`。**唯一直接给出 fps 的接口**。
   - 视频页内嵌的 `__UNIVERSAL_DATA_FOR_REHYDRATION__` →
     `webapp.video-detail.itemInfo.itemStruct`，含 `video.bitrateInfo[]`
     （`GearName / Bitrate / BitrateFPS / CodecType / PlayAddr`），另有 desc、
     作者、音乐、播放量等展示用数据。
   本模块两个都取并合并：档位以 API 的 `profiles` 为准，展示信息以网页为准。

3. **一条视频给 3 条直链，选错那条是 403 的真正成因（2026-09-26 实测）**：

   `play_addr.url_list` 里是 3 条**不同主机**的直链。同一台机器、同一个出口、
   同一套请求头逐一实测：

   | 档位 | url_list[0] | url_list[1] | url_list[2] |
   | --- | --- | --- | --- |
   | h265（HEVC） | `v16-webapp-prime.us.tiktok.com` **403** | `v19-webapp-prime.us.tiktok.com` **403** | `www.tiktok.com` **200** → 302 到 `v16m-default.tiktokcdn-us.com` |
   | h264 | `v45.tiktokcdn-us.com` **200** | `v19.tiktokcdn-us.com` **200** | `api16-normal-useast8.tiktokv.us` **200** |

   - Akamai 的 `webapp-prime` 主机对我们这类请求**一律 403**，响应是
     `AkamaiGHost` 的 `Access Denied`（`X-Cache: TCP_DENIED`）。
   - **请求头换什么都没用**：不带任何头 / 只带 Referer / 只带 UA /
     全带（UA+Referer+Origin+Accept）/ 加 `Range` —— 六种组合全是 403。
     所以这**不是防盗链问题**，是选错了直链。
   - 而 `www.tiktok.com` 那条会 302 到可用的 `*.tiktokcdn-us.com`。

   用户界面默认选中的是最高画质，最高画质恰好是 h265 —— 于是「能解析、下不动」
   稳定复现。修法见 `_rank_urls()`：把 webapp-prime 主机排到最后，
   其余保持原顺序；同时登记备胎，取流层遇到 403/404 还能自动换下一条。

   历史备注：早期版本（只有 h264 单档位）实测过「不带 Referer → 403，
   带 `Referer: https://www.tiktok.com/` → 206」。那是
   `*.tiktokcdn-us.com` 主机的行为，与 webapp-prime 的 403 是两回事 ——
   两者都存在，Referer 仍要带，但它救不了选错主机。

4. **必须显式带桌面 Chrome UA**：yt-dlp 用默认 UA 会栽在 TikTok 的 JS challenge 上
   （报 `Unexpected response from webpage request`），换成 Chrome UA 就正常。
   但 yt-dlp 拿不到 fps（`fps=None`），所以「最高帧率」的排序不能依赖它 ——
   本模块把它降级为兜底路径。

5. **网页端的档位可能只有 1 个**：实测目标视频只有 `original_2160_0`
   （2160x3840 / **60fps** / 32 Mbps / 48,160,740 字节），
   且 `video.playAddr` 与 `video.downloadAddr` 完全相同。界面因此必须能
   优雅呈现「只有一档」的情况，不能假设一定有多档可选。

6. **TikTok 在中国大陆不可达**：DNS 能解析（157.240.7.20）但 TCP 443 直接超时。
   故本模块内置代理解析（显式配置 > 环境变量 > 自动探测常见本地端口），
   不可达时给出可操作的中文提示，而不是抛一个 requests 超时。
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import runtime_paths

WEB = "https://www.tiktok.com"
ITEMS_API = WEB + "/player/api/v1/items"

# 实测：用桌面 Chrome UA 才能稳定拿到网页数据（默认 UA 会被挑战）
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WEB_HEADERS = {
    "User-Agent": UA,
    "Referer": WEB + "/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}

CONFIG_FILENAME = "tiktok_config.json"
# 打包成 exe 后必须落到**可写目录**，否则用户设的代理一重启就没了（详见 runtime_paths.py）。
CONFIG_PATH = runtime_paths.config_path(CONFIG_FILENAME)

# 代理优先级：显式配置 > 这些环境变量 > 自动探测
PROXY_ENV_KEYS = ("TIKTOK_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

# 自动探测用的常见本地代理端口（7890=Clash 系默认；7897=Clash Verge；10809=v2rayN）
COMMON_PROXY_PORTS = (7890, 7897, 7891, 10809, 10808, 2080, 1080, 8889, 8080)

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef<>\"'，。！？、；：]+", re.I)
VIDEO_ID_RE = re.compile(r"/(?:video|photo)/(\d{10,25})")
SHORT_HOST_RE = re.compile(r"^https?://(?:www\.)?(?:vm|vt|m)\.tiktok\.com/", re.I)
BARE_ID_RE = re.compile(r"^\d{15,25}$")

_lock = threading.Lock()
# 代理解析结果缓存（避免每次解析都去探测端口）
_proxy_cache: Dict[str, Any] = {"value": None, "source": "", "checked_at": 0.0}
# 按代理复用的 Session（保留 ttwid，减少请求数）
_sessions: Dict[str, requests.Session] = {}


class TikTokError(Exception):
    """带可操作中文提示的解析错误。"""


# ---------------------------------------------------------------------------
# 代理解析
# ---------------------------------------------------------------------------


def _normalize_proxy(value: str) -> str:
    """规整代理串：补协议头、去引号与空白。"""
    raw = (value or "").strip().strip('"').strip("'").strip()
    if not raw:
        return ""
    if "://" not in raw:
        # 只给了 host:port 时按 http 处理（Clash 的混合端口同时吃 http/https）
        raw = "http://" + raw
    return raw


def _read_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(data: Dict[str, Any]) -> None:
    """把代理配置落盘。代理留空即回到「自动探测」。"""
    payload = {
        "_说明": "TikTok 代理配置；proxy 留空则自动探测常见本地代理端口。",
        "_示例": "http://127.0.0.1:7890",
        "proxy": _normalize_proxy(str(data.get("proxy") or "")),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _socks_supported() -> bool:
    import importlib.util

    return importlib.util.find_spec("socks") is not None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_reaches_tiktok(proxy: str, timeout: float = 12.0) -> Tuple[bool, str]:
    """探一次 TikTok，确认这个代理真的能用（端口开着不等于能出网）。"""
    proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.get(
            WEB + "/", headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            proxies=proxies, timeout=timeout,
        )
        if resp.status_code == 200:
            return True, ""
        return False, f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return False, type(exc).__name__


def _proxy_candidates() -> List[Tuple[str, str]]:
    """按优先级列出候选代理：配置文件 > 环境变量 > 常见本地端口。"""
    items: List[Tuple[str, str]] = []

    def add(value: str, source: str) -> None:
        normalized = _normalize_proxy(value)
        if normalized and all(normalized != seen for seen, _ in items):
            items.append((normalized, source))

    add(str(_read_config().get("proxy") or ""), "config")
    for key in PROXY_ENV_KEYS:
        add(str(os.environ.get(key) or ""), f"env:{key}")
    for port in COMMON_PROXY_PORTS:
        add(f"http://127.0.0.1:{port}", f"auto:{port}")
    return items


def resolve_proxy(force: bool = False) -> Tuple[str, str]:
    """返回 (proxy, source)。

    source 取值：config / env:<名> / auto:<端口> / none。

    候选按优先级**逐个实测**后返回第一个真能连上 TikTok 的 —— 这一步很关键：
    「端口开着」和「环境变量里有个代理」都不等于「这个代理能出网」。
    例如某些 IDE / 沙箱会注入 HTTP_PROXY 指向一个连不了 TikTok 的代理，
    若盲信环境变量就会把一个必然失败的代理交给解析流程。

    一个候选都不通时，仍返回优先级最高的那个，让报错信息指向用户本来就想用的代理。
    结果缓存 60 秒，避免每次解析都做端口探测。
    """
    with _lock:
        if not force and _proxy_cache["source"] and (time.time() - _proxy_cache["checked_at"] < 60):
            return _proxy_cache["value"], _proxy_cache["source"]

        candidates = _proxy_candidates()
        value, source = "", "none"

        for candidate, candidate_source in candidates:
            if candidate_source.startswith("auto:"):
                # 端口没开着就没必要浪费一次网络探测
                try:
                    port = int(candidate.rsplit(":", 1)[1])
                except ValueError:
                    continue
                if not _port_open(port):
                    continue
            ok, _ = _proxy_reaches_tiktok(candidate, timeout=8.0)
            if ok:
                value, source = candidate, candidate_source
                break

        if not value and candidates:
            # 全部不通时，只回退到**用户显式配置过**的代理（config / env），
            # 让报错指向「你配的那个代理连不上」。自动探测出来的端口不通就不回退了 ——
            # 返回空代理会走直连 —— 报错会变成「连不上 TikTok（…）+ 分情形建议」，
            # 对用户更准确、也更好排查（境外部署时直连本来就该成功，见 _offline_hint）。
            first_value, first_source = candidates[0]
            if not first_source.startswith("auto:"):
                value, source = first_value, first_source

        _proxy_cache.update({"value": value, "source": source, "checked_at": time.time()})
        return value, source


def proxy_status(probe: bool = False) -> Dict[str, Any]:
    """给前端看的代理状态摘要（不含任何敏感信息）。"""
    value, source = resolve_proxy()
    info: Dict[str, Any] = {
        "configured": bool(value),
        "source": source,
        "value": value,
        "from_config": source == "config",
        "from_env": source.startswith("env:"),
        "socks_supported": _socks_supported(),
        "candidates": [p for p in COMMON_PROXY_PORTS if _port_open(p)],
    }
    if probe:
        if value:
            ok, detail = _proxy_reaches_tiktok(value)
            info["reachable"] = ok
            info["detail"] = detail
        else:
            # 没代理时探直连，用于区分「墙」与「网络故障」
            try:
                resp = requests.get(WEB + "/", headers={"User-Agent": UA}, timeout=8)
                info["reachable"] = resp.status_code == 200
                info["detail"] = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                info["reachable"] = False
                info["detail"] = type(exc).__name__
    return info


def current_proxy() -> Tuple[str, str]:
    """取「当前已知可用」的代理，缓存失效时**不重新探测**。

    媒体取流（/api/proxy、/api/download）走这个：这些请求很频繁，
    不该为了确认代理可用而额外挂一次网络探测。缓存为空（刚启动还没解析过）
    时才退回完整解析流程。
    """
    with _lock:
        if _proxy_cache["value"]:
            return _proxy_cache["value"], _proxy_cache["source"]
    return resolve_proxy()


def reset_session() -> None:
    """代理配置变更后丢弃缓存的 Session。"""
    with _lock:
        for session in _sessions.values():
            try:
                session.close()
            except Exception:
                pass
        _sessions.clear()
        _proxy_cache.update({"value": None, "source": "", "checked_at": 0.0})


def _session(proxy: str) -> requests.Session:
    """按代理复用 Session（复用可保留 ttwid，降低被风控概率）。

    ★ 必须关掉 trust_env（2026-09-20 实测踩坑）：
    requests 2.34.x 的 ``Session.request`` 只把**调用处传入的** proxies 交给
    ``merge_environment_settings``，并不会合并 ``session.proxies``。于是当系统里存在
    ``HTTP_PROXY`` / ``HTTPS_PROXY``（很多 IDE、沙箱、企业环境都会注入）时，环境代理会
    直接顶掉这里显式设置的代理 —— 表现为「设置面板里代理可用，一解析就 ProxyError」。
    ``trust_env = False`` 让本会话只认自己这一份代理配置；环境变量仍由
    ``resolve_proxy`` 显式纳入候选，不存在「配置了却没用上」的情况。
    """
    with _lock:
        cached = _sessions.get(proxy)
        if cached is not None:
            return cached
        session = requests.Session()
        session.trust_env = False
        session.headers.update(WEB_HEADERS)
        if proxy:
            if proxy.lower().startswith("socks") and not _socks_supported():
                raise TikTokError(
                    f"代理 {proxy} 是 SOCKS 协议，但当前 Python 环境没装 PySocks，无法使用。"
                    "两个选择：① 改用 HTTP 代理（如 Clash 的 7890 混合端口，写法 http://127.0.0.1:7890）；"
                    "② 执行 pip install requests[socks] 后重试。"
                )
            session.proxies = {"http": proxy, "https": proxy}
        _sessions[proxy] = session
        return session


def _offline_hint(detail: str) -> str:
    """连不上 TikTok 时的可操作提示。

    ★ 不要写成「必须走代理」（2026-09-30 改）：
    部署到 Render（境外机房）后确认，那种环境下**直连就能通**，代理反而是多余的 ——
    `resolve_proxy()` 探不到候选代理时返回空串，取流层就走真直连
    （见本模块 `_session` 关于 trust_env 的说明）。所以这里按「本工具跑在哪」
    分两种情形给建议，而不是一口咬定必须挂代理。
    """
    return (
        f"连不上 TikTok（{detail}）。TikTok 官方接口在中国大陆的网络里无法直连"
        "（DNS 能解析出 IP、但 TCP 443 超时），要不要代理取决于本工具跑在哪："
        "① 跑在你自己电脑上（大陆网络）—— 必须走代理：打开代理客户端"
        "（FlClash / Clash 的混合端口通常是 7890，本工具会自动探测并实测它能否连上），"
        "或在页面「⚙ 设置 → TikTok 代理」手填 http://127.0.0.1:7890，"
        "或设置环境变量 TIKTOK_PROXY；"
        "② 部署在境外服务器（如 Render / 海外 VPS）—— 直连即可，代理留空。"
        "若仍报这个错，说明该机房出口被 TikTok 拦了：换个机房区域，或填一个公网可达的代理；"
        "③ 两种情形都不成立时，可能是这条视频已删除或仅限特定地区，换一条链接试试。"
    )


# ---------------------------------------------------------------------------
# 链接解析
# ---------------------------------------------------------------------------


def is_tiktok_text(text: str) -> bool:
    return bool(re.search(r"tiktok\.com", text or "", re.I))


def extract_url(text: str) -> str:
    """从任意文本里揪出 TikTok 链接（支持混了中文口令的分享文案）。"""
    raw = (text or "").strip()
    if not raw:
        raise TikTokError("请先粘贴 TikTok 分享链接")
    match = URL_RE.search(raw)
    if not match:
        if BARE_ID_RE.match(raw):
            return raw  # 允许直接粘视频 id
        raise TikTokError("没找到 TikTok 链接，请粘贴 https://www.tiktok.com/... 或 vm.tiktok.com 短链")
    return match.group(0).rstrip(".,;)")


def extract_video_id(url: str) -> str:
    if BARE_ID_RE.match(url or ""):
        return url
    match = VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else ""


def resolve_url(url: str, proxy: str) -> Tuple[str, str]:
    """跟随短链跳转，返回 (最终地址, 视频id)。"""
    session = _session(proxy)
    if extract_video_id(url) and not SHORT_HOST_RE.search(url):
        return url.split("?")[0], extract_video_id(url)

    try:
        resp = session.get(url, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        raise TikTokError(_offline_hint(type(exc).__name__)) from exc

    final = resp.url or url
    video_id = extract_video_id(final)
    if not video_id:
        # 有些短链最终落到 /t/xxx 的落地页，再从 HTML 里捞一次
        found = VIDEO_ID_RE.search(resp.text or "")
        if found:
            video_id = found.group(1)
        else:
            raise TikTokError(
                f"没能从链接里解析出视频 id（最终地址：{final[:120]}）。"
                "可能是私密作品、已删除，或链接不是视频帖。"
            )
    return final.split("?")[0], video_id


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------


def _fetch_web_meta(session: requests.Session, video_id: str, proxy: str) -> Dict[str, Any]:
    """取视频页内嵌数据（顺带种下 ttwid cookie）。"""
    url = f"{WEB}/@i/video/{video_id}"
    try:
        resp = session.get(url, timeout=40)
    except requests.RequestException as exc:
        raise TikTokError(_offline_hint(type(exc).__name__)) from exc

    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', resp.text or "", re.S
    )
    if not match:
        return {}
    try:
        scope = json.loads(match.group(1)).get("__DEFAULT_SCOPE__") or {}
    except ValueError:
        return {}
    detail = scope.get("webapp.video-detail") or {}
    item = (detail.get("itemInfo") or {}).get("itemStruct") or {}
    if not item:
        code = detail.get("statusCode")
        msg = detail.get("statusMsg") or ""
        if code:
            raise TikTokError(f"TikTok 未返回该作品数据（statusCode={code} {msg}）。可能已删除、私密或区域受限。")
        return {}
    return item


def _fetch_items_api(session: requests.Session, video_id: str) -> Dict[str, Any]:
    """取 /player/api/v1/items —— 唯一直接给出 fps 的接口。"""
    try:
        resp = session.get(
            ITEMS_API,
            params={"item_ids": video_id, "aid": "1988"},
            headers={"Accept": "application/json, text/plain, */*"},
            timeout=40,
        )
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        items = (resp.json() or {}).get("items") or []
    except ValueError:
        return {}
    return items[0] if items else {}


# ---------------------------------------------------------------------------
# 档位构建
# ---------------------------------------------------------------------------


def _resolution_tag(height: int) -> str:
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    if height >= 1080:
        return "1080P"
    if height >= 720:
        return "720P"
    if height >= 540:
        return "540P"
    if height >= 480:
        return "480P"
    if height >= 360:
        return "360P"
    return f"{height}P" if height else "未知"


CODEC_NAMES = {
    "h264": "H.264",
    "avc1": "H.264",
    "hevc": "H.265",
    "h265": "H.265",
    "bytevc1": "H.265",
    "av01": "AV1",
}


def _quality_label(height: int, width: int, fps: Any, codec: str) -> str:
    parts = []
    if height and width:
        parts.append(f"{_resolution_tag(height)} {width}×{height}")
    elif height:
        parts.append(_resolution_tag(height))
    if fps:
        parts.append(f"{fps}fps")
    if codec:
        parts.append(CODEC_NAMES.get(str(codec).lower(), str(codec)))
    return " · ".join(parts) or "默认档位"


#: 实测恒被拒的主机特征（见模块文档第 3 条）：Akamai 的 webapp-prime 边缘
#: 对这类请求返回 `Access Denied`，与我们带什么请求头无关。
_BAD_HOST_MARKERS = ("webapp-prime",)

#: 「这条直链不行时还能换成哪几条」——解析时登记，取流层 403/404 时取用。
#: 只在当前进程内有效：App 的真实顺序是「先解析、再下载」，解析完这张表就是热的。
#: 上限按「一次会话解析几十条视频」留足余量（每条视频约 3 个键）。
_alts_lock = threading.Lock()
_ALTS: "OrderedDict[str, List[str]]" = OrderedDict()
ALTS_LIMIT = 240


def _url_rank(url: str) -> int:
    """直链优先级：0 = 实测可用，1 = 实测常被拒。"""
    host = (urlparse(url).hostname or "").lower()
    return 1 if any(marker in host for marker in _BAD_HOST_MARKERS) else 0


def _remember_alternatives(urls: List[str]) -> None:
    """登记一组直链的「互为备胎」关系。

    取**并集**而不是覆盖：同一台主机上的直链可能在多个档位里重复出现，
    后一次登记若直接覆盖，会把先前已知的可用候选挤掉 —— 那正是「本来能
    换链成功、却因为候选被挤掉而失败」的隐患。
    """
    if len(urls) < 2:
        return
    with _alts_lock:
        for url in urls:
            merged = list(_ALTS.get(url) or [])
            for other in urls:
                if other != url and other not in merged:
                    merged.append(other)
            _ALTS[url] = merged
            _ALTS.move_to_end(url)
        while len(_ALTS) > ALTS_LIMIT:
            _ALTS.popitem(last=False)


def alternatives(url: str) -> List[str]:
    """同一条视频、同一个档位的**其余**直链；没登记过就返回空表。

    给服务端取流层用：某条直链 403/404 时，换成同组的另一条往往就通了
    （实测 webapp-prime 被拒，而同组的 `www.tiktok.com` 会 302 到可用 CDN）。
    """
    with _alts_lock:
        return list(_ALTS.get(url) or [])


def _rank_urls(urls: List[str]) -> List[str]:
    """排序 + 登记备胎，返回可直接使用的直链表（第 0 条即首选）。"""
    if not urls:
        return []
    # sorted 稳定：同为「可用」的几条保持 TikTok 给的原始顺序
    ordered = sorted(urls, key=_url_rank)
    _remember_alternatives(ordered)
    return ordered


def _from_profiles(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in profiles or []:
        play = entry.get("play_addr") or {}
        urls = _rank_urls([u for u in (play.get("url_list") or []) if u])
        if not urls:
            continue
        height = int(play.get("height") or 0)
        width = int(play.get("width") or 0)
        fps = entry.get("fps")
        codec = str(entry.get("codec_type") or "")
        out.append(
            {
                "gear": entry.get("gear_name") or "",
                "label": _quality_label(height, width, fps, codec),
                "width": width,
                "height": height,
                "fps": int(fps) if fps else 0,
                "codec": codec,
                "hdr": str(entry.get("hdr_type") or ""),
                "bitrate": int(entry.get("bitrate") or 0),
                "size": int(play.get("data_size") or 0),
                "url": urls[0],
                "urls": urls,
                "ext": "mp4",
                "has_audio": True,  # TikTok 网页播放源是音视频合一的渐进式 mp4
            }
        )
    return out


def _from_bitrate_info(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in entries or []:
        play = entry.get("PlayAddr") or {}
        urls = _rank_urls([u for u in (play.get("UrlList") or []) if u])
        if not urls:
            continue
        height = int(play.get("Height") or 0)
        width = int(play.get("Width") or 0)
        fps = entry.get("BitrateFPS")
        codec = str(entry.get("CodecType") or "")
        out.append(
            {
                "gear": entry.get("GearName") or "",
                "label": _quality_label(height, width, fps, codec),
                "width": width,
                "height": height,
                "fps": int(fps) if fps else 0,
                "codec": codec,
                "hdr": "",
                "bitrate": int(entry.get("Bitrate") or 0),
                "size": int(play.get("DataSize") or 0),
                "url": urls[0],
                "urls": urls,
                "ext": "mp4",
                "has_audio": True,
            }
        )
    return out


def _sort_key(item: Dict[str, Any]) -> Tuple[int, int, int]:
    """排序口径：分辨率 → 帧率 → 码率，全部降序。"""
    return (item.get("height") or 0, item.get("fps") or 0, item.get("bitrate") or 0)


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一 gear 可能因多 CDN 镜像重复出现，按 (gear,bitrate,height) 去重。"""
    seen = set()
    out = []
    for item in items:
        key = (item.get("gear") or "", item.get("bitrate") or 0, item.get("height") or 0, item.get("codec") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 展示数据
# ---------------------------------------------------------------------------


def _author_of(item: Dict[str, Any], api_item: Dict[str, Any]) -> Dict[str, str]:
    author = item.get("author") or (api_item.get("author_info") or {})
    return {
        "name": str(author.get("nickname") or ""),
        "unique_id": str(author.get("uniqueId") or author.get("unique_id") or ""),
    }


def _cover_of(video: Dict[str, Any], api_item: Dict[str, Any]) -> str:
    cover = video.get("cover")
    if isinstance(cover, str) and cover:
        return cover
    api_cover = (api_item.get("video_info") or {}).get("cover") or {}
    urls = api_cover.get("url_list") or []
    return urls[0] if urls else ""


def _duration_of(video: Dict[str, Any], api_item: Dict[str, Any]) -> int:
    """网页端给秒，API 给毫秒，统一成秒。"""
    api_meta = (api_item.get("video_info") or {}).get("meta") or {}
    ms = api_meta.get("duration")
    if ms:
        try:
            value = int(ms)
            return value // 1000 if value > 1000 else value
        except (TypeError, ValueError):
            pass
    try:
        return int(video.get("duration") or 0)
    except (TypeError, ValueError):
        return 0


def _stats_of(item: Dict[str, Any], api_item: Dict[str, Any]) -> Dict[str, int]:
    raw = item.get("stats") or {}
    api = api_item.get("statistics_info") or {}

    def pick(*names: str) -> int:
        for source in (raw, api):
            for name in names:
                value = source.get(name)
                if value not in (None, ""):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
        return 0

    return {
        "play": pick("playCount", "play_count"),
        "digg": pick("diggCount", "digg_count"),
        "comment": pick("commentCount", "comment_count"),
        "share": pick("shareCount", "share_count"),
        "collect": pick("collectCount", "collect_count"),
    }


def _safe_name(value: str, fallback: str = "tiktok") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t#]', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:70] or fallback


# ---------------------------------------------------------------------------
# 兜底路径：yt-dlp
#
# 为什么它不是主路径（2026-09-20 实测，6 组选项）：
#   1. 默认 UA 下 5/6 组直接失败，报 `Unexpected response from webpage request`
#      —— 挂在 TikTok 的 JS challenge 上（yt_dlp/extractor/tiktok.py:231）；
#      只有显式传 `http_headers.User-Agent = 桌面 Chrome` 那一组成功。
#   2. 成功那组也拿不到 fps（`fps=None`），而「最高帧率」正是本需求的核心，
#      所以排序不能依赖它。
#   3. 封面/网页两者的 profiles 接口本身已能给出 fps，且零额外依赖。
# 保留它作为兜底：主路径拿不到档位时（如网页结构再变、或某些图文帖）多一条路。
# 未安装 yt-dlp 时整条路径静默跳过，不影响主流程。
# ---------------------------------------------------------------------------


def _ytdlp_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("yt_dlp") is not None


def _ytdlp_formats(url: str, proxy: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """用 yt-dlp 兜底取档位，返回 (qualities, 展示信息)。

    这条路径给不出 fps，所以 label 里不写帧率，排序退化为「分辨率 → 码率」。
    """
    if not _ytdlp_available():
        return [], {}
    try:
        import yt_dlp
    except Exception:
        return [], {}

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # 关键：不显式给桌面 UA 会栽在 JS challenge 上（见上方实测记录）
        "http_headers": {"User-Agent": UA, "Referer": WEB + "/"},
    }
    if proxy:
        opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return [], {}

    if not isinstance(info, dict):
        return [], {}
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}

    out: List[Dict[str, Any]] = []
    for fmt in info.get("formats") or []:
        if (fmt.get("vcodec") or "none") == "none":
            continue  # 纯音频轨单列不要
        fmt_url = fmt.get("url")
        if not fmt_url:
            continue
        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        codec = str(fmt.get("vcodec") or "").split(".")[0]
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        out.append(
            {
                "gear": f"ytdlp:{fmt.get('format_id')}",
                "label": _quality_label(height, width, fmt.get("fps"), codec),
                "width": width,
                "height": height,
                "fps": int(fmt.get("fps") or 0),
                "codec": codec,
                "hdr": "",
                "bitrate": int((fmt.get("tbr") or 0) * 1000),
                "size": int(size or 0),
                "url": fmt_url,
                "urls": [fmt_url],
                "ext": str(fmt.get("ext") or "mp4"),
                "has_audio": (fmt.get("acodec") or "none") != "none",
            }
        )

    meta = {
        "title": info.get("title") or "",
        "author": {
            "name": info.get("uploader") or "",
            "unique_id": info.get("uploader_id") or "",
        },
        "cover": info.get("thumbnail") or "",
        "duration": int(info.get("duration") or 0),
    }
    return out, meta


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse(text: str) -> Dict[str, Any]:
    """解析一条 TikTok 链接，返回统一结构（与 bili_parser.parse 对齐）。"""
    url = extract_url(text)
    proxy, proxy_source = resolve_proxy()

    final_url, video_id = resolve_url(url, proxy)
    session = _session(proxy)

    # 两个数据源各自容错：任一可用即可出结果
    web_error = ""
    try:
        item = _fetch_web_meta(session, video_id, proxy)
    except TikTokError as exc:
        item, web_error = {}, str(exc)
    api_item = _fetch_items_api(session, video_id)

    if not item and not api_item:
        if web_error:
            raise TikTokError(web_error)
        raise TikTokError(
            f"没能取到该作品的数据（id={video_id}）。可能是私密作品、已被删除，"
            "或当前代理节点被 TikTok 风控；换一个节点后重试通常可解。"
        )

    video = item.get("video") or {}
    api_video = api_item.get("video_info") or {}

    # 图文帖（Photo Mode）走的是另一套结构，本工具暂不处理 —— 明确告知而非静默出错
    images = ((item.get("imagePost") or {}).get("images")) or []
    if images:
        raise TikTokError(
            f"这是一条图文帖（Photo Mode，{len(images)} 张图），本工具目前只处理视频帖。"
            "如需支持图文，告诉我一声即可加上。"
        )

    qualities = _dedupe(
        _from_profiles(api_video.get("profiles") or []) + _from_bitrate_info(video.get("bitrateInfo") or [])
    )
    source = "web"
    yt_meta: Dict[str, Any] = {}

    # 主路径拿不到档位时，退回 yt-dlp（代价：丢 fps，只按 分辨率→码率 排）
    if not qualities:
        qualities, yt_meta = _ytdlp_formats(final_url, proxy)
        qualities = _dedupe(qualities)
        if qualities:
            source = "ytdlp"

    qualities.sort(key=_sort_key, reverse=True)

    if not qualities:
        raise TikTokError(
            "该作品没有返回任何可下载的视频轨。可能是纯图文帖、直播回放，或 TikTok 限制了该地区访问。"
        )

    best = qualities[0]
    description = str(item.get("desc") or api_item.get("desc") or yt_meta.get("title") or "").strip()
    author = _author_of(item, api_item)
    if not (author.get("name") or author.get("unique_id")) and yt_meta.get("author"):
        author = yt_meta["author"]
    music = (api_item.get("music_info") or {})
    web_music = item.get("music") or {}

    hint = (
        f"已自动选取最高画质：{best['label']}"
        if len(qualities) > 1
        else f"TikTok 网页端对该视频只提供 1 个档位：{best['label']}（这就是它能给到的最高画质）"
    )
    if source == "ytdlp":
        hint += " · 本次由 yt-dlp 兜底解析（该路径取不到帧率，排序退化为分辨率→码率）"
    if proxy_source.startswith("auto:"):
        hint += f" · 代理自动探测为 {proxy}"

    filename = _safe_name(f"{author.get('name') or author.get('unique_id') or 'tiktok'}-{description[:30]}")
    if not filename or filename == "tiktok":
        filename = f"tiktok_{video_id}"

    duration = _duration_of(video, api_item) or int(yt_meta.get("duration") or 0)
    cover = _cover_of(video, api_item) or str(yt_meta.get("cover") or "")

    return {
        "platform": "tiktok",
        "type": "video",
        "id": video_id,
        "url": final_url,
        "title": description or f"TikTok {video_id}",
        "desc": description,
        "author": author,
        "duration": duration,
        "create_time": item.get("createTime") or 0,
        "region": item.get("region") or api_item.get("region") or "",
        "cover": cover,
        "music": {
            "title": str(music.get("title") or web_music.get("title") or ""),
            "url": str((web_music.get("playUrl") or "")),
        },
        "stats": _stats_of(item, api_item),
        "qualities": qualities,
        "best_index": 0,
        "filename": filename,
        "hint": hint,
        "source": source,
        "proxy": {
            "value": proxy,
            "source": proxy_source,
            "configured": bool(proxy),
        },
    }
