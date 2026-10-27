# -*- coding: utf-8 -*-
"""B站（bilibili）解析：自研 WBI 签名，直连官方接口，不依赖任何第三方库。

本模块的每条关键结论都来自 2026-09-19 的真实接口实测，非文档推测：

1. **WBI 签名**稳定可用，约 30 行即可复现：
   `/x/web-interface/nav` → `wbi_img.img_url / sub_url` 取文件名得 img_key + sub_key
   → 拼接后按 64 位表重排取前 32 位 = mixin_key
   → 参数加 `wts`、按 key 排序、剔除 `!'()*`、urlencode
   → `md5(query + mixin_key)` 即 `w_rid`。密钥每日轮换，进程内缓存即可。

2. **未登录清晰度上限 = 480P（实测）**。接口的 `accept_quality` 会「谎报」支持
   `[120, 116, 80, 64, 32, 16]`（含 4K），自报 `quality=64`（720P），
   但 `dash.video` 里实际只有 `qn=32`(480P) 与 `qn=16`(360P)。
   所以：**一切档位以 `dash.video` 实际存在的 id 为准**，绝不信任
   `accept_quality` / `support_formats` / `data.quality`。
   1080P 需登录（SESSDATA），1080P+ / 4K / 8K / HDR / 杜比需大会员。

3. **风控**：短时间内的突发请求会让 `playurl` 返回 `code=-412 request was banned`，
   且不是立刻解除。故本模块内置：先取 buvid3 指纹 + 生成 `bili_ticket`、
   全局请求限速、`-412` 退避重试（并刷新指纹）。

4. **高清恒为 DASH**（音视频分离），合流必须 ffmpeg `-c copy`（见 server.py）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlencode

import requests

import runtime_paths

API = "https://api.bilibili.com"
WEB = "https://www.bilibili.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# WBI mixin_key 的 64 位重排表（公开算法）
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

QUALITY_NAMES = {
    6: "240P 极速",
    16: "360P 流畅",
    32: "480P 清晰",
    64: "720P 高清",
    74: "720P60 高帧率",
    80: "1080P 高清",
    100: "智能修复",
    112: "1080P+ 高码率",
    116: "1080P60 高帧率",
    120: "4K 超清",
    125: "HDR 真彩",
    126: "杜比视界",
    127: "8K 超高清",
}

AUDIO_NAMES = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}

# 同一档位的编码优先级：优先选兼容性最好的（avc1 到处能播，av01 播放器支持最差）
CODEC_PRIORITY = ("avc1", "hvc1", "hev1", "av01")

# 登录态之下仍能拿到的档位上限：用于判断「未配置 SESSDATA」的提示
FREE_MAX_QN = 32

BV_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_PATTERN = re.compile(r"av(\d+)", re.IGNORECASE)
BILI_HOST_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|acg\.tv|biligame\.com)", re.IGNORECASE)
WEB_PAGE_PATTERN = re.compile(r"[?&]p=(\d+)")

# 全局限速：B站 风控对突发请求极敏感（实测十余次请求即 -412）
_THROTTLE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MIN_INTERVAL = 1.2


class BiliError(Exception):
    """B站解析失败，message 面向最终用户，可直接展示。"""


def is_bili_text(text: str) -> bool:
    """判断一段文本是否属于 B站（含纯 BV 号）。"""
    if not text:
        return False
    return bool(BILI_HOST_PATTERN.search(text) or BV_PATTERN.search(text))


def extract_bvid(text: str) -> Optional[str]:
    match = BV_PATTERN.search(text or "")
    return match.group(1) if match else None


def extract_aid(text: str) -> Optional[int]:
    match = AV_PATTERN.search(text or "")
    return int(match.group(1)) if match else None


def extract_page(text: str) -> int:
    """从链接里取 `?p=3` 这种分P参数，默认第 1 P。"""
    match = WEB_PAGE_PATTERN.search(text or "")
    return int(match.group(1)) if match else 1


def resolve_short_link(text: str) -> str:
    """把 b23.tv 短链跟随 302 还原成长链（BV 号只在长链里）。"""
    match = re.search(r"https?://b23\.tv/[A-Za-z0-9]+", text or "")
    if not match:
        return text
    try:
        resp = requests.get(
            match.group(0),
            headers={"User-Agent": UA},
            allow_redirects=True,
            timeout=15,
        )
        return resp.url or text
    except requests.RequestException:
        return text


def _throttle() -> None:
    """全局串行 + 最小间隔，避免把 B站 风控打爆。"""
    with _THROTTLE_LOCK:
        wait = MIN_INTERVAL - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()


def _codec_rank(track: Dict[str, Any]) -> Tuple[int, int]:
    """排序用：编码优先级靠前更优，同编码取码率更高者。"""
    codecs = str(track.get("codecs") or "")
    rank = len(CODEC_PRIORITY)
    for index, prefix in enumerate(CODEC_PRIORITY):
        if codecs.startswith(prefix):
            rank = index
            break
    return rank, -(track.get("bandwidth") or 0)


def _normalize_sessdata(raw: str) -> str:
    """把用户从浏览器里复制来的 SESSDATA 归一化成可直接用的值。

    实测常见的三种「复制走形」，都在这里兜住：
    1. 从 Network 面板的请求头里复制 → 值被 **URL 编码**（`,` 变成 `%2C`）→ 需 `unquote`；
    2. 连同 cookie 名一起复制 → 形如 `SESSDATA=xxxx` → 去掉前缀；
    3. 外面套了引号 / 带了多余空白 → 去掉。
    """
    value = (raw or "").strip().strip('"').strip("'").strip()
    if value[:9].upper() == "SESSDATA=":
        value = value[9:].strip()
    # 原生的 SESSDATA 由 base64 字符 + `,` + `*` 组成，正常不含 `%`；
    # 一旦出现 `%` 几乎必然是 URL 编码残留，解一次即可。
    if "%" in value:
        value = unquote(value).strip()
    return value


CONFIG_FILENAME = "bili_config.json"
# 打包成 exe 后必须落到**可写目录**：写进 PyInstaller 的解包目录等于
# 「用户填完 cookie，一重启就丢」（详见 runtime_paths.py）。
CONFIG_PATH = runtime_paths.config_path(CONFIG_FILENAME)

# 浏览器插件导出的 cookies.txt 里，真正值得保留的条目。
#   前 5 个：决定「能看多清晰」和「是谁」；
#   后 6 个：设备指纹，带上下载时可省掉一次指纹请求，并显著降低 -412 概率。
KEEP_COOKIES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "b_nut",
    "buvid_fp",
    "LIVE_BUVID",
    "_uuid",
)


def _mask(value: str) -> str:
    """打码：只露头尾，用于界面回显「填的是哪一个」。绝不在任何接口返回明文。"""
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "*" * max(len(value) - 2, 0)
    return f"{value[:4]}…{value[-4:]}"


def parse_netscape_cookies(text: str) -> Dict[str, str]:
    """解析 Netscape / curl 格式的 cookies.txt。

    「Get cookies.txt LOCALLY」这类插件导出的就是这个格式，每行 7 个 TAB 分隔字段：
        domain  flag  path  secure  expires  name  value

    两个必须处理的细节，否则最容易在 SESSDATA 上翻车：

    1. **`#HttpOnly_` 开头的行是真实 cookie，不是注释**。而 SESSDATA 恰恰常被标记
       HttpOnly —— 直接 `startswith("#")` 跳过，就会精准地把最关键的那个扔掉。
    2. 分隔符理论上固定是 TAB，但用户手工编辑后可能退化成空格 →
       TAB 切不够 7 段时，退回按空白切分（`maxsplit=6` 保住 value 里的空格）。
    """
    found: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        candidate = line
        if candidate.startswith("#HttpOnly_"):
            candidate = candidate[len("#HttpOnly_"):]
        elif candidate.lstrip().startswith("#"):
            continue  # 真正的注释行

        fields = candidate.split("\t")
        if len(fields) < 7:
            fields = re.split(r"\s+", candidate.strip(), maxsplit=6)
        if len(fields) < 7:
            continue

        domain, _flag, _path, _secure, _expires, name, value = fields[:7]
        if "bilibili.com" not in domain.lower():
            continue
        name = name.strip()
        if name in KEEP_COOKIES and value.strip():
            found[name] = value.strip()
    return found


def parse_cookie_input(text: str) -> Dict[str, str]:
    """把用户粘贴的内容解析成 cookie 字典。四种粘贴习惯都要能接住：

    1. 整个 `cookies.txt`（插件导出，最推荐）—— 含 `#HttpOnly_` 行；
    2. 请求头里的 `SESSDATA=xxx; bili_jct=yyy`（一行或分多行）；
    3. 单行 `SESSDATA=xxx`；
    4. 光秃秃的一个 SESSDATA 值。
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    # 1) Netscape cookies.txt
    net = parse_netscape_cookies(raw)
    if net.get("SESSDATA"):
        net["SESSDATA"] = _normalize_sessdata(net["SESSDATA"])
        return net

    # 2/3) `k=v` 形式（分号或换行分隔）
    found: Dict[str, str] = {}
    if "\n" in raw or ";" in raw or raw[:9].upper().startswith("SESSDATA="):
        for chunk in re.split(r"[;\n]", raw):
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name, value = name.strip(), value.strip()
            if not name or " " in name:
                continue
            found[name] = value
        found = {k: v for k, v in found.items() if k in KEEP_COOKIES and v}
        if found.get("SESSDATA"):
            found["SESSDATA"] = _normalize_sessdata(found["SESSDATA"])
            return found

    # 4) 裸值：整段文本就是一个 SESSDATA。
    #    真实值由 base64 字符 + `,` + `*` 组成，**绝不含空白** —— 用这一点挡掉
    #    「用户随手粘了一段无关文字」的情况，否则会被当成 SESSDATA 去校验，
    #    最后报「登录校验失败」，把原因指到完全错误的方向。
    value = _normalize_sessdata(raw)
    if value and not re.search(r"\s", value):
        return {"SESSDATA": value}
    return {}


def _read_config() -> Dict[str, Any]:
    """读同目录 `bili_config.json`，返回原始字典（读不到就返回空字典）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config_file(cookies: Dict[str, str]) -> None:
    """把 cookie 落盘到 `bili_config.json`（供网页「账号设置」调用）。

    cookies 为空时写入空表而不是删文件 —— 保留文件能让用户直观看到配置项确实存在。
    """
    cookies = {k: v for k, v in (cookies or {}).items() if k in KEEP_COOKIES and v}
    payload: Dict[str, Any] = {
        "_说明": "由网页「B站 账号设置」自动写入；也可手工编辑。清空 cookies 即为未配置。",
        "_风险": "本文件等同于账号登录态，已在 .gitignore 中忽略，请勿提交或外传。",
        "sessdata": cookies.get("SESSDATA", ""),
        "cookies": cookies,
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_cookies() -> Dict[str, str]:
    """返回应当注入 session 的完整 cookie 集合（**环境变量优先于配置文件**）。

    环境变量只承载 SESSDATA 一项；其余指纹类 cookie 由客户端自行获取。
    """
    env_value = _normalize_sessdata(os.environ.get("BILI_SESSDATA") or "")
    if env_value:
        return {"SESSDATA": env_value}

    data = _read_config()
    cookies = data.get("cookies")
    result: Dict[str, str] = {}
    if isinstance(cookies, dict):
        result.update({k: str(v) for k, v in cookies.items() if v})
    # 兼容旧版只有顶层 sessdata 字段的配置文件
    legacy = _normalize_sessdata(data.get("sessdata") or "")
    if legacy:
        result["SESSDATA"] = legacy
    return {k: v for k, v in result.items() if k in KEEP_COOKIES and v}


def load_sessdata() -> str:
    """按「环境变量 > 同目录配置文件」的顺序读取 SESSDATA。

    每次调用都重新读文件，所以网页改完配置**无需重启服务**即可生效。
    """
    return load_cookies().get("SESSDATA", "")


def cookie_status() -> Dict[str, Any]:
    """当前 cookie 配置的来源与摘要，供前端设置面板回显。

    只回打码值与长度 —— 前端需要「确认填的是哪一个」和「判断是不是复制不全」，
    但没有任何理由拿到明文。
    """
    env_value = _normalize_sessdata(os.environ.get("BILI_SESSDATA") or "")
    file_cookies = {}
    if not env_value:
        data = _read_config()
        raw = data.get("cookies")
        if isinstance(raw, dict):
            file_cookies = {k: str(v) for k, v in raw.items() if v}
        legacy = data.get("sessdata") or ""
        if legacy and "SESSDATA" not in file_cookies:
            file_cookies["SESSDATA"] = legacy

    active = {"SESSDATA": env_value} if env_value else file_cookies
    sessdata = _normalize_sessdata(active.get("SESSDATA", ""))

    return {
        "configured": bool(sessdata),
        # 环境变量优先级高于文件；两者都有时以 env 为准
        "source": "env" if env_value else ("file" if file_cookies else "none"),
        "env_override": bool(env_value),
        "masked": _mask(sessdata),
        "length": len(sessdata),
        "cookie_names": sorted(active.keys()),
    }


class BiliClient:
    """带 WBI 签名、指纹与风控退避的 B站 客户端。

    进程内按 cookie 集合复用（复用可保持 buvid3 / bili_ticket 温热，
    显著降低请求数与 -412 概率）。
    """

    def __init__(self, sessdata: str = "", cookies: Optional[Dict[str, str]] = None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Referer": WEB + "/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        # 插件导出的整份 cookie 都注入进去：buvid3/buvid4 能让后续请求
        # 直接跳过「先取指纹」这一步，也就少一次被 -412 命中的机会。
        merged: Dict[str, str] = {}
        if cookies:
            merged.update({k: v for k, v in cookies.items() if v})
        value = (sessdata or "").strip()
        if value:
            merged["SESSDATA"] = value
        for name, cookie_value in merged.items():
            self.session.cookies.set(name, cookie_value, domain=".bilibili.com")
        self.sessdata = merged.get("SESSDATA", "")
        self.cookies = merged
        self._mixin_key: Optional[str] = None
        self._fingerprinted = False
        self.logged_in = False
        self.is_vip = False
        self.uname = ""

    # ---------- 指纹与签名 ----------

    def ensure_fingerprint(self) -> None:
        """取 buvid3/buvid4 并生成 bili_ticket —— 这两样是绕开 -412 的关键。"""
        if self._fingerprinted:
            return
        try:
            self.session.get(WEB + "/", timeout=15)
        except requests.RequestException:
            pass
        if "buvid3" not in self.session.cookies:
            try:
                payload = self.session.get(API + "/x/frontend/finger/spi", timeout=15).json()
                data = payload.get("data") or {}
                for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
                    if data.get(key):
                        self.session.cookies.set(name, data[key])
            except (requests.RequestException, ValueError):
                pass
        try:
            ts = int(time.time())
            # 官方 web 端固定密钥与 key_id，HMAC-SHA256(ts...) 即 hexsign
            hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
            payload = self.session.post(
                API + "/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                params={"key_id": "ec02", "hexsign": hexsign, "context[ts]": ts, "csrf": ""},
                timeout=15,
            ).json()
            data = payload.get("data") or {}
            if data.get("ticket"):
                self.session.cookies.set("bili_ticket", data["ticket"])
                created = data.get("created_at") or ts
                self.session.cookies.set("bili_ticket_expires", str(created + 259200))
        except (requests.RequestException, ValueError):
            pass
        self._fingerprinted = True

    def nav(self) -> Dict[str, Any]:
        """拉一次 nav，并同步登录态。返回精简后的账号信息（含 wbi_img）。

        nav 是判断「SESSDATA 到底生效没有」的**唯一可靠依据**。
        注意：未登录时返回 `code=-101「账号未登录」`，但 `data.wbi_img` 依然可用，
        所以必须容忍 -101，不能用严格校验把签名所需的密钥一起抛掉。
        """
        payload = self._request_json(
            API + "/x/web-interface/nav", None, allow_codes=(-101, -400, -403)
        )
        data = payload.get("data") or {}
        self.logged_in = bool(data.get("isLogin"))
        # vipStatus / data.vip.status 只表示「会员当前是否在有效期内」，
        # 并不等于「能看多清晰」—— 实测有 vipStatus=0（会员已于 2024-07 过期）的账号
        # 仍能取到 qn=120 (4K)。所以界面上的清晰度文案一律以 dash.video
        # 实际返回的轨道为准（见 parse()），不要拿这个字段去预测。
        self.is_vip = bool(data.get("vipStatus"))
        self.uname = data.get("uname") or ""
        return {
            "logged_in": self.logged_in,
            "vip": self.is_vip,
            "uname": self.uname,
            "mid": data.get("mid") or 0,
            "wbi_img": data.get("wbi_img") or {},
        }

    def _ensure_mixin_key(self) -> str:
        if self._mixin_key:
            return self._mixin_key
        nav = self.nav()
        wbi = nav.get("wbi_img") or {}
        if not wbi.get("img_url") or not wbi.get("sub_url"):
            raise BiliError("无法获取 B站签名密钥（wbi_img），请稍后重试")
        img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in MIXIN_TAB)[:32]
        return self._mixin_key

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mixin = self._ensure_mixin_key()
        params = dict(params)
        params["wts"] = int(time.time())
        items = [
            (key, re.sub(r"[!'()*]", "", str(params[key]))) for key in sorted(params)
        ]
        query = urlencode(items)
        params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return params

    # ---------- 带退避的请求 ----------

    def _request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]],
        signed: bool = False,
        retries: int = 3,
        allow_codes: Tuple[int, ...] = (),
    ) -> Dict[str, Any]:
        """带限速与退避的请求；`allow_codes` 里的业务码不视为错误（如 nav 的 -101）。"""
        self.ensure_fingerprint()
        last_error = "请求失败"
        for attempt in range(retries):
            _throttle()
            final = self._sign(params or {}) if signed else params
            try:
                resp = self.session.get(url, params=final, timeout=25)
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = f"网络请求失败（{exc}）"
                time.sleep(1.5 * (attempt + 1))
                continue

            code = payload.get("code")
            if code == 0 or code in allow_codes:
                return payload
            if code == -412:
                last_error = "B站风控拦截（-412）"
                # 风控时重新获取指纹再试，往往能过
                self._fingerprinted = False
                time.sleep(2.0 * (attempt + 1))
                continue
            if code in (-352, -401, -403):
                raise BiliError(self._friendly(code))
            raise BiliError(payload.get("message") or f"B站接口返回 code={code}")
        raise BiliError(f"{last_error}，已自动重试 {retries} 次，请 1~2 分钟后重试")

    @staticmethod
    def _friendly(code: int) -> str:
        if code == -352:
            return "B站风控校验失败（-352），请稍后重试；长期如此建议配置 SESSDATA"
        return "该内容需要登录或更高权限（如大会员专享），请在配置中填入 SESSDATA"

    # ---------- 业务接口 ----------

    def view(self, bvid: Optional[str] = None, aid: Optional[int] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
        return self._request_json(API + "/x/web-interface/view", params)["data"]

    def playurl(self, bvid: str, cid: int, qn: int = 127) -> Dict[str, Any]:
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": 4048,  # DASH + HDR + 4K + 杜比 + 8K + AV1 全开
            "fnver": 0,
            "fourk": 1,
        }
        return self._request_json(API + "/x/player/wbi/playurl", params, signed=True)["data"]

    def danmaku(self, cid: int) -> bytes:
        """取弹幕 XML（list.so 返回的是弹幕池 XML，可直接播放器加载）。"""
        _throttle()
        resp = self.session.get(
            API + "/x/v1/dm/list.so", params={"oid": cid}, timeout=25
        )
        resp.raise_for_status()
        return resp.content

    # ---------- 用户空间（UP 主主页） ----------

    def space_info(self, mid: str) -> Dict[str, Any]:
        """UP 主名片：昵称 / 头像 / 签名。需要 WBI 签名，否则 -352 风控。"""
        return self._request_json(
            API + "/x/space/wbi/acc/info",
            {"mid": str(mid), "platform": "web", "web_location": "1550101"},
            signed=True,
        ).get("data") or {}

    def space_relation(self, mid: str) -> Dict[str, Any]:
        """粉丝 / 关注数。**不签名**也能取，成本低；失败时调用方降级为 0。"""
        return self._request_json(
            API + "/x/relation/stat", {"vmid": str(mid)}
        ).get("data") or {}

    def space_videos(self, mid: str, pn: int = 1, ps: int = 30) -> Dict[str, Any]:
        """UP 主投稿列表。`pn` 是**页码**（不是游标），`ps` 每页条数。"""
        return self._request_json(
            API + "/x/space/wbi/arc/search",
            {
                "mid": str(mid),
                "ps": int(ps),
                "pn": int(pn),
                "order": "pubdate",
                "platform": "web",
                "web_location": "1550101",
            },
            signed=True,
        ).get("data") or {}


_CLIENTS: Dict[str, BiliClient] = {}
_CLIENTS_LOCK = threading.Lock()


def _cookie_key(cookies: Dict[str, str]) -> str:
    """把 cookie 集合压成一个稳定字符串，作为客户端缓存的键。"""
    return json.dumps(sorted((cookies or {}).items()), ensure_ascii=False)


def get_client(sessdata: Optional[str] = None) -> BiliClient:
    """按 cookie 集合复用客户端，保持指纹温热、减少请求数（也就减少 -412）。

    `sessdata` 显式传入时只用它构造一个最小 cookie 集合；否则读配置文件。
    """
    if sessdata is None:
        cookies = load_cookies()
    else:
        value = _normalize_sessdata(sessdata)
        cookies = {"SESSDATA": value} if value else {}
    key = _cookie_key(cookies)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = BiliClient(cookies=cookies)
            _CLIENTS[key] = client
        return client


def reset_clients() -> None:
    """清空客户端缓存。cookie 变更后调用，确保下一次请求用的是新登录态。"""
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


def check_account(cookies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """用给定 cookie 探一次 `nav`，判断登录态是否真的生效。

    只读探测，**不写任何状态**：调用方据此决定「保存 or 拒绝保存」。
    网络异常会照常抛出，交给调用方区分「cookie 无效」和「网络不通」。
    """
    if cookies is None:
        cookies = load_cookies()
    client = BiliClient(cookies=cookies)
    info = client.nav()
    return {
        "logged_in": info["logged_in"],
        "vip": info["vip"],
        "uname": info["uname"],
        "mid": info["mid"],
    }


# ---------- 轨道与档位提取 ----------


def extract_tracks(data: Dict[str, Any]) -> Dict[str, Any]:
    """从 playurl 返回里取出真实存在的视频/音频轨。

    关键：只认 `dash.video` / `dash.audio` 里真实存在的对象，
    不参考 `accept_quality`（会谎报）。
    """
    dash = data.get("dash") or {}
    best_video: Dict[int, Dict[str, Any]] = {}
    for track in dash.get("video") or []:
        qn = track.get("id")
        if qn is None:
            continue
        current = best_video.get(qn)
        if current is None or _codec_rank(track) < _codec_rank(current):
            best_video[qn] = track

    audio: Optional[Dict[str, Any]] = None
    flac = (dash.get("flac") or {}).get("audio")
    dolby_list = (dash.get("dolby") or {}).get("audio") or []
    if flac:
        audio = dict(flac)
        audio["_label"] = "Hi-Res 无损"
        audio["_priority"] = 300
    elif dolby_list:
        audio = dict(dolby_list[0])
        audio["_label"] = "杜比全景声"
        audio["_priority"] = 200
    else:
        for track in dash.get("audio") or []:
            priority = track.get("id") or 0
            if audio is None or priority > (audio.get("_priority") or 0):
                audio = dict(track)
                audio["_label"] = AUDIO_NAMES.get(track.get("id"), f"id{track.get('id')}")
                audio["_priority"] = priority

    return {"video": best_video, "audio": audio}


def track_url(track: Dict[str, Any]) -> Optional[str]:
    """取轨道的可下载地址（优先 baseUrl，其次第一条 backupUrl）。"""
    url = track.get("baseUrl") or track.get("base_url")
    if url:
        return url
    backups = track.get("backupUrl") or track.get("backup_url") or []
    return backups[0] if backups else None


def build_qualities(data: Dict[str, Any], duration: int) -> List[Dict[str, Any]]:
    """把真实轨道整理成「档位列表」，按清晰度从高到低。"""
    tracks = extract_tracks(data)
    result: List[Dict[str, Any]] = []
    for qn, track in sorted(tracks["video"].items(), reverse=True):
        bandwidth = track.get("bandwidth") or 0
        result.append(
            {
                "qn": qn,
                "label": QUALITY_NAMES.get(qn, f"qn{qn}"),
                "width": track.get("width") or 0,
                "height": track.get("height") or 0,
                "codec": str(track.get("codecs") or "").split(".")[0] or "?",
                "bandwidth": bandwidth,
                "fps": track.get("frameRate") or 0,
                # 预估体积（字节），仅视频轨；用于前端提示
                "size": int(bandwidth * duration / 8) if bandwidth and duration else 0,
                "url": track_url(track),
            }
        )
    return result


def audio_info(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    audio = extract_tracks(data)["audio"]
    if not audio:
        return None
    bandwidth = audio.get("bandwidth") or 0
    return {
        "label": audio.get("_label") or "音频",
        "bandwidth": bandwidth,
        "codec": str(audio.get("codecs") or "").split(".")[0] or "?",
        "url": track_url(audio),
    }


# ---------- 对外入口 ----------


def parse(text: str, page: int = 0, sessdata: Optional[str] = None) -> Dict[str, Any]:
    """解析一条 B站 链接，返回可供前端渲染的结构。

    page=0 表示自动：优先用链接里的 `?p=`，否则第 1 P。
    """
    original = text or ""
    if BILI_HOST_PATTERN.search(original) and BV_PATTERN.search(original) is None:
        original = resolve_short_link(original)

    bvid = extract_bvid(original)
    aid = extract_aid(original)
    if not bvid and not aid:
        raise BiliError("没识别出 BV 号，请粘贴完整的 B站 视频链接")

    client = get_client(sessdata)
    info = client.view(bvid=bvid, aid=aid)
    bvid = info.get("bvid") or bvid
    aid = info.get("aid") or aid

    raw_pages = info.get("pages") or []
    if raw_pages:
        pages = [
            {
                "page": item.get("page") or index + 1,
                "cid": item.get("cid"),
                "part": item.get("part") or f"P{item.get('page') or index + 1}",
                "duration": item.get("duration") or 0,
            }
            for index, item in enumerate(raw_pages)
        ]
    else:
        pages = [
            {
                "page": 1,
                "cid": info.get("cid"),
                "part": info.get("title") or "",
                "duration": info.get("duration") or 0,
            }
        ]

    target = page or extract_page(original) or 1
    chosen = next((p for p in pages if p["page"] == target), pages[0])

    play = client.playurl(bvid, chosen["cid"])
    duration = chosen.get("duration") or info.get("duration") or 0
    qualities = build_qualities(play, duration)

    owner = info.get("owner") or {}
    stat = info.get("stat") or {}
    best_qn = qualities[0]["qn"] if qualities else 0
    configured = bool(client.sessdata)

    hint = ""
    logged_in = bool(client.logged_in)
    if not qualities:
        hint = "该视频没有返回任何可下载轨道，可能需要大会员或已下架"
    elif not configured:
        hint = "未配置 SESSDATA，当前最高 480P；填入自己的登录 Cookie 后可解锁 1080P/4K"
    elif not logged_in:
        # 填了 Cookie 但没生效 —— 必须和「登录了但只有 480P」区分开，
        # 否则用户会误以为是视频本身的限制，白白排查方向。
        hint = (
            "SESSDATA 已填写但登录态无效（多半是复制不完整或已过期），"
            "当前仍按未登录给到 480P，请重新获取一次"
        )
    elif best_qn <= FREE_MAX_QN:
        hint = (
            "该视频本身最高只有 480P（与账号无关）"
            if client.is_vip
            else "当前账号可见的最高档位为 480P，更高清晰度需要大会员"
        )
    else:
        # 登录态确实解锁了高于 480P 的档位 —— 给一句正向确认，
        # 让用户知道「填的 Cookie 生效了」，而不是靠猜。
        hint = (
            f"已登录，该视频可用最高 {qualities[0]['label']}；"
            "下载时由服务端用 ffmpeg 合流音视频轨，无需分别下载"
        )

    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (info.get("title") or "bilibili")).strip()
    safe_author = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", owner.get("name") or "").strip()
    filename = f"{safe_title}" + (f"_{safe_author}" if safe_author else "")

    return {
        "platform": "bilibili",
        "bvid": bvid,
        "aid": aid,
        "title": info.get("title") or "",
        "author": owner.get("name") or "",
        "author_avatar": owner.get("face") or "",
        # UP 主 mid：前端据此把作者名做成「看 TA 的全部作品」入口。
        # 之前只回昵称，结果解析完一个视频就没法顺着人往下走。
        "author_mid": str(owner.get("mid") or ""),
        "cover": info.get("pic") or "",
        "desc": (info.get("desc") or "").strip(),
        "pubdate": info.get("pubdate") or 0,
        "duration": duration,
        "duration_text": _duration_text(duration),
        "stats": {
            "view": stat.get("view") or 0,
            "danmaku": stat.get("danmaku") or 0,
            "like": stat.get("like") or 0,
            "coin": stat.get("coin") or 0,
            "favorite": stat.get("favorite") or 0,
            "share": stat.get("share") or 0,
            "reply": stat.get("reply") or 0,
        },
        "pages": pages,
        "current_page": chosen["page"],
        "cid": chosen["cid"],
        "qualities": qualities,
        "best_qn": best_qn,
        "audio": audio_info(play),
        "account": {
            "configured": configured,
            "logged_in": client.logged_in,
            "vip": client.is_vip,
            "uname": client.uname,
        },
        "hint": hint,
        "filename": filename or "bilibili",
        "source_url": original,
    }


def _duration_text(seconds: int) -> str:
    if not seconds:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ===========================================================================
# 用户主页（UP 主空间）作品列表
#
# 走 `/x/space/wbi/arc/search`，与 playurl 共用同一套 WBI 签名与限速。
# 实测（2026-10-27，登录态）：mid=2 → 43 条、mid=546195 → 681 条，
# 分页用 `pn`（**页码**，与抖音的游标不同）+ `ps`（每页条数，实测 30 可用）。
#
# ⚠️ 主页列表**不返回清晰度信息**。B站 的档位要靠 `/x/player/wbi/playurl`
# 逐条去问（还要按账号的登录/大会员身份给不同上限），所以这里只给「有哪些作品」，
# 用户点开某一条时才走 `parse()` 拿多档 —— 否则列 30 条就要打 30 次接口，
# 在 B站 的 -412 风控下几乎必然被拦。
# ===========================================================================

#: `https://space.bilibili.com/546195` / `space.bilibili.com/546195/video`
SPACE_PATTERN = re.compile(r"space\.bilibili\.com/(\d{3,20})", re.IGNORECASE)
#: 纯数字 mid（B站 的 mid 从 1 位数到 17 位数都有）。**上限刻意收在 12 位**：
#: 抖音的 aweme_id 是 15~19 位纯数字，放开到 20 位就会把作品 id 误当成 UP 主 mid。
MID_PATTERN = re.compile(r"\d{3,12}")

#: 每页条数。实测 30 可用；B站 网页版自己用 30。
SPACE_PAGE_SIZE = 30
#: 单次连续翻页上限，防止 has_more 异常时打爆风控
SPACE_MAX_PAGES = 20


def looks_like_space(text: str) -> bool:
    """这段内容是不是「UP 主主页」链接（而不是单个视频）。只做形态判断，不联网。"""
    if not text:
        return False
    if SPACE_PATTERN.search(text):
        return True
    stripped = text.strip()
    # 裸 mid：纯数字且不含 BV 号。作品 aid（av170001）不会被误判 —— 它带 av 前缀，
    # 而 Space 链接里的 mid 是纯数字。
    return bool(MID_PATTERN.fullmatch(stripped))


def extract_mid(text: str) -> Optional[str]:
    """从任意文本里取 UP 主 mid。取不到返回 None。"""
    match = SPACE_PATTERN.search(text or "")
    if match:
        return match.group(1)
    stripped = (text or "").strip()
    if MID_PATTERN.fullmatch(stripped):
        return stripped
    return None


def _space_duration_seconds(length: str) -> int:
    """把列表里的 `12:34` / `1:02:03` 转成秒。列表接口不直接给秒数。"""
    parts = [p for p in str(length or "").split(":") if p != ""]
    if not parts:
        return 0
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for value in numbers:
        seconds = seconds * 60 + value
    return seconds


def normalize_space_post(item: Dict[str, Any], author: Dict[str, Any]) -> Dict[str, Any]:
    """把 space 列表的一条作品归一化成前端既有契约（与抖音主页同一套字段）。

    `qualities` 刻意留空：B站 的档位必须逐条问 playurl，见本节开头说明。
    前端据此显示「点开解析」，而不是伪造一个档位。
    """
    bvid = item.get("bvid") or ""
    title = (item.get("title") or "").strip()
    # 列表里的标题带 <em class="keyword"> 高亮标签（搜索时才有），统一剥掉
    title = re.sub(r"<[^>]+>", "", title)
    duration = _space_duration_seconds(item.get("length"))
    cover = item.get("pic") or ""
    if cover.startswith("//"):
        cover = "https:" + cover

    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", title or "bilibili").strip()

    return {
        "platform": "bilibili",
        "aweme_id": bvid,          # 复用 douyin 的字段名，前端 historyId/去重逻辑共用
        "bvid": bvid,
        "aid": item.get("aid") or 0,
        "type": "video",
        "title": title or "(无标题)",
        "filename": safe_title or "bilibili",
        "desc": (item.get("description") or "").strip(),
        "cover": cover,
        "duration": duration,
        "create_time": item.get("created") or 0,
        "author": {
            "nickname": author.get("nickname") or "",
            "uid": author.get("sec_uid") or "",
            "sec_uid": author.get("sec_uid") or "",
            "avatar": [author.get("avatar") or ""],
        },
        "stats": {
            # B站 列表只给播放与弹幕，没有点赞 —— 不要编 0 之外的数
            "digg": 0,
            "play": item.get("play") or 0,
            "comment": item.get("comment") or 0,
            "danmaku": item.get("video_review") or 0,
        },
        "video_urls": [],
        "images": [],
        "qualities": [],
        "hd": False,
        "is_top": False,
        "share_url": "https://www.bilibili.com/video/%s" % bvid,
    }


def fetch_space_profile(mid: str, sessdata: Optional[str] = None) -> Dict[str, Any]:
    """UP 主资料（昵称 / 头像 / 签名 / 粉丝数）。

    分两个接口：`acc/info` 有昵称头像签名、`relation/stat` 才有粉丝数。
    粉丝数拿不到不影响主流程（列表照常能出），所以这里对它做降级处理。
    """
    client = get_client(sessdata)
    try:
        info = client.space_info(mid)
    except BiliError:
        raise
    if not info:
        raise BiliError("没能取到该 UP 主的资料（可能已注销、被封禁，或链接里的 mid 不完整）")

    relation: Dict[str, Any] = {}
    try:
        relation = client.space_relation(mid)
    except BiliError:
        relation = {}

    return {
        "platform": "bilibili",
        # sec_uid 是「本机关注列表里的用户标识」，对 B站 就是 mid —— 沿用同名字段，
        # 让关注列表不必区分平台就能存/删/排序。
        "sec_uid": str(info.get("mid") or mid),
        "uid": str(info.get("mid") or mid),
        "nickname": (info.get("name") or "").strip() or "B站 用户",
        "unique_id": "",
        "signature": (info.get("sign") or "").strip(),
        "avatar": info.get("face") or "",
        "follower_count": relation.get("follower") or 0,
        "following_count": relation.get("following") or 0,
        "total_favorited": 0,
        "aweme_count": 0,          # 由调用方用 space 首屏的 page.count 回填
        "url": "https://space.bilibili.com/%s" % (info.get("mid") or mid),
    }


def fetch_space_videos(
    mid: str, pn: int = 1, sessdata: Optional[str] = None
) -> Dict[str, Any]:
    """取一页 UP 主作品。返回 {posts, cursor, has_more}，cursor 里放的是**下一页页码**。"""
    client = get_client(sessdata)
    data = client.space_videos(mid, pn=pn)
    vlist = ((data.get("list") or {}).get("vlist")) or []
    page = data.get("page") or {}

    # 先取资料：列表条目里没有头像/昵称，得从 space_info 补
    profile = fetch_space_profile(mid, sessdata)
    posts = [normalize_space_post(item, profile) for item in vlist if isinstance(item, dict)]

    total = int(page.get("count") or 0)
    size = int(page.get("ps") or SPACE_PAGE_SIZE) or SPACE_PAGE_SIZE
    done = int(page.get("pn") or pn) * size
    return {
        "posts": posts,
        "cursor": str(int(page.get("pn") or pn) + 1),
        "has_more": bool(vlist) and done < total,
        "total": total,
    }


def parse_space(text: str, pn: int = 1, pages: int = 1,
                sessdata: Optional[str] = None) -> Dict[str, Any]:
    """UP 主主页入口：链接 -> {user, posts, cursor, has_more}。

    与 `douyin_user.parse_user()` 的返回结构保持一致，`server.py` 因此可以
    用同一套接口给前端，前端也只需要一套渲染。
    """
    mid = extract_mid(text)
    if not mid:
        raise BiliError(
            "没能从这条链接里认出 UP 主。请打开他的个人空间、复制地址栏链接"
            "（形如 https://space.bilibili.com/546195）；单个视频请直接粘视频链接。"
        )

    profile = fetch_space_profile(mid, sessdata)
    posts: List[Dict[str, Any]] = []
    cursor = str(pn)
    has_more = True
    total = 0

    for _ in range(max(1, min(int(pages or 1), SPACE_MAX_PAGES))):
        page = fetch_space_videos(mid, int(cursor), sessdata)
        posts.extend(page["posts"])
        cursor = page["cursor"]
        has_more = page["has_more"]
        total = page.get("total") or total
        if not has_more:
            break

    # 列表里可能有置顶/重复条目，按 bvid 去重
    seen = set()
    unique: List[Dict[str, Any]] = []
    for post in posts:
        key = post.get("bvid") or post.get("aweme_id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(post)

    profile["aweme_count"] = total or len(unique)
    return {"user": profile, "posts": unique, "cursor": cursor, "has_more": has_more}

