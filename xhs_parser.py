# -*- coding: utf-8 -*-
"""小红书（Xiaohongshu / RED）无水印解析模块。

## 为什么必须带 xsec_token（2026-09 实测）

小红书自 2024 年起全面启用 ``xsec_token`` 反爬：

* 直接访问 ``https://www.xiaohongshu.com/explore/<note_id>``（**不带 token**）
  → HTTP 200，但页面是「你访问的页面不见了」，内嵌状态里
  ``note.noteDetailMap == {}``，**一条数据都没有**（已实测确认）；
* 未登录访问 ``/explore`` 首页 → 302 跳 ``/login?redirectPath=...``，
  站内也无法自行捞取 token（已实测：首页/搜索页均为 0 个 token）。

因此本模块的输入**必须是小红书 App「分享 → 复制链接」得到的完整链接**
（xhslink.com 短链或带 ``xsec_token`` 的长链）。这是平台限制，无法绕过。

## 解析链路

    分享文本 -> 提取链接 -> 若是 xhslink.com 短链则跟随 302 还原为长链
             -> 从长链取 note_id + xsec_token
             -> 请求笔记页 HTML（curl_cffi 模拟 Chrome TLS 指纹）
             -> 正则抠出 window.__INITIAL_STATE__
             -> 兼容 undefined 的 JSON 清洗后 json.loads
             -> 定位 noteDetailMap[note_id].note
             -> 归一化成与 douyin_parser 一致的结构（前端可复用同一套渲染）

## 三个能力点

1. **图片最高清**：去掉 URL 上的压缩样式后缀（``!nd_dft_wlteh_webp_3``）与
   处理参数（``?imageView2/...``），并对 webpic 主机另给一份 img 主机备选；
2. **动图（Live Photo）**：在图片条目里递归寻找动态视频流（``masterUrl`` /
   ``backupUrls``），有则挂到该图的 ``live`` 字段；
3. **无水印视频**：优先用 ``video.consumer.originVideoKey`` 拼「原始」直链
   （上传原片，无平台 logo），并保留播放用的 ``media.stream`` 各档作为备选。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

# curl_cffi 用于模拟 Chrome 的 TLS 指纹（JA3），小红书对此有校验。
# 未安装时自动降级到 requests（可能被风控，但保证不崩）。
try:  # pragma: no cover - 环境相关
    from curl_cffi import requests as _curl_requests  # type: ignore
    _HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    _curl_requests = None
    _HAS_CURL_CFFI = False

import requests as _plain_requests


# 桌面 UA（保留备查：实测它在 PC 线路上会被强制跳登录，不要用它请求笔记页）
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ★ 必须用移动端 UA（2026-09-20 实测）
# 用桌面 UA 请求 /explore/<id>?xsec_token=... 会被**服务端** 302 到
# https://www.xiaohongshu.com/login?redirectPath=...，即使 token 是刚复制的、
# 完全有效也一样（实测 5 种 URL 形态全部被拦）。换成移动端 UA 后同一个
# 链接直接返回 200 且内嵌完整笔记数据（约 140KB vs 登录页 36KB）。
# 原因推测：移动 H5 走的是「免登录可读」通道，PC web 走的是需要 web_session
# cookie 的通道。**这是待证实项，但现象稳定可复现。**
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-S9110) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

# 对外统一口径：UA 就是移动端 UA（server.py 的 XHS_HEADERS 引用这个名字）
UA = MOBILE_UA

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.xiaohongshu.com/explore",
    "Upgrade-Insecure-Requests": "1",
}

# 原始视频直链主机（originVideoKey 走这个域）
ORIGIN_VIDEO_HOST = "https://sns-video-bd.xhscdn.com/"
# originVideoKey 形如 "pre_post/xxxx" / "spectrum/xxxx"，含斜杠，故允许 /
_VIDEO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-/]+$")

# ---- 图片「真·原图」主机（2026-09-20 实测）--------------------------------
# 平台在笔记页里给的图片地址是**压缩分发版**，形如
#   http://sns-webpic-qc.xhscdn.com/<ts>/<hash>/<fileId>!h5_1080jpg
# 实测 1080x1920 / 514KB（原图是 2160x3840 / 1032KB）。
# ★ 关键坑：把 `!h5_1080jpg` 后缀去掉**不会**得到原图，而是 403 ——
#   该后缀是签名 URL 的组成部分，不是可选的「样式」。所以正确做法是
#   拿 imageList 里的 fileId 去换一个「原图分发」域名，而不是删后缀。
# 实测四种换法（fileId 相同）：
#   sns-img-bd.xhscdn.com/<fileId>      → 200  907KB  image/heic（原图 HEIC）
#   sns-img-qc.xhscdn.com/<fileId>      → 200  907KB  image/heic
#   ci.xiaohongshu.com/<fileId>         → 200  907KB  image/heic
#   ci.xiaohongshu.com/<fileId>?imageView2/2/w/0/format/jpg
#                                       → 200 1032KB  image/jpeg 2160x3840 ★最高清且浏览器可看
CI_ORIGIN_HOST = "https://ci.xiaohongshu.com/"
XHS_IMG_HOSTS = ("https://sns-img-bd.xhscdn.com/", "https://sns-img-qc.xhscdn.com/")
# fileId 形如 "notes_pre_post/xxxx" 或 "c/notes_pre_post/xxxx"，含斜杠
_IMAGE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-/]+$")

# 从分享文本里抠链接：排除中文标点与空白，避免把「，复制本条信息」吃进来
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
BARE_HOST_PATTERN = re.compile(
    r"(?:www\.)?(?:xiaohongshu\.com|xhslink\.(?:com|cn))/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+"
)

# 短链域名：xhslink.com 是老的，xhslink.cn 是现在 App「复制链接」实际给出的
SHORT_LINK_HOSTS = ("xhslink.com", "xhslink.cn")
# 尾部常被中文标点粘连
TAIL_TRIM = " \t\r\n，。；、！？）】》”’…·"

INITIAL_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>", re.DOTALL
)

NOTE_ID_PATTERNS = (
    re.compile(r"/(?:explore|discovery/item|note)/([0-9a-fA-F]{16,32})"),
    re.compile(r"noteId=([0-9a-fA-F]{16,32})"),
)
TOKEN_PATTERN = re.compile(r"xsec_token=([A-Za-z0-9_\-=+/]+)")

ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')

# imageScene 里带 WM 的是「带水印」，用作排序时的降权依据
_WM_HINT = re.compile(r"WM|watermark", re.I)


class XHSError(Exception):
    """解析失败，携带面向用户的中文提示。"""


# --------------------------------------------------------------------------
# 请求层
# --------------------------------------------------------------------------

def _session():
    """返回一个「会模拟 Chrome」的会话。

    curl_cffi 的 requests 提供 ``impersonate`` 参数；这里统一封装成
    ``_get(url)``，未装 curl_cffi 时退回普通 requests。
    """
    if _HAS_CURL_CFFI:
        return _curl_requests.Session()
    return _plain_requests.Session()


def _get(url: str, session=None, allow_redirects: bool = True, timeout: int = 25):
    session = session or _session()
    if _HAS_CURL_CFFI:
        return session.get(url, headers=HEADERS, impersonate="chrome",
                           allow_redirects=allow_redirects, timeout=timeout)
    return session.get(url, headers=HEADERS, allow_redirects=allow_redirects,
                       timeout=timeout)


# --------------------------------------------------------------------------
# 链接处理
# --------------------------------------------------------------------------

def extract_url(text: str) -> str:
    """从分享文案中提取链接；也接受裸域名（无 http:// 前缀）。"""
    if not text or not text.strip():
        raise XHSError("请输入小红书分享链接")
    match = URL_PATTERN.search(text)
    if match:
        return match.group(0).rstrip(TAIL_TRIM).rstrip(".,;")
    bare = BARE_HOST_PATTERN.search(text)
    if bare:
        return ("https://" + bare.group(0)).rstrip(TAIL_TRIM).rstrip(".,;")
    raise XHSError(
        "没在内容里找到小红书链接。请在小红书 App 里点「分享 → 复制链接」，"
        "把复制到的整段内容粘进来（短链 xhslink.cn / xhslink.com 或含 xsec_token 的长链都行）。"
    )


def _looks_like_xhs(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("xiaohongshu.com"):
        return True
    return any(host.endswith(s) for s in SHORT_LINK_HOSTS)


def _unwrap_login_redirect(url: str) -> str:
    """把 ``/login?redirectPath=<真实地址>`` 里的真实地址解出来。

    2026-09 实测：短链（xhslink.cn / xhslink.com）跟随 302 后**不会**落到笔记页，
    而是落到 ``https://www.xiaohongshu.com/login?redirectPath=...``，
    真实目标被 URL 编码塞在 ``redirectPath`` 参数里（含 note_id 与 xsec_token）。
    不解这一层，就会「跳登录页 → 认为需要登录」而直接失败。
    """
    if not url:
        return ""
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return ""
    raw = (qs.get("redirectPath") or [""])[0]
    if not raw:
        return ""
    target = unquote(raw)
    if not target.startswith("http"):
        return ""
    # http -> https，避免多一次跳转
    if target.startswith("http://"):
        target = "https://" + target[len("http://"):]
    return target


def resolve_short_link(url: str) -> Tuple[str, str]:
    """短链还原：跟随 302 拿到带 xsec_token 的长链。

    返回 ``(最终链接, 页面HTML)``。HTML 可能为空（未真正抓到页面时）。
    """
    if not _looks_like_xhs(url):
        raise XHSError(f"这不是小红书链接：{url}")

    host = (urlparse(url).hostname or "").lower()
    if not any(host.endswith(s) for s in SHORT_LINK_HOSTS):
        return url, ""

    try:
        resp = _get(url)
    except Exception as exc:
        raise XHSError(f"短链还原失败（网络问题）：{exc}") from exc

    final = str(resp.url or url)
    html = resp.text or ""

    # 短链通常落在 /login?redirectPath=... —— 把真实笔记地址解出来
    unwrapped = _unwrap_login_redirect(final)
    if unwrapped:
        return unwrapped, html
    return final, html


def extract_note_id(url: str) -> str:
    """从长链里取 note_id。"""
    for pattern in NOTE_ID_PATTERNS:
        m = pattern.search(url or "")
        if m:
            return m.group(1)
    # 兜底：路径里最长的 16~32 位十六进制串
    candidates = re.findall(r"[0-9a-fA-F]{16,32}", urlparse(url).path or "")
    if candidates:
        return max(candidates, key=len)
    raise XHSError(
        "没能从链接里识别出笔记 ID。请重新在小红书 App 里复制分享链接后重试。"
    )


def extract_token(url: str) -> str:
    """取 xsec_token。

    优先用 ``parse_qs``（会做 URL 解码，token 结尾的 ``=`` 常被编码成 ``%3D``），
    正则仅作兜底 —— 正则遇到 ``%`` 会提前截断，拿到的 token 不完整。
    """
    if not url:
        return ""
    try:
        qs = parse_qs(urlparse(url).query)
        tok = (qs.get("xsec_token") or [""])[0]
        if tok:
            return tok
    except Exception:
        pass
    m = TOKEN_PATTERN.search(url)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# 页面数据
# --------------------------------------------------------------------------

def clean_state_json(raw: str) -> Optional[dict]:
    """把内嵌 JS 对象文本变成 dict。

    小红书的 SSR 输出里含裸 ``undefined``（不是合法 JSON），需要先替换再做解析。
    """
    if not raw:
        return None
    text = raw.strip().rstrip(";").strip()
    if not text.startswith("{"):
        return None
    text = re.sub(r"\bundefined\b", "null", text)
    try:
        return json.loads(text)
    except Exception:
        return None


def extract_initial_state(html: str) -> Optional[dict]:
    m = INITIAL_STATE_PATTERN.search(html or "")
    if not m:
        return None
    return clean_state_json(m.group(1))


def _unwrap(value: Any) -> Any:
    """解 Vue ref 包装：{_rawValue: x} / {value: x} / {_value: x}。"""
    depth = 0
    while isinstance(value, dict) and depth < 4:
        for key in ("_rawValue", "value", "_value"):
            if key in value:
                value = value[key]
                break
        else:
            return value
        depth += 1
    return value


def _deep_get(obj: Any, *paths: str) -> Any:
    for path in paths:
        cur = obj
        ok = True
        for seg in path.split("."):
            cur = _unwrap(cur)
            if not isinstance(cur, dict) or seg not in cur:
                ok = False
                break
            cur = cur[seg]
        if ok and cur not in (None, {}, []):
            return _unwrap(cur)
    return None


def pick_note(state: dict, note_id: str = "") -> dict:
    """从 __INITIAL_STATE__ 里定位笔记主体。

    兼容两种结构：
      * PC  ：note.noteDetailMap[<note_id>].note（另有 .value / ._rawValue 包装）
      * 手机：noteData.data.noteData
    """
    if not isinstance(state, dict):
        raise XHSError("页面数据结构异常（不是对象）")

    note_map = _deep_get(state, "note.noteDetailMap")
    if isinstance(note_map, dict) and note_map:
        # 优先按 id 命中，否则取第一条
        entry = None
        if note_id and note_id in note_map:
            entry = note_map[note_id]
        if entry is None:
            first_key = next(iter(note_map))
            entry = note_map[first_key]
        entry = _unwrap(entry)
        if isinstance(entry, dict):
            note = _unwrap(entry.get("note")) or _unwrap(entry.get("noteCard"))
            if isinstance(note, dict) and note:
                return note

    phone = _deep_get(state, "noteData.data.noteData", "noteData.noteData")
    if isinstance(phone, dict) and phone:
        return phone

    # 再兜底：把 noteDetailMap 当作 note 本身
    if isinstance(note_map, dict) and note_map:
        first = _unwrap(next(iter(note_map.values())))
        if isinstance(first, dict) and first.get("noteId"):
            return first

    raise XHSError(
        "页面里没有笔记数据。多半是链接里的 xsec_token 已过期或缺失——"
        "请重新在小红书 App 里点「分享 → 复制链接」，用最新的链接重试。"
    )


def fetch_note(url: str, note_id: str = "") -> Tuple[dict, str]:
    """抓取并解析笔记页，返回 ``(note原始dict, 最终URL)``。"""
    final_url, _html_from_short = resolve_short_link(url)

    token = extract_token(final_url)
    if not token:
        raise XHSError(
            "这条链接里没有 xsec_token，小红书不会返回数据。"
            "请不要手动删掉链接里的参数，也别只复制纯链接——"
            "请在小红书 App 里点「分享 → 复制链接」，把整段内容粘进来。"
        )

    resolve_target = final_url
    if not note_id:
        note_id = extract_note_id(final_url)

    try:
        resp = _get(resolve_target)
    except Exception as exc:
        raise XHSError(f"请求小红书失败（网络问题，重点看代理是否影响国内站点）：{exc}") from exc

    html = resp.text or ""
    state = extract_initial_state(html)
    if state is None:
        # 有时会落到登录页或验证页
        if "登录" in html and "noteDetailMap" not in html:
            raise XHSError(
                "小红书要求登录后才返回数据（可能触发了风控）。"
                "请稍后重试，或换一条新复制的分享链接。"
            )
        raise XHSError("页面里没找到内嵌数据（__INITIAL_STATE__），可能被风控拦截。")

    note = pick_note(state, note_id)
    return note, str(resp.url or resolve_target)


# --------------------------------------------------------------------------
# 图片：最高清 + 动图
# --------------------------------------------------------------------------

def normalize_image_url(url: str) -> str:
    """清洗图片地址（只去首尾空白）。

    ★ 历史实现会删掉 ``!...`` 后缀，**这是错的**（2026-09-20 实测）：
    平台给的
        http://sns-webpic-qc.xhscdn.com/<ts>/<hash>/<fileId>!h5_1080jpg
    删掉 ``!h5_1080jpg`` 后请求返回 **403**（该后缀是签名的一部分）。
    所以这里只清洗空白，要「更高清」请用 ``original_image_urls(fileId)``
    换成原图分发域名。
    """
    return (url or "").strip()


def original_image_urls(file_id: str) -> List[str]:
    """用 ``fileId`` 拼出「原图」候选地址，最高清优先。

    实测（2026-09-20，同一张图）：
        ci.xiaohongshu.com/<fileId>?imageView2/2/w/0/format/jpg
            → 2160x3840 · JPEG · 1032KB   ← 真原图，浏览器可直接看
        sns-img-bd.xhscdn.com/<fileId>  → 907KB · HEIC（同一原图，HEIC 需专门解码）
    对照：平台默认给的 ``!h5_1080jpg`` 只有 1080x1920 · 514KB。
    """
    fid = (file_id or "").strip().lstrip("/")
    if not fid or not _IMAGE_KEY_RE.match(fid):
        return []
    out = [CI_ORIGIN_HOST + fid + "?imageView2/2/w/0/format/jpg"]
    for host in XHS_IMG_HOSTS:
        out.append(host + fid)
    return out


def image_url_candidates(url: str) -> List[str]:
    """对「平台原始地址」生成候选。

    只做清洗，不再做「换主机 + 删后缀」——实测那套写法在移动端地址上
    会得到 403 / 404（详见 ``normalize_image_url`` 注释）。
    更清晰的来源请走 ``original_image_urls``。
    """
    base = normalize_image_url(url)
    return [base] if base else []


def _collect_image_urls(image: dict) -> List[str]:
    """收集单张图的候选地址：**真原图（fileId 拼）优先**，平台原始地址兜底。

    平台给的 ``url`` 是 ``!h5_1080jpg`` 压缩分发版；``fileId`` 才能换到
    2160x3840 的真原图（实测），所以顺序是「原图 → 压缩版兜底」。
    """
    raw: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.startswith("http") and value not in raw:
            raw.append(value)

    for key in ("urlDefault", "url", "urlPre", "urlWatermark", "urlTrail"):
        add(image.get(key))

    info_list = _unwrap(image.get("infoList"))
    if isinstance(info_list, list):
        # 带水印（WM）的场景排到后面
        ordered = sorted(
            [it for it in info_list if isinstance(it, dict)],
            key=lambda it: 1 if _WM_HINT.search(str(it.get("imageScene") or "")) else 0,
        )
        for it in ordered:
            add(it.get("url"))

    out: List[str] = []
    # 1) 最高清：用 fileId 换原图分发域名
    for u in original_image_urls(str(image.get("fileId") or "")):
        if u not in out:
            out.append(u)
    # 2) 兜底：平台原始地址（压缩版，但一定可用）
    for u in raw:
        for cand in image_url_candidates(u):
            if cand and cand not in out:
                out.append(cand)
    return out


def _find_video_stream_in(obj: Any, depth: int = 0) -> List[str]:
    """递归在任意嵌套里寻找视频直链（动图的动态部分藏在这里）。"""
    found: List[str] = []
    if depth > 6:
        return found
    if isinstance(obj, dict):
        for key in ("masterUrl", "master_url", "originVideoKey", "url"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                if val.startswith("http"):
                    found.append(val)
                elif key == "originVideoKey" and _VIDEO_KEY_RE.match(val):
                    found.append(ORIGIN_VIDEO_HOST + val.lstrip("/"))
        for key in ("backupUrls", "backup_urls"):
            val = obj.get(key)
            if isinstance(val, list):
                for u in val:
                    if isinstance(u, str) and u.startswith("http"):
                        found.append(u)
        for key, val in obj.items():
            if key in ("masterUrl", "backupUrls", "urlDefault", "urlPre"):
                continue
            found.extend(_find_video_stream_in(val, depth + 1))
    elif isinstance(obj, list):
        for val in obj[:20]:
            found.extend(_find_video_stream_in(val, depth + 1))
    # 只保留看起来像视频的，去重
    out: List[str] = []
    for u in found:
        if any(ext in u for ext in (".mp4", ".mov", "sns-video", "stream")) or "sns-video" in u:
            if u not in out:
                out.append(u)
    return out


def _safe_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_images(image_list: Any) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """解析图片列表，返回 ``(图片url列表, image_items, live视频列表)``。"""
    urls: List[str] = []
    items: List[Dict[str, Any]] = []
    lives: List[Dict[str, Any]] = []

    for index, image in enumerate(_unwrap(image_list) or [], start=1):
        if not isinstance(image, dict):
            continue
        cands = _collect_image_urls(image)
        primary = cands[0] if cands else ""

        live: Optional[Dict[str, Any]] = None
        if image.get("livePhoto") or image.get("isLivePhoto") or image.get("live_photo"):
            motion = _find_video_stream_in(image)
            if motion:
                live = {
                    "url": motion[0],
                    "urls": motion,
                    "label": "动图",
                    "width": _safe_int(image.get("liveWidth") or image.get("width")),
                    "height": _safe_int(image.get("liveHeight") or image.get("height")),
                    "duration": 0,
                    "size": 0,
                }

        if not primary and not live:
            continue
        if primary:
            urls.append(primary)

        entry: Dict[str, Any] = {
            "index": index,
            "url": primary,
            "urls": cands,
            "width": _safe_int(image.get("width")),
            "height": _safe_int(image.get("height")),
            "watermark": False,  # 去压缩后缀后取到的是原图，无水印
            "live": bool(live),
        }
        if live:
            entry["live"] = live
            lives.append({"index": index, **live})
        items.append(entry)

    return urls, items, lives


# --------------------------------------------------------------------------
# 视频
# --------------------------------------------------------------------------

def _decode_media_v2(value: Any) -> Optional[dict]:
    """解码 ``video.mediaV2``。

    移动端笔记页里 ``mediaV2`` 是**字符串形式的 JSON**（不是对象），
    里面装着完整的 ``stream.{h264,h265,av1}[]``。不解这一层就会只剩
    1 档「1080P」，拿不到全部可选项。
    """
    obj = _unwrap(value)
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        text = obj.strip()
        if text.startswith("{"):
            try:
                decoded = json.loads(text)
            except Exception:
                return None
            return decoded if isinstance(decoded, dict) else None
    return None


def parse_video(video: Any) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
    """解析视频条目，返回 ``(url列表, 清晰度档位, 附加信息)``。"""
    video = _unwrap(video) or {}
    if not isinstance(video, dict):
        return [], [], {}

    qualities: List[Dict[str, Any]] = []
    info: Dict[str, Any] = {}

    # 1) 原始视频（originVideoKey）：上传原片，无平台 logo
    consumer = _unwrap(video.get("consumer")) or {}
    origin_key = ""
    if isinstance(consumer, dict):
        origin_key = str(consumer.get("originVideoKey") or "").strip()
    origin_url = ""
    if origin_key.startswith("http"):
        origin_url = origin_key
    elif origin_key and _VIDEO_KEY_RE.match(origin_key):
        origin_url = ORIGIN_VIDEO_HOST + origin_key.lstrip("/")
    if origin_url:
        qualities.append({
            "name": "origin",
            "label": "原始 · 无水印",
            "resolution": 0,
            "width": 0,
            "height": 0,
            "url": origin_url,
            "urls": [origin_url],
            "codec": "h264",
            "is_h265": False,
            "bit_rate": 0,
            "size": 0,
        })
        info["origin_video_key"] = origin_key

    # 2) 播放流各档。可能出现的位置（移动页把完整流塞在 mediaV2 里）：
    #    - video.media.stream.{h264,h265,av1,h266}[]
    #    - video.stream.{...}
    #    - video.mediaV2 —— **字符串形式的 JSON**，要二次 json.loads 才拿到 stream
    stream_sources: List[Any] = [
        _deep_get(video, "media.stream"),
        video.get("stream"),
        _decode_media_v2(video.get("mediaV2")),
    ]
    seen_streams: set = set()
    for src in stream_sources:
        src = _unwrap(src)
        if not isinstance(src, dict):
            continue
        codec_order = ("h265", "h264", "av1", "h266")
        for codec in codec_order:
            variants = _unwrap(src.get(codec))
            if not isinstance(variants, list):
                continue
            for variant in variants:
                variant = _unwrap(variant)
                if not isinstance(variant, dict):
                    continue
                master = variant.get("masterUrl") or variant.get("master_url") or ""
                if not master or master in seen_streams:
                    continue
                seen_streams.add(master)
                backup = variant.get("backupUrls") or variant.get("backup_urls") or []
                urls = [master] + [u for u in backup if isinstance(u, str)]
                height = _safe_int(variant.get("height"))
                width = _safe_int(variant.get("width"))
                qualities.append({
                    "name": codec,
                    "label": _quality_label(height, codec, width),
                    "resolution": min(width, height) if width and height else height,
                    "width": width,
                    "height": height,
                    "url": master,
                    "urls": urls,
                    "codec": "h265" if codec in ("h265", "h266") else codec,
                    "is_h265": codec in ("h265", "h266"),
                    "bit_rate": _safe_int(variant.get("avgBitrate") or variant.get("bitrate")),
                    "size": _safe_int(variant.get("size")),
                    "fps": _safe_int(variant.get("fps")),
                })
                info.setdefault("duration_ms", _safe_int(variant.get("duration")))
                info.setdefault("cover", "")

    # 原始档自带宽高为 0，用最高档回填（实测两者一致，便于前端显示）
    best_stream = max(
        (q for q in qualities if q.get("name") != "origin"),
        key=lambda q: (q.get("resolution") or 0, q.get("fps") or 0, q.get("bit_rate") or 0),
        default=None,
    )
    if best_stream:
        for q in qualities:
            if q.get("name") == "origin" and not q.get("width"):
                for key in ("width", "height", "resolution", "fps"):
                    q[key] = best_stream.get(key) or 0

    # 去重（同一地址只留一条），并按「原始优先 -> 分辨率降序 -> 帧率降序 -> 码率降序」排
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for q in qualities:
        if not q.get("url") or q["url"] in seen:
            continue
        seen.add(q["url"])
        deduped.append(q)
    deduped.sort(
        key=lambda q: (0 if q.get("name") == "origin" else 1,
                       -(q.get("resolution") or q.get("height") or 0),
                       -(q.get("fps") or 0),
                       -(q.get("bit_rate") or 0))
    )

    urls: List[str] = []
    for q in deduped:
        for u in q.get("urls") or [q["url"]]:
            if u not in urls:
                urls.append(u)
    return urls, deduped, info


def _quality_label(height: int, codec: str, width: int = 0) -> str:
    """档位文案。

    竖屏视频（如 720x1280）按 **短边** 定档更贴合直觉：
    长边 720 的竖屏叫「720P」而不是「1080P」。
    """
    side = min(width, height) if width and height else height
    if side >= 2160:
        name = "4K"
    elif side >= 1440:
        name = "2K"
    elif side >= 1080:
        name = "1080P"
    elif side >= 720:
        name = "720P"
    elif side >= 480:
        name = "480P"
    elif side > 0:
        name = f"{side}P"
    else:
        name = "默认"
    codec_name = {"h264": "H.264", "h265": "H.265", "h266": "H.266", "av1": "AV1"}.get(
        codec, codec.upper()
    )
    return f"{name} · {codec_name}"


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------

def _clean_filename(name: str, fallback: str = "小红书") -> str:
    name = ILLEGAL_FILENAME.sub("_", (name or "").strip())
    name = name[:60].strip(" .")
    return name or fallback


def normalize(note: dict, note_id: str, source_url: str = "") -> Dict[str, Any]:
    """把小红书 note 归一化成与 douyin_parser 一致的结构，前端可复用渲染。"""
    note = _unwrap(note) or {}

    title = (note.get("title") or "").strip()
    desc = (note.get("desc") or "").strip()
    user = _unwrap(note.get("user")) or {}
    interact = _unwrap(note.get("interactInfo") or note.get("interact_info")) or {}

    images, image_items, live_videos = parse_images(note.get("imageList") or note.get("images"))

    is_video = str(note.get("type") or "").lower() == "video"
    video_urls, qualities, vinfo = ([], [], {})
    if is_video:
        video_urls, qualities, vinfo = parse_video(note.get("video"))

    # 封面：优先视频自带封面，否则第一张图
    cover = ""
    if is_video:
        media = _deep_get(note, "video.media") or {}
        images_cover = _unwrap(media.get("image")) if isinstance(media, dict) else None
        if isinstance(images_cover, dict):
            cover = normalize_image_url(
                str(images_cover.get("firstFrame") or images_cover.get("thumbnail") or "")
            )
        if not cover and images:
            cover = images[0]
        # 视频笔记的 imageList 只是封面，不能当图集渲染，否则会和视频预览重复
        images, image_items, live_videos = [], [], []
    else:
        if images:
            cover = images[0]

    # 时长：优先视频流给的 duration（毫秒），否则 note 上的
    duration_ms = _safe_int(vinfo.get("duration_ms")) or _safe_int(note.get("videoDuration"))
    if duration_ms > 86_400_000:
        duration_ms //= 1000

    # 发布时间：小红书 note.time 是毫秒；前端 fmtTime 按「秒」处理，这里先归一
    create_time = _safe_int(note.get("time"))
    if create_time > 10_000_000_000:
        create_time //= 1000

    fallback_title = f"小红书笔记_{note_id}"
    filename = _clean_filename(title or desc, fallback_title)

    return {
        "note_id": note.get("noteId") or note_id,
        "type": "video" if is_video else "images",
        "title": title or desc or fallback_title,
        "filename": filename,
        "desc": desc,
        "cover": cover,
        "duration": round(duration_ms / 1000, 1),
        "create_time": create_time,
        "ip_location": note.get("ipLocation") or "",
        "author": {
            "nickname": user.get("nickname") or user.get("nickName") or "",
            "uid": user.get("userId") or "",
            "sec_uid": user.get("xsecToken") or "",
            "avatar": normalize_image_url(str(user.get("avatar") or "")),
        },
        "author_avatar": normalize_image_url(str(user.get("avatar") or "")),
        "music": {"title": "", "author": "", "url": ""},
        "stats": {
            "digg": _safe_int(interact.get("likedCount")),
            "comment": _safe_int(interact.get("commentCount")),
            "share": _safe_int(interact.get("shareCount")),
            "collect": _safe_int(interact.get("collectedCount")),
            "play": 0,
        },
        "tags": [
            (t.get("name") if isinstance(t, dict) else str(t)).strip()
            for t in (_unwrap(note.get("tagList")) or [])
        ][:12],
        "video_urls": video_urls,
        "images": images,
        "image_items": image_items,
        "live_photo": bool(live_videos),
        "live_videos": live_videos,
        "qualities": qualities,
        "hd": bool(qualities),
        "source_url": source_url,
    }


def parse(text: str) -> Dict[str, Any]:
    """入口：传入分享文本/链接，返回归一化笔记信息。"""
    url = extract_url(text)
    if not _looks_like_xhs(url):
        raise XHSError(f"这不是小红书链接：{url}")

    final_url, _ = resolve_short_link(url)
    note_id = extract_note_id(final_url)
    note, used_url = fetch_note(final_url, note_id)

    data = normalize(note, note_id, source_url=final_url)
    data["resolved_url"] = used_url
    if not data.get("images") and not data.get("video_urls"):
        raise XHSError(
            "解析到了笔记，但没有取到可下载的图片或视频地址。"
            "可能是链接里的 xsec_token 已过期，请重新复制分享链接。"
        )
    return data


# --------------------------------------------------------------------------
# 调试：结构 dump（拿真实链接对照字段用）
# --------------------------------------------------------------------------

def _dump_shape(obj: Any, prefix: str = "", depth: int = 0, lines: Optional[List[str]] = None) -> List[str]:
    lines = lines if lines is not None else []
    if depth > 4:
        return lines
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, dict):
                lines.append(f"{path}  (dict, {len(val)} keys)")
                _dump_shape(val, path, depth + 1, lines)
            elif isinstance(val, list):
                lines.append(f"{path}  (list, {len(val)})")
                if val:
                    _dump_shape(val[0], path + "[0]", depth + 1, lines)
            else:
                preview = str(val)[:70]
                lines.append(f"{path} = {preview}")
    return lines


def dump(url_or_html: str) -> str:
    """打印笔记数据的字段结构，便于对照真实返回。"""
    if url_or_html.startswith("http"):
        url = extract_url(url_or_html)
        final_url, _ = resolve_short_link(url)
        note_id = extract_note_id(final_url)
        note, _ = fetch_note(final_url, note_id)
    else:
        html = open(url_or_html, encoding="utf-8").read()
        state = extract_initial_state(html)
        note = pick_note(state or {})
    return "\n".join(_dump_shape(note))


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python xhs_parser.py <分享链接>")
        print("      python xhs_parser.py --dump <分享链接|html文件>")
        raise SystemExit(1)

    if sys.argv[1] == "--dump":
        print(dump(sys.argv[2]))
    else:
        print(json.dumps(parse(sys.argv[1]), ensure_ascii=False, indent=2))
