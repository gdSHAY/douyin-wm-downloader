# -*- coding: utf-8 -*-
"""
抖音无水印解析模块。

解析思路复用自开源项目 yzfly/douyin-mcp-server（MIT License）中验证过的链路：
    分享短链 -> 302 跟随 -> https://www.iesdouyin.com/share/video/<aweme_id>
             -> 正则提取 window._ROUTER_DATA -> JSON -> item_list[0]
该链路不需要 a_bogus/X-Bogus 签名，也不需要 Cookie，实测（2026-09）仍然有效。

在原有实现基础上做了以下扩展：
    1. 支持视频 / 图集（note）/ 实况照片（Live Photo：静态图 + 动态短视频）三种形态；
    2. 输出结构化元数据（作者、封面、音乐、互动数据、多 CDN 备源）；
    3. 多策略 URL 提取，兼容短链、长链、discover、分享口令文本；
    4. 无水印地址做 playwm->play 归一化，并保留全部备源用于失败重试；
    5. 图集取「无水印且分辨率最高」变体，实况照片额外输出动态视频（无水印）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import requests

from douyin_hd import HDClient, extract_qualities

# 高清通道客户端（模块级复用，共享 ttwid）
_hd_client = HDClient()

# 移动端 UA：短链跟随跳后会落到 iesdouyin share 页（返回内嵌 JSON），
# 而 PC UA 会跳到 www.douyin.com/video/（返回 JS 挑战页，无法直接解析）。
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1"
)

HEADERS = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}

SHARE_VIDEO_URL = "https://www.iesdouyin.com/share/video/{aweme_id}/"
SHARE_SLIDES_URL = "https://www.iesdouyin.com/share/slides/{aweme_id}/"
HOME_URL = "https://www.iesdouyin.com/"

# 复用同一会话：抖音 share 页首次访问只下发 ttwid，
# 携带该 Cookie 的第二次请求才会 SSR 输出完整作品数据。
_SESSION = requests.Session()

# 匹配 URL：标准 RFC 3986 允许的字符集，天然排除中文口令文字
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
ROUTER_DATA_PATTERN = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
# 长数字 ID（aweme_id 通常 19 位）
ID_PATTERN = re.compile(r"(\d{15,25})")

VIDEO_PAGE_KEY = "video_(id)/page"
NOTE_PAGE_KEY = "note_(id)/page"

ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


class ParseError(Exception):
    """解析失败，携带面向用户的中文提示。"""


def extract_first_url(text: str) -> str:
    """从抖音分享口令文本中提取第一个有效链接。"""
    if not text or not text.strip():
        raise ParseError("请输入抖音分享链接")
    match = URL_PATTERN.search(text)
    if not match:
        raise ParseError("未在内容中识别到链接，请复制完整的抖音分享文案后重试")
    return match.group(0).rstrip(".,;")


def _warm_session() -> None:
    """预热会话，确保拿到 ttwid Cookie。"""
    if "ttwid" in _SESSION.cookies:
        return
    try:
        _SESSION.get(HOME_URL, headers=HEADERS, timeout=15)
    except requests.RequestException:
        pass


def extract_aweme_id(url: str) -> str:
    """把任意形态的抖音链接解析为 aweme_id。

    短链（v.douyin.com/xxx）需要先跟随 302 跳转，用移动端 UA 才能落到 share 页。
    """
    _warm_session()
    if "v.douyin.com" in url:
        try:
            resp = _SESSION.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            url = resp.url
        except requests.RequestException as exc:
            raise ParseError(f"短链还原失败：{exc}") from exc

    # discover?modal_id=xxxxx
    modal = re.search(r"modal_id=(\d{15,25})", url)
    if modal:
        return modal.group(1)

    # 路径中的长数字 ID：取最长的一段，避免命中 uid 等其它数字
    candidates = ID_PATTERN.findall(url.split("?")[0])
    if candidates:
        return max(candidates, key=len)

    raise ParseError(f"无法从链接中识别作品 ID：{url}")


def fetch_router_data(aweme_id: str) -> Dict[str, Any]:
    """抓取 share 页并解析 window._ROUTER_DATA。

    同一地址最多请求两次：首次用于换取/补充 ttwid，第二次才会返回作品数据。
    """
    _warm_session()
    last_error = "获取作品信息失败"

    for template in (SHARE_VIDEO_URL, SHARE_SLIDES_URL):
        url = template.format(aweme_id=aweme_id)
        for attempt in range(2):
            try:
                resp = _SESSION.get(url, headers=HEADERS, timeout=20)
            except requests.RequestException as exc:
                last_error = f"网络请求失败：{exc}"
                break

            if resp.status_code != 200:
                last_error = f"页面返回 HTTP {resp.status_code}"
                break

            match = ROUTER_DATA_PATTERN.search(resp.text)
            if not match:
                last_error = "页面结构中未找到作品数据（可能已触发风控）"
                continue

            try:
                data = json.loads(match.group(1).strip())
            except json.JSONDecodeError as exc:
                last_error = f"作品数据解析失败：{exc}"
                continue

            if _has_item(data):
                return data
            last_error = "作品数据为空，可能已删除或设为私密"
            # 数据为空时再试一次（等待 Cookie 生效）

    raise ParseError(last_error)


def _has_item(router_data: Dict[str, Any]) -> bool:
    """判断 router_data 中是否已包含作品主体数据。"""
    loader = router_data.get("loaderData") or {}
    for node in loader.values():
        if not isinstance(node, dict):
            continue
        info = node.get("videoInfoRes") or node.get("videoInfo") or {}
        if info.get("item_list"):
            return True
    return _deep_find_item(router_data) is not None


def pick_item(router_data: Dict[str, Any]) -> Dict[str, Any]:
    """从 router_data 中取出作品主体 item。"""
    loader = router_data.get("loaderData") or {}

    for key in (VIDEO_PAGE_KEY, NOTE_PAGE_KEY):
        node = loader.get(key)
        if not node:
            continue
        info = node.get("videoInfoRes") or node.get("videoInfo") or {}
        items = info.get("item_list") or []
        if items:
            return items[0]

    # 兜底：递归搜索第一个含 aweme_id 的字典
    found = _deep_find_item(router_data)
    if found:
        return found

    raise ParseError("作品不存在、已删除，或作者设置了私密/仅好友可见")


def _deep_find_item(obj: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if "aweme_id" in obj and ("video" in obj or "images" in obj):
            return obj
        for value in obj.values():
            result = _deep_find_item(value, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = _deep_find_item(value, depth + 1)
            if result:
                return result
    return None


def _nowatermark(url: str) -> str:
    """把带水印的播放地址替换为无水印地址。"""
    if not url:
        return url
    return url.replace("playwm", "play").replace("/playwm/", "/play/")


def _clean_url_list(raw: Any) -> List[str]:
    """归一化地址列表：去水印 + 去重 + 保持顺序。"""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]

    result: List[str] = []
    for url in raw:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        url = _nowatermark(url)
        if url not in result:
            result.append(url)
    return result


# 抖音图集对同一张图提供多种变体，实测（1920×1273 原图）：
#   无水印 url_list        -> tplv-dy-aweme-images:q75     2044×1356  无水印
#   无水印 lqen-new        -> tplv-dy-lqen-new:...:q80     1920×1273  无水印
#   带水印 download_url_list -> tplv-dy-water-v2 / lqen-new-water  带“抖音号”水印
# 因此图集必须优先取无水印变体，绝不能沿用视频那套 download_addr 优先的思路。
_WATERMARK_MARKERS = ("dy-water", "-water", "watermark", "owner_watermark")


def _is_watermarked_url(url: str) -> bool:
    """按抖音图集 URL 的模板名判断是否为带水印变体（忽略查询串，避免签名误伤）。"""
    return any(marker in url.split("?", 1)[0].lower() for marker in _WATERMARK_MARKERS)


def _pick_image_url(image: Dict[str, Any]) -> str:
    """从单张图的全部变体中挑出「无水印且分辨率最高」的地址。

    优先级（参照开源实现 Evil0ctal / combodevy 的候选排序思路）：
        无水印下载 > origin_image 原图 > display_image > url_list > download_url
        > download_url_list(带水印) > owner_watermark_image
    同一字段内取最后一个（抖音把 jpeg 原图放在 webp 预览之后，实测分辨率最高）。
    """
    groups: tuple = (
        image.get("watermark_free_download_url_list"),
        (image.get("origin_image") or {}).get("url_list"),
        (image.get("display_image") or {}).get("url_list"),
        image.get("url_list"),
        [image.get("download_url")],
        image.get("download_url_list"),
        (image.get("owner_watermark_image") or {}).get("url_list"),
    )

    watermarked: List[str] = []
    for group in groups:
        urls = _clean_url_list(group)
        if not urls:
            continue
        clean = [u for u in urls if not _is_watermarked_url(u)]
        if clean:
            return clean[-1]
        watermarked.extend(urls)
    # 极端兜底：全部变体都带水印时，仍给出可用地址
    return watermarked[-1] if watermarked else ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# 实况照片（Live Photo）= 一张静态图 + 一段约 2~3 秒的动态短视频，
# 动态部分挂在 images[i].video 下（仅高清详情通道返回，分享页不返回）。
# 实测（2026-09，aweme_type=68 / is_live_photo=1 / live_photo_type=1 / clip_type=5）：
#   video.play_addr          720×962  2.55s  无水印   <- 取这个
#   video.download_addr      720×962  5.57s  带“抖音号”片尾卡（多出约 3s 水印动画）
# 与主视频同理：play_addr 是无水印播放地址，download_addr 才是带水印下载版。
def _pick_live_video(image: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取实况照片的动态视频（无水印），非实况或不可用时返回 None。"""
    video = image.get("video")
    if not isinstance(video, dict) or not video:
        return None

    qualities = [q for q in extract_qualities(video) if q.get("url")]
    if qualities:
        best = qualities[0]
        urls = list(best.get("urls") or [best["url"]])
        label = best.get("label") or ""
        codec = best.get("codec") or "h264"
        is_h265 = bool(best.get("is_h265"))
        bit_rate = best.get("bit_rate") or 0
    else:
        # 少数作品没有 bit_rate，退回 play_addr（同样是无水印变体）
        urls = _clean_url_list((video.get("play_addr") or {}).get("url_list"))
        if not urls:
            return None
        label = "默认"
        is_h265 = bool(video.get("is_h265") or video.get("is_bytevc1"))
        codec = "h265" if is_h265 else "h264"
        bit_rate = 0

    duration_ms = _safe_int(video.get("duration") or 0)
    if duration_ms > 3_600_000:  # 异常大值按微秒兜底
        duration_ms //= 1000

    return {
        "url": urls[0],
        "urls": urls,
        "label": label,
        "codec": codec,
        "is_h265": is_h265,
        "bit_rate": bit_rate,
        "width": _safe_int(video.get("width")),
        "height": _safe_int(video.get("height")),
        "duration": round(duration_ms / 1000, 2),
        "size": _safe_int((video.get("play_addr") or {}).get("data_size")),
    }


def _clean_filename(name: str, fallback: str = "douyin") -> str:
    name = ILLEGAL_FILENAME.sub("_", (name or "").strip())
    name = name[:60].strip(" .")
    return name or fallback


def normalize(item: Dict[str, Any], aweme_id: str, qualities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把原始 item 归一化成前端需要的结构。

    qualities 由高清通道提供；为空时退回 play_addr 默认档。
    """
    video = item.get("video") or {}
    music = item.get("music") or {}
    author = item.get("author") or {}
    stats = item.get("statistics") or {}
    cover = video.get("cover") or video.get("origin_cover") or item.get("cover") or {}

    video_urls = _clean_url_list(((video.get("play_addr") or {}).get("url_list")))
    # 部分作品 play_addr 为空，退回到 download_addr
    if not video_urls:
        video_urls = _clean_url_list((video.get("download_addr") or {}).get("url_list"))

    # 清晰度档位：优先取高清通道提供的多档，否则用默认档兜底
    quality_list = [q for q in (qualities or []) if q.get("url")]
    if not quality_list and video_urls:
        quality_list = [{
            "name": "default",
            "label": "默认",
            "bit_rate": 0,
            "resolution": 0,
            "url": video_urls[0],
            "urls": video_urls,
            "codec": "h264",
            "is_h265": False,
        }]
    # 默认播放/下载地址取最高档
    if quality_list:
        video_urls = quality_list[0].get("urls") or [quality_list[0]["url"]]

    images: List[str] = []
    image_items: List[Dict[str, Any]] = []
    live_videos: List[Dict[str, Any]] = []
    for index, image in enumerate(item.get("images") or [], start=1):
        if not isinstance(image, dict):
            continue
        url = _pick_image_url(image)
        # 实况照片的动态部分：分享页不返回该字段，故只在高清详情通道可用
        live = _pick_live_video(image)
        if not url and not live:
            continue
        if url:
            images.append(url)
        entry: Dict[str, Any] = {
            "index": index,
            "url": url,
            "width": _safe_int(image.get("width")),
            "height": _safe_int(image.get("height")),
            "watermark": _is_watermarked_url(url) if url else False,
        }
        if live:
            entry["live"] = live
            live_videos.append({"index": index, **live})
        image_items.append(entry)

    # 抖音该字段为毫秒（实测长短视频一致）。仅当数值离谱（超过 24 小时）
    # 才按微秒兜底，避免把十几分钟的长视频误算成不到 1 秒。
    duration_ms = _safe_int(video.get("duration") or item.get("duration") or 0)
    if duration_ms > 86_400_000:
        duration_ms //= 1000

    cover_urls = _clean_url_list(cover.get("url_list")) if isinstance(cover, dict) else []
    music_urls = _clean_url_list((music.get("play_url") or {}).get("url_list"))

    desc = (item.get("desc") or "").strip()

    return {
        "aweme_id": item.get("aweme_id") or aweme_id,
        "type": "images" if images else "video",
        "title": desc or f"抖音作品_{aweme_id}",
        "filename": _clean_filename(desc, f"抖音作品_{aweme_id}"),
        "desc": desc,
        "cover": cover_urls[0] if cover_urls else "",
        "duration": round(duration_ms / 1000, 1),
        "create_time": _safe_int(item.get("create_time")),
        "author": {
            "nickname": author.get("nickname") or "",
            "uid": author.get("uid") or "",
            "sec_uid": author.get("sec_uid") or "",
            "avatar": _clean_url_list(
                ((author.get("avatar_larger") or author.get("avatar_thumb") or {}).get("url_list"))
            ),
        },
        "author_avatar": (
            _clean_url_list(
                ((author.get("avatar_larger") or author.get("avatar_thumb") or {}).get("url_list"))
            )
            or [""]
        )[0],
        "music": {
            "title": music.get("title") or "",
            "author": music.get("author") or "",
            "url": music_urls[0] if music_urls else "",
        },
        "stats": {
            "digg": _safe_int(stats.get("digg_count")),
            "comment": _safe_int(stats.get("comment_count")),
            "share": _safe_int(stats.get("share_count")),
            "collect": _safe_int(stats.get("collect_count")),
            "play": _safe_int(stats.get("play_count")),
        },
        "video_urls": video_urls,
        "images": images,
        "image_items": image_items,
        "live_photo": bool(live_videos),
        "live_videos": live_videos,
        "qualities": quality_list,
        "hd": bool(qualities),
    }


def parse(text: str, prefer_hd: bool = True) -> Dict[str, Any]:
    """入口：传入分享文本或链接，返回归一化作品信息。

    优先走高清通道（a_bogus 签名，可拿到多档清晰度）；
    该通道失败时自动降级到 share 页方案（无需签名，最高 720p）。
    """
    url = extract_first_url(text)
    aweme_id = extract_aweme_id(url)

    qualities: Optional[List[Dict[str, Any]]] = None
    item: Optional[Dict[str, Any]] = None
    hd_error = ""

    if prefer_hd:
        try:
            detail = _hd_client.fetch_detail(aweme_id)
            qualities = extract_qualities(detail.get("video") or {})
            # 图集（note）没有 bit_rate，但详情接口的图集地址分辨率最高
            # （实测同一张图：详情 url_list 2044×1356，分享页仅 1920×1273），
            # 因此只要拿到图集就沿用详情结果，不再降级到分享页。
            if qualities or detail.get("images"):
                item = detail
        except Exception as exc:  # 签名或风控失败 -> 降级
            hd_error = str(exc)
            item = None

    if item is None:
        router_data = fetch_router_data(aweme_id)
        item = pick_item(router_data)

    data = normalize(item, aweme_id, qualities)
    data["source_url"] = url
    if hd_error and not data.get("hd"):
        data["hd_error"] = hd_error
        # 分享页既不返回实况动态视频，也没有 is_live_photo 标识，
        # 因此降级后无法判断是否遗漏了动态部分，前端据此给出提示。
        if data.get("type") == "images":
            data["live_maybe_missing"] = True
    return data


if __name__ == "__main__":
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/L4FJNR3/"
    print(json.dumps(parse(sample), ensure_ascii=False, indent=2))
