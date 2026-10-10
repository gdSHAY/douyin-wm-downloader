# -*- coding: utf-8 -*-
"""
抖音 Web 详情接口（高清通道）。

调用 /aweme/v1/web/aweme/detail/ 可拿到底层 bit_rate 多档清晰度
（部分新作品含 1080p / 2K / 4K），而 share 页方案只会给到默认的 720p 档。

签名实现复用自 Evil0ctal/Douyin_TikTok_Download_API（Apache-2.0），
其 abogus.py 源自 JoeanAmier/TikTokDownloader（GPL-3.0），见 douyin_abogus.py 文件头。

重要：ABogus 的 ua_code 与下列 User-Agent 强绑定，不可更改，否则签名校验失败。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

import requests

from douyin_abogus import ABogus

# 该 UA 必须与 douyin_abogus.py 中的 ua_code 对应，切勿修改
FIXED_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36")

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1")

DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# ---------------------------------------------------------------------------
# ★ 边缘网关的业务前置校验：缺这个请求头，detail 接口一律 403
#
# 2026-09-19 起，抖音在**边缘网关**给 /aweme/v1/web/aweme/detail/（以及 aweme/post
# 那批接口）挂了 ArgusSecurityPlugin，做业务前置校验。缺 `x-tt-argus` 请求头时
# 直接 403，响应体是：
#
#     Blocked by ArgusSecurityPlugin Uifid Not Found
#
# ⚠️ 这句文案**极具误导性**：它并不真的要求 Uifid。本机 8 组对照实测（2026-10-10）：
#
#     什么头都不加                     → 403  ... Uifid Not Found
#     只补 `uifid: 1`、不补 x-tt-argus → 403  ... Signature Not Found（更糟）
#     补上 `x-tt-argus`（值随便写）     → 200  21 条 bit_rate / 4 档清晰度
#
# 而且用的只是**匿名 ttwid**，没有任何登录 Cookie，照样放行。
# 也就是说：曾经「高清通道被风控挡死、抖音上限只有 720P」的判断是错的 ——
# 真正缺的只是这个头。详见项目 README 的「抖音取流」章节。
#
# ⚠️ 这是**权宜之计**：网关当前不校验该头的取值，但哪天升级到真校验，就会重新
# 变成 Signature Not Found。届时的正解是「在页面里注入 JS 让抖音自带的 SDK 补齐
# Argus 头」（等于引入浏览器内核），而不是继续猜这个固定值。
# 外部依据：NanmiCoder/MediaCrawler commit 380b426（2026-09-19）、
# Johnserf-Seed/f2 issue #443 —— 两处都明确标注了这是权宜之计。
# ---------------------------------------------------------------------------
ARGUS_HEADER = "x-tt-argus"

#: 网关当前不校验取值，任意非空字符串均可（实测 "1" / "0" / a_bogus 都放行）。
ARGUS_HEADER_VALUE = "1"

# 与上游项目 models.py 中 BaseRequestModel + PostDetail 的默认字段保持一致
BASE_PARAMS: Dict[str, Any] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": 1,
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": 1920,
    "screen_height": 1080,
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "130.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "130.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": 12,
    "device_memory": 8,
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "from_user_page": "1",
    "locate_query": "false",
    "need_time_list": "1",
    "pc_libra_divert": "Windows",
    "publish_video_strategy_type": "2",
    "round_trip_time": "0",
    "show_live_replay_strategy": "1",
    "time_list_query": "0",
    "whale_cut_token": "",
    "msToken": "",
}

# 从档位名里兜底解析分辨率（如 normal_720_0 -> 720）。
#
# ⚠️ 旧写法 `_(\d{3,4})_` 有个洞：要求数字**前后都有下划线**，于是形如
#    `1080_1_1` / `720_1_1` / `540_2_1` 这类「无 normal_ 前缀」的档位名解析出来是 0。
#    0 会被排到最末，直接破坏「qualities[0] = 最高档」这个后端对前端的契约。
#    现在允许数字出现在串首（`(?:^|_)`）。
#
# 注意这只是**兜底**：优先用 play_addr 的真实宽高判档（见 _short_side），
# 档位名解析只在拿不到宽高时才生效。
GEAR_RES_PATTERN = re.compile(r"(?:^|_)(\d{3,4})(?:_|$)")


class HDUnavailable(Exception):
    """高清通道不可用（签名失败 / 风控 / 作品不可见），调用方应降级。"""


def detail_headers(ttwid: Optional[str] = None) -> Dict[str, str]:
    """detail 接口要带的请求头。

    抽成独立函数只为一件事：让离线单测能直接断言「必需的头到底带没带」，
    而不必去 patch `requests` 或真的联网。
    """
    headers = {
        "User-Agent": FIXED_UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.8",
        ARGUS_HEADER: ARGUS_HEADER_VALUE,
    }
    if ttwid:
        headers["Cookie"] = "ttwid=%s" % ttwid
    # ⚠️ 刻意**不**发 `uifid` 头：拿不到真实 Uifid 时补一个假值，会把报错从
    #    "Uifid Not Found" 变成 "Signature Not Found"，反而更难排查
    #    （MediaCrawler 的测试里也专门断言了这一条）。
    return headers


class HDClient:
    """带 a_bogus 签名的抖音 Web 详情客户端。"""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._ttwid: Optional[str] = None

    def _ensure_ttwid(self) -> Optional[str]:
        """获取非登录态 ttwid。抖音 Web API 至少需要该 Cookie 才会返回数据。"""
        if self._ttwid:
            return self._ttwid
        try:
            self._session.get(
                "https://www.iesdouyin.com/",
                headers={"User-Agent": MOBILE_UA},
                timeout=15,
            )
            self._ttwid = self._session.cookies.get("ttwid")
        except requests.RequestException:
            self._ttwid = None
        return self._ttwid

    def fetch_detail(self, aweme_id: str, timeout: int = 20) -> Dict[str, Any]:
        """获取作品完整详情，失败抛出 HDUnavailable。"""
        params = dict(BASE_PARAMS)
        params["aweme_id"] = str(aweme_id)

        try:
            a_bogus = ABogus().get_value(params)
        except Exception as exc:
            raise HDUnavailable(f"生成签名失败：{exc}") from exc

        url = f"{DETAIL_API}?{urlencode(params)}&a_bogus={quote(a_bogus, safe='')}"

        headers = detail_headers(self._ensure_ttwid())

        try:
            resp = self._session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise HDUnavailable(f"网络请求失败：{exc}") from exc

        if resp.status_code != 200:
            # 把响应体一起带出来 —— 2026-10 踩过的坑：只报「HTTP 403」时，
            # 只能靠翻日志才知道是 Argus 网关拦的（文案还写着误导性的 Uifid）。
            body = (resp.text or "").strip()
            detail = f"接口返回 HTTP {resp.status_code}"
            if body:
                detail += f"（{body[:80]}）"
            if "ArgusSecurityPlugin" in body:
                detail += (
                    "。这是边缘网关的业务前置校验：确认请求头 %s 已带上"
                    "（当前固定值 %r）。注意该文案里的「Uifid」是误导，"
                    "不必真去获取 Uifid。" % (ARGUS_HEADER, ARGUS_HEADER_VALUE)
                )
            raise HDUnavailable(detail)

        text = (resp.text or "").strip()
        if not text.startswith("{"):
            # 触发风控时返回空响应体
            raise HDUnavailable("接口返回空数据（可能已触发风控）")

        try:
            data = resp.json()
        except ValueError as exc:
            raise HDUnavailable(f"响应解析失败：{exc}") from exc

        detail = data.get("aweme_detail")
        if not detail:
            raise HDUnavailable("响应中缺少作品数据（可能已删除或需登录）")
        return detail


def _gear_resolution(gear_name: str) -> int:
    """从档位名中提取分辨率（短边像素），如 normal_720_0 -> 720。

    仅作兜底：档位名里的数字并不总是存在，也不总是可信，
    能拿到真实宽高时应优先用 `_short_side()`。
    """
    match = GEAR_RES_PATTERN.search(gear_name or "")
    return int(match.group(1)) if match else 0


def _short_side(play_addr: Any) -> int:
    """取播放地址里的**短边**像素作为分辨率档位，拿不到返回 0。

    2026-10-10 实测该作品 18 条 bit_rate：宽高**不在条目顶层**
    （`item["width"]` / `item["height"]` 都是 None），只存在于
    `play_addr.width` / `play_addr.height`：`normal_1080_0` -> 1920x1080。

    用短边而不是长边，是为了横竖屏口径统一（与小红书档位标签同一口径）：
    竖屏 1080x1920 与横屏 1920x1080 都应归为「1080P」，只按长边算竖屏会被
    误报成 1920P。
    """
    if not isinstance(play_addr, dict):
        return 0
    try:
        width = int(play_addr.get("width") or 0)
        height = int(play_addr.get("height") or 0)
    except (TypeError, ValueError):
        return 0
    if width <= 0 or height <= 0:
        return 0
    return min(width, height)


def extract_qualities(video: Dict[str, Any]) -> list:
    """从 video 中提取全部清晰度档位，按（分辨率, 码率）降序排列。

    返回：[{name, label, bit_rate, resolution, url, urls, codec, is_h265}]

    ★ 契约：返回列表的**第 0 项就是最高档**。前端不做二次排序，直接取 [0]
    作为默认选中项（见 static/index.html 的 optionHtml），所以这个序不能乱。
    """
    qualities = []
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        gear = item.get("gear_name") or ""
        play_addr = item.get("play_addr") or {}
        urls = _clean(play_addr.get("url_list"))
        if not urls:
            continue

        # 真实宽高优先，拿不到才退回档位名里的数字（见两个函数的 docstring）
        resolution = _short_side(play_addr) or _gear_resolution(gear)
        is_h265 = bool(item.get("is_h265") or item.get("is_bytevc1"))
        bit_rate = int(item.get("bit_rate") or 0)

        qualities.append({
            "name": gear,
            "label": _label(resolution, bit_rate, is_h265),
            "bit_rate": bit_rate,
            "resolution": resolution,
            "url": urls[0],
            "urls": urls,
            "codec": "h265" if is_h265 else "h264",
            "is_h265": is_h265,
        })

    # 分辨率优先，其次码率；h265 在同分辨率下码率更低但画质相当，故不单独降权
    qualities.sort(key=lambda x: (x["resolution"], x["bit_rate"]), reverse=True)

    # 同（分辨率 + 编码）只保留码率最高的一档，避免下拉框出现大量重复项
    seen = set()
    deduped = []
    for q in qualities:
        key = (q["resolution"], q["codec"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)
    return deduped


def _clean(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for u in raw:
        if isinstance(u, str) and u.startswith("http"):
            u = u.replace("playwm", "play").replace("/playwm/", "/play/")
            if u not in out:
                out.append(u)
    return out


def _label(resolution: int, bit_rate: int, is_h265: bool) -> str:
    if resolution:
        text = f"{resolution}P"
    else:
        text = "自适应"
    if is_h265:
        text += " (H265)"
    if bit_rate:
        text += f" · {round(bit_rate / 1000)}kbps"
    return text
