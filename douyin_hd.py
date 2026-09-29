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

GEAR_RES_PATTERN = re.compile(r"_(\d{3,4})_")


class HDUnavailable(Exception):
    """高清通道不可用（签名失败 / 风控 / 作品不可见），调用方应降级。"""


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

        headers = {
            "User-Agent": FIXED_UA,
            "Referer": "https://www.douyin.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.8",
        }
        ttwid = self._ensure_ttwid()
        if ttwid:
            headers["Cookie"] = f"ttwid={ttwid}"

        try:
            resp = self._session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise HDUnavailable(f"网络请求失败：{exc}") from exc

        if resp.status_code != 200:
            raise HDUnavailable(f"接口返回 HTTP {resp.status_code}")

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
    """从档位名中提取分辨率高度，如 normal_720_0 -> 720。"""
    match = GEAR_RES_PATTERN.search(gear_name or "")
    return int(match.group(1)) if match else 0


def extract_qualities(video: Dict[str, Any]) -> list:
    """从 video 中提取全部清晰度档位，按（分辨率, 码率）降序排列。

    返回：[{name, label, bit_rate, resolution, url, urls, codec, is_h265}]
    """
    qualities = []
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        gear = item.get("gear_name") or ""
        urls = _clean(((item.get("play_addr") or {}).get("url_list")))
        if not urls:
            continue

        resolution = _gear_resolution(gear)
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
