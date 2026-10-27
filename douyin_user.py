# -*- coding: utf-8 -*-
"""
抖音用户主页解析 + 本地「关注列表」存储。

能力边界（2026-10-27 实测确认）：

    · 用户资料    /aweme/v1/web/user/profile/other/   ✓ 昵称/签名/粉丝数/作品数
    · 作品列表    /aweme/v1/web/aweme/post/          ✓ 游标翻页（count 实测生效）
    · 关注列表    本机 follows.json                    ✓ 原子写、去重、备注、新作品计数

两个接口都要 `a_bogus` 签名（复用 douyin_abogus）与 `x-tt-argus` 请求头
（复用 douyin_hd.detail_headers）—— 与高质量详情接口同一套网关。

⚠️ 主页列表接口与详情接口的**字段差异**（实测，2026-10-27）：
    · 视频条目带 `video.bit_rate`（15~18 档），可直接出多档清晰度，无需再查详情；
    · **图集条目的 `video.play_addr` 指向背景音乐 mp3，不是视频**
      （实测 `lf26-music-east.douyinstatic.com/...mp3`，Content-Type: audio/mpeg）。
      若照搬详情接口的解析路径，前端会把「一首 mp3」渲染成可下载的视频。
      因此 `normalize_post()` 对图集条目清空 video_urls / qualities。
    · 列表不返回 `video.duration`（恒为 0），时长只有详情接口才有。

⚠️ TikTok / 小红书 的主页作品列表**不在本模块支持范围内**：
    实测 TikTok 的 /api/post/item_list/ 不带 X-Bogus 时返回 **HTTP 200 + 空 body**
    （风控静默丢弃），主页 SSR 的 `webapp.user-detail.itemList` 也是空数组。
    小红书同理需要 x-s 签名。要支持它们得引入一整套 JS 签名算法，属于另一个量级的
    工程，故本期只做抖音，并由上层给出明确提示，而不是让用户对着「解析失败」猜。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests

import runtime_paths
from douyin_abogus import ABogus
from douyin_hd import BASE_PARAMS, HDClient, detail_headers, extract_qualities
from douyin_parser import HEADERS, normalize

#: 主页作品列表（游标翻页）
POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
#: 用户资料
PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"

#: 单页条数。实测 18 最稳（官方 Web 端也是这个值）；传 3 也确实只回 3 条。
PAGE_SIZE = 18
#: 单页上限：接口对 count 有自己的截断，这里只做防御性收敛。
PAGE_MAX = 50

#: 一次「加载全部」最多翻多少页 —— 防止遇到异常 has_more 时无限循环。
MAX_PAGES = 30

#: 首屏缓存时长（秒）。翻页与「重新打开同一个用户」时不重复打接口。
CACHE_TTL = 120
CACHE_MAX = 60

_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

_SESSION = requests.Session()
_CLIENT = HDClient()

#: sec_uid 形如 MS4wLjABAAAAxxxx（长度约 55）。宽松匹配，避免抖音换前缀后失效。
SEC_UID_PATTERN = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_\-]+")
#: 主页路径 /user/<sec_uid>；`self` 这类短段天然不会被匹配（至少 16 位）
USER_PATH_PATTERN = re.compile(r"/user/([A-Za-z0-9_\-]{16,})")
#: 从任意文本里捞第一个链接（与 douyin_parser 同口径，但这里不 import 私有常量）
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


class UserError(Exception):
    """用户主页解析 / 关注列表操作失败，携带面向用户的中文提示。"""


# ---------------------------------------------------------------- 链接识别


def _sec_from_url(url: str) -> str:
    """从一条 URL 里取 sec_uid：查询参数优先，其次 /user/<sec_uid> 路径。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""

    values = parse_qs(parsed.query or "").get("sec_uid") or []
    for value in values:
        if SEC_UID_PATTERN.fullmatch(value or ""):
            return value

    match = USER_PATH_PATTERN.search(parsed.path or "")
    if match:
        return match.group(1)
    return ""


def looks_like_user(text: str) -> bool:
    """这条内容看起来是不是「用户主页」链接（而不是单个作品）。

    只做形态判断，不联网。前端用它决定走 /api/user/parse 还是 /api/parse。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    if SEC_UID_PATTERN.fullmatch(raw):
        return True
    match = URL_PATTERN.search(raw)
    url = match.group(0) if match else raw
    if _sec_from_url(url):
        return True
    return "/share/user/" in url


def extract_sec_uid(text: str) -> str:
    """把任意形态的用户主页链接/分享文案解析成 sec_uid。

    支持：
        1. https://www.douyin.com/user/MS4wLjABAAAA...
        2. https://www.iesdouyin.com/share/user/<uid>?sec_uid=MS4w...
        3. 主页短链（v.douyin.com/xxxxx）—— 跟随 302 后再取
        4. 直接粘贴一段 sec_uid
    """
    raw = (text or "").strip()
    if not raw:
        raise UserError(
            "请输入抖音用户主页链接（形如 https://www.douyin.com/user/MS4wLjABAAAA...）"
        )

    # 情形 4：裸 sec_uid
    if SEC_UID_PATTERN.fullmatch(raw):
        return raw

    match = URL_PATTERN.search(raw)
    url = match.group(0).rstrip(".,;") if match else raw

    # 情形 1 / 2：链接里直接带
    sec = _sec_from_url(url)
    if sec:
        return sec

    # 情形 3：短链 / 分享页。用移动端 UA 跟随跳转，落点通常是 share/user 页。
    #
    # ⚠️ 只对这两类地址发请求：拿一条作品长链（douyin.com/video/xxx）来跟 302
    #    纯属白跑一趟，而且会把作品页当成主页、报一句让人更糊涂的错。
    is_short = bool(re.search(r"(v\.douyin\.com|/share/user/)", url))
    if not is_short:
        if "douyin.com" not in url:
            raise UserError("这不是抖音链接，无法解析用户主页：%s" % url)
        raise UserError(
            "没能从这条链接里认出用户主页。"
            "请用浏览器打开该作者主页后复制地址栏链接（应含 /user/MS4wLjABAAAA...）；"
            "单个作品请直接粘贴作品链接，不需要走用户主页。"
        )

    try:
        resp = _SESSION.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        raise UserError("打开该链接失败：%s" % exc) from exc

    sec = _sec_from_url(resp.url) or _sec_from_url(resp.text[:200000])
    if sec:
        return sec

    raise UserError(
        "这条短链没有落到用户主页（可能已经失效，或它本来就是单个作品的分享链接）。"
        "请用浏览器打开该作者主页后复制地址栏链接（应含 /user/MS4wLjABAAAA...）。"
    )


# ---------------------------------------------------------------- 接口调用


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _signed_get(api: str, params: Dict[str, Any], referer: str) -> Dict[str, Any]:
    """带 a_bogus 签名与网关请求头的一次 GET，返回 JSON。

    失败一律转成 UserError —— 上层只在 server.py 里统一转前端文案，
    这里必须把「HTTP 状态 / 响应长度 / 网关关键字」写进原因，否则线上排查
    只能看到一句没有信息量的「解析失败」。
    """
    try:
        a_bogus = ABogus().get_value(params)
    except Exception as exc:  # noqa: BLE001 - 签名算法异常种类不确定
        raise UserError("生成请求签名失败：%s" % exc) from exc

    url = "%s?%s&a_bogus=%s" % (api, urlencode(params), quote(a_bogus, safe=""))
    headers = detail_headers(_CLIENT.ttwid())
    headers["Referer"] = referer

    try:
        resp = _SESSION.get(url, headers=headers, timeout=25)
    except requests.RequestException as exc:
        raise UserError("请求抖音接口失败：%s" % exc) from exc

    body = (resp.text or "").strip()
    if resp.status_code != 200:
        detail = "抖音接口返回 HTTP %d（响应 %d 字节）" % (resp.status_code, len(body))
        if "ArgusSecurityPlugin" in body:
            detail += "。这是边缘网关的前置校验，通常是本机出口被风控，稍后或换网络重试"
        raise UserError(detail)

    if not body:
        # 风控的典型表现：HTTP 200 + 空 body（TikTok 那边也一样）
        raise UserError("抖音接口返回空数据（本机出口可能被风控，稍后重试或换个网络）")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise UserError("抖音接口返回的不是 JSON（%d 字节），可能被风控了" % len(body)) from exc

    if not isinstance(payload, dict):
        raise UserError("抖音接口返回了非预期的数据结构")

    status = payload.get("status_code")
    if status not in (0, None):
        raise UserError(
            "抖音拒绝了这次请求（status_code=%s）。可能是该用户已注销、作品仅自己可见，"
            "或本机出口被风控" % status
        )
    return payload


def fetch_profile(sec_uid: str) -> Dict[str, Any]:
    """取用户资料（昵称、签名、头像、粉丝/作品数）。"""
    params = dict(BASE_PARAMS)
    params["sec_user_id"] = sec_uid
    params["publish_video_strategy_type"] = "2"

    payload = _signed_get(PROFILE_API, params, "https://www.douyin.com/user/%s" % sec_uid)
    user = payload.get("user") or {}
    if not user:
        raise UserError("没能取到该用户的资料（可能已注销、被封禁，或链接里的 ID 不完整）")

    avatar = (user.get("avatar_larger") or user.get("avatar_thumb") or {}).get("url_list") or []
    real_sec = user.get("sec_uid") or sec_uid
    return {
        "platform": "douyin",
        "sec_uid": real_sec,
        "uid": str(user.get("uid") or ""),
        "nickname": (user.get("nickname") or "").strip() or "抖音用户",
        "unique_id": (user.get("unique_id") or "").strip(),
        "signature": (user.get("signature") or "").strip(),
        "avatar": avatar[0] if avatar else "",
        "follower_count": _int(user.get("follower_count")),
        "following_count": _int(user.get("following_count")),
        "total_favorited": _int(user.get("total_favorited")),
        "aweme_count": _int(user.get("aweme_count")),
        "url": "https://www.douyin.com/user/%s" % real_sec,
    }


def normalize_post(item: Dict[str, Any]) -> Dict[str, Any]:
    """把主页列表接口的一条作品归一化成前端既有契约。

    直接复用 `douyin_parser.normalize`，保证「主页列表」与「单条解析」两条
    链路产出的字段完全一致 —— 前端只需要一套渲染逻辑。
    """
    aweme_id = str(item.get("aweme_id") or "")
    images = item.get("images") or []
    video = item.get("video") or {}

    qualities = [] if images else extract_qualities(video)
    data = normalize(item, aweme_id, qualities)

    if images:
        # 见模块头注释：图集条目的 video.play_addr 是背景音乐 mp3，必须清掉
        data["video_urls"] = []
        data["qualities"] = []
        data["duration"] = 0
        if not data.get("cover") and data.get("images"):
            data["cover"] = data["images"][0]

    data["is_top"] = bool(item.get("is_top"))
    data["share_url"] = "https://www.douyin.com/video/%s" % aweme_id
    return data


def _cache_get(sec_uid: str, cursor: str) -> Optional[Dict[str, Any]]:
    hit = _CACHE.get((sec_uid, cursor))
    if not hit:
        return None
    if time.time() - hit[0] > CACHE_TTL:
        _CACHE.pop((sec_uid, cursor), None)
        return None
    return hit[1]


def _cache_put(sec_uid: str, cursor: str, page: Dict[str, Any]) -> None:
    if len(_CACHE) >= CACHE_MAX:
        now = time.time()
        for key in [k for k, v in _CACHE.items() if now - v[0] > CACHE_TTL]:
            _CACHE.pop(key, None)
        while len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[(sec_uid, cursor)] = (time.time(), page)


def fetch_posts(
    sec_uid: str, cursor: str = "0", count: int = PAGE_SIZE, use_cache: bool = True
) -> Dict[str, Any]:
    """取一页主页作品。返回 {posts, cursor, has_more}。

    cursor 是**游标**而不是页码：首屏传 "0"，后续传上一页返回的 cursor，
    绝不能自行 +1（会拿到重复数据或直接触发校验）。
    """
    cursor = str(cursor or "0")
    cached = _cache_get(sec_uid, cursor) if use_cache else None
    if cached is not None:
        return cached

    try:
        size = int(count)
    except (TypeError, ValueError):
        size = PAGE_SIZE
    size = max(1, min(size, PAGE_MAX))

    params = dict(BASE_PARAMS)
    params.update({
        "sec_user_id": sec_uid,
        "max_cursor": cursor,
        "count": str(size),
        "publish_video_strategy_type": "2",
    })

    payload = _signed_get(POST_API, params, "https://www.douyin.com/user/%s" % sec_uid)
    items = payload.get("aweme_list") or []
    posts = [normalize_post(item) for item in items if isinstance(item, dict)]

    page = {
        "posts": posts,
        "cursor": str(payload.get("max_cursor") or 0),
        "has_more": bool(payload.get("has_more")),
    }
    if use_cache:
        _cache_put(sec_uid, cursor, page)
    return page


def parse_user(
    text: str, cursor: str = "", count: int = PAGE_SIZE, pages: int = 1
) -> Dict[str, Any]:
    """用户主页入口：链接 -> {user, posts, cursor, has_more}。

    `pages` > 1 时连续翻页（「一次性加载全部」用），最多 MAX_PAGES 页。
    """
    sec_uid = extract_sec_uid(text)
    user = fetch_profile(sec_uid)

    posts: List[Dict[str, Any]] = []
    next_cursor = str(cursor or "0")
    has_more = True

    for _ in range(max(1, min(int(pages or 1), MAX_PAGES))):
        page = fetch_posts(sec_uid, next_cursor, count)
        posts.extend(page["posts"])
        next_cursor = page["cursor"]
        has_more = page["has_more"]
        if not has_more:
            break

    # 置顶作品在翻页时会重复出现，按 aweme_id 去重（保留首次出现位置）
    seen = set()
    unique: List[Dict[str, Any]] = []
    for post in posts:
        if post.get("aweme_id") in seen:
            continue
        seen.add(post.get("aweme_id"))
        unique.append(post)

    return {
        "user": user,
        "posts": unique,
        "cursor": next_cursor,
        "has_more": has_more,
    }


# ---------------------------------------------------------------- 关注列表


#: 关注列表上限。纯本机 JSON，不做分页，太大只会让首屏变慢。
FOLLOW_LIMIT = 300


class FollowStore:
    """本机「关注列表」持久化。

    为什么放 JSON 而不是数据库：这份数据只有一份、单人使用、上限 300 条，
    引入 SQLite 只会多一个「数据库文件损坏怎么修」的运维问题。
    写入用「临时文件 + os.replace」保证原子性 —— 断电时不会留下半个 JSON
    让整个关注列表读不出来。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or runtime_paths.config_path("follows.json")
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"version": 1, "users": []}
        if not isinstance(data, dict):
            return {"version": 1, "users": []}
        users = data.get("users")
        if not isinstance(users, list):
            users = []
        return {
            "version": 1,
            "users": [u for u in users if isinstance(u, dict) and u.get("sec_uid")],
        }

    def _write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # ---- 读

    def listing(self) -> List[Dict[str, Any]]:
        with self._lock:
            users = self._read()["users"]
        users.sort(key=lambda u: (_int(u.get("added_at")), u.get("sec_uid") or ""), reverse=True)
        return users

    def find(self, sec_uid: str) -> Optional[Dict[str, Any]]:
        for user in self.listing():
            if user.get("sec_uid") == sec_uid:
                return user
        return None

    # ---- 写

    def upsert(self, user: Dict[str, Any], note: Optional[str] = None) -> Dict[str, Any]:
        sec_uid = (user.get("sec_uid") or "").strip()
        if not sec_uid:
            raise UserError("保存关注失败：缺少用户 ID")

        with self._lock:
            data = self._read()
            users = data["users"]
            now = int(time.time())

            for index, old in enumerate(users):
                if old.get("sec_uid") != sec_uid:
                    continue
                merged = dict(old)
                # 空值不覆盖旧值：主页偶尔会返回空签名/空昵称，不该把已有资料抹掉
                for key, value in user.items():
                    if value not in (None, "", 0):
                        merged[key] = value
                if note is not None:
                    merged["note"] = note
                merged["updated_at"] = now
                users[index] = merged
                self._write(data)
                return merged

            if len(users) >= FOLLOW_LIMIT:
                raise UserError(
                    "关注列表已满（上限 %d 个），请先在列表里移除一些再添加" % FOLLOW_LIMIT
                )

            record = dict(user)
            record["note"] = note if note is not None else ""
            record["added_at"] = now
            record["updated_at"] = now
            record["last_aweme_id"] = record.get("last_aweme_id") or ""
            record["new_count"] = 0
            record["checked_at"] = 0
            users.append(record)
            self._write(data)
            return record

    def remove(self, sec_uid: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data["users"])
            data["users"] = [u for u in data["users"] if u.get("sec_uid") != sec_uid]
            if len(data["users"]) == before:
                return False
            self._write(data)
            return True

    def set_note(self, sec_uid: str, note: str) -> bool:
        with self._lock:
            data = self._read()
            for index, user in enumerate(data["users"]):
                if user.get("sec_uid") != sec_uid:
                    continue
                user["note"] = (note or "").strip()[:120]
                data["users"][index] = user
                self._write(data)
                return True
            return False

    def mark_checked(
        self, sec_uid: str, profile: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """记录一次「已查看」：刷新资料快照，并把新作品计数清零。"""
        with self._lock:
            data = self._read()
            for index, user in enumerate(data["users"]):
                if user.get("sec_uid") != sec_uid:
                    continue
                if profile:
                    for key in ("nickname", "avatar", "signature", "follower_count", "aweme_count"):
                        value = profile.get(key)
                        if value not in (None, "", 0):
                            user[key] = value
                user["new_count"] = 0
                user["checked_at"] = int(time.time())
                data["users"][index] = user
                self._write(data)
                return user
            return None

    def record_first_post(self, sec_uid: str, aweme_id: str) -> None:
        """记住「上次看到的第一条作品」，作为下次判断有没有更新的基线。"""
        if not aweme_id:
            return
        with self._lock:
            data = self._read()
            for index, user in enumerate(data["users"]):
                if user.get("sec_uid") != sec_uid:
                    continue
                user["last_aweme_id"] = aweme_id
                data["users"][index] = user
                self._write(data)
                return

    def set_new_count(self, sec_uid: str, count: int) -> bool:
        """只更新「新作品数」与检查时间，**不动基线**。

        基线（last_aweme_id）只由「用户真的打开了这个主页」来推进 —— 否则每
        检查一次就把角标清零，用户永远看不到「有新作品」。
        """
        with self._lock:
            data = self._read()
            for index, user in enumerate(data["users"]):
                if user.get("sec_uid") != sec_uid:
                    continue
                user["new_count"] = max(0, _int(count))
                user["checked_at"] = int(time.time())
                data["users"][index] = user
                self._write(data)
                return True
            return False


#: 进程级默认实例（server.py 直接用这个）
STORE = FollowStore()
