# -*- coding: utf-8 -*-
"""
抖音无水印解析下载站 · 后端服务

提供三个能力：
    /api/parse     解析分享文案，返回作品元数据与无水印地址
    /api/download  流式代理下载（带中文文件名，规避直链防盗链）
    /api/proxy     媒体代理（支持 Range，用于在线预览与封面显示）

启动： python server.py  ->  http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

import bili_parser
import dash_muxer
import disk_saver
import runtime_paths
import tiktok_parser
import xhs_parser
from bili_parser import BiliError
from bili_parser import parse as bili_parse
from douyin_parser import ParseError, parse as douyin_parse
from tiktok_parser import TikTokError
from tiktok_parser import parse as tiktok_parse
from xhs_parser import XHSError
from xhs_parser import parse as xhs_parse

# 路径统一交给 runtime_paths 处理「源码运行 / exe 打包运行」两种形态：
# 打包后只读资源在 _MEIPASS，可写配置写在 exe 同目录的 data/ —— 不能再用 __file__。
BASE_DIR = runtime_paths.resource_dir()
STATIC_DIR = os.path.join(BASE_DIR, "static")
# 部署平台（Render/Heroku 等）通过 $PORT 传入端口；本地默认 8787
PORT = int(os.environ.get("PORT", os.environ.get("DOUYIN_DL_PORT", "8787")))
# 实际生效端口。main() 里若发现 8787 被占用会顺延，并写回这里（页面渲染时注入给前端）。
ACTUAL_PORT = PORT
# 监听地址。默认仅本机：打包给别人用时不会弹防火墙、也不会把服务暴露给同网段。
# 云端部署走 `uvicorn server:app --host 0.0.0.0`（见 Dockerfile 的 CMD），不经过 main()，不受此项影响。
HOST = (os.environ.get("HOST") or "127.0.0.1").strip()

# 仅允许代理白名单内的媒体域名，避免服务被当作任意下载代理（SSRF 防护）
ALLOWED_HOST_SUFFIXES = (
    # —— 抖音系 ——
    "douyinvod.com",
    "douyinpic.com",
    "douyinstatic.com",
    "snssdk.com",
    "ixigua.com",
    "iesdouyin.com",
    "douyin.com",
    "amemv.com",
    "byteimg.com",
    "bytecdn.cn",
    "pstatp.com",
    "toutiaovod.com",
    "huoshan.com",
    # —— B站系 ——
    "bilibili.com",
    "hdslb.com",
    "bilivideo.com",
    "bilivideo.cn",
    "biliapi.net",
    "akamaized.net",
    "bstarstatic.com",
    # B站 自建 PCDN。2026-09-30 在云端（境内机房）解析时被分配到
    # `b-xxxx.edge.mountaintoys.cn`：带 `Referer: https://www.bilibili.com/` 返回
    # 200 / video/mp4 / 57,742,841 字节，不带则 403 —— 域名归属确认无误。
    # ⚠️ 这类域名会随出口地区漂移，枚举只是兜底，真正的保障是 _MEDIA_HOSTS。
    "mountaintoys.cn",
    # —— TikTok 系 ——
    # 实测用到的：v45.tiktokcdn-us.com（视频）、p19-common-sign.tiktokcdn-us.com（封面）、
    # v16-webapp-prime.us.tiktok.com（视频）、api16-normal-useast8.tiktokv.us（跳转直链）
    "tiktok.com",
    "tiktokv.com",
    "tiktokv.us",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "ttwstatic.com",
    "byteoversea.com",
    "ibyteimg.com",
    "muscdn.com",
    "musical.ly",
    # —— 小红书系 ——
    # 图片：sns-webpic-qc / sns-img-qc / sns-avatar-qc .xhscdn.com
    # 视频：sns-video-qc / sns-video-bd .xhscdn.com
    "xhscdn.com",
    "xiaohongshu.com",
    "xhs.cn",
)

# 移动端 UA + Referer，用于通过抖音 CDN 的基础校验
CDN_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) EdgiOS/121.0.2277.107 Version/17.0 Mobile/15E148 Safari/604.1"
)
CDN_HEADERS = {
    "User-Agent": CDN_UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
}

# B站 CDN 有 Referer 防盗链，必须带自家 Referer 才允许拉取
BILI_HEADERS = {
    "User-Agent": bili_parser.UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "*/*",
}

# TikTok CDN 同样认自家 Referer。实测同一条直链：
#   不带 Referer → HTTP 403（响应体是 <HTML><HEAD>… 的拦截页）
#   带 Referer   → HTTP 206，Content-Range 正常，首块 magic = ftypqt
TIKTOK_HEADERS = {
    "User-Agent": tiktok_parser.UA,
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com",
    "Accept": "*/*",
}

# 小红书 CDN 同样有防盗链：带自家 Referer 最稳（图片实测可直连，但统一带上更保险）
XHS_HEADERS = {
    "User-Agent": xhs_parser.UA,
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
    "Accept": "*/*",
}

# 平台识别规则：顺序即优先级（BV 号可能出现在任意文本里，故 B站 放最前）
PLATFORM_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("bilibili", re.compile(r"(bilibili\.com|b23\.tv|acg\.tv|BV[0-9A-Za-z]{10})", re.I)),
    ("xiaohongshu", re.compile(r"(xiaohongshu\.com|xhslink\.(?:com|cn))", re.I)),
    ("tiktok", re.compile(r"(tiktok\.com|tiktokv\.com)", re.I)),
    ("douyin", re.compile(r"(douyin\.com|iesdouyin\.com)", re.I)),
)


def detect_platform(text: str) -> str:
    """按链接/文本识别所属平台；识别不出返回空串。"""
    for name, pattern in PLATFORM_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg 可执行文件。

    B站 高清是 DASH 音视频分离，合流必须依赖 ffmpeg。
    查找顺序：环境变量 FFMPEG_PATH → **打包内置的那份** → PATH → 已知安装位置。

    「内置」刻意排在 PATH 之前：否则用户机器上一个老旧/残缺的 ffmpeg 会被优先选中，
    表现为「合流失败」，而这类问题在别人电脑上几乎无法排查。
    """
    explicit = (os.environ.get("FFMPEG_PATH") or "").strip().strip('"')
    if explicit and os.path.isfile(explicit):
        return explicit

    bundled = [
        os.path.join(runtime_paths.resource_dir(), "ffmpeg.exe"),
        os.path.join(runtime_paths.app_dir(), "ffmpeg.exe"),
        os.path.join(runtime_paths.resource_dir(), "bin", "ffmpeg.exe"),
        os.path.join(runtime_paths.app_dir(), "bin", "ffmpeg.exe"),
    ]
    for path in bundled:
        if path and os.path.isfile(path):
            return path

    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        r"D:\codex\.tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        os.path.join(os.environ.get("ProgramFiles", ""), "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _port_available(port: int) -> bool:
    """端口能否绑定。

    刻意**不设** SO_REUSEADDR —— Windows 上它允许绑定到已被占用的地址，
    会让这个探测得出「可用」的错误结论。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, tries: int = 20) -> int:
    """端口被占用时顺延，避免别人电脑上一个 8787 冲突就「双击没反应」。

    前端的后端地址由页面渲染时注入实际端口（见 index_page），所以顺延不会打错接口。
    """
    for offset in range(tries):
        candidate = preferred + offset
        if _port_available(candidate):
            return candidate
    raise SystemExit(
        f"端口 {preferred}–{preferred + tries - 1} 全被占用，无法启动。"
        f"可设环境变量 DOUYIN_DL_PORT 指定其它端口后重试。"
    )

app = FastAPI(title="多平台无水印下载 · 抖音/B站/TikTok/小红书", docs_url="/docs")

# 页面可能从其它本地端口（如编辑器预览服务）打开，此时属于跨源请求。
# 仅放行本机来源，兼顾可用性与安全：外部站点仍无法调用本服务。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range"],
)


class ParseRequest(BaseModel):
    text: str
    # 留空或 "auto" 时按链接自动识别平台；也可显式指定 douyin / bilibili / xiaohongshu
    platform: Optional[str] = None
    # B站 专用：指定分P（0 或留空表示用链接里的 ?p=，否则第 1 P）
    page: int = 0


class BiliConfigRequest(BaseModel):
    """B站 账号配置请求（供网页「账号设置」面板调用）。

    text 需要能接住四种粘贴习惯，解析规则见 `bili_parser.parse_cookie_input`：
      1. 插件「Get cookies.txt LOCALLY」导出的 cookies.txt 全文（最推荐）；
      2. 请求头形式的 `SESSDATA=xxx; bili_jct=yyy`；
      3. 单行 `SESSDATA=xxx`；
      4. 光秃秃的一个 SESSDATA 值。

    force=True 表示「明知登录校验没过也照样保存」，用于 B站 侧风控/网络抖动
    导致误判时的人工兜底。
    """

    text: str = ""
    force: bool = False


class TikTokConfigRequest(BaseModel):
    """TikTok 代理配置。

    proxy 形如 `http://127.0.0.1:7890`；留空表示回到「自动探测常见本地端口」。

    要不要填取决于本服务跑在哪（2026-09-30 部署到 Render 后确认）：
      · 跑在大陆网络的电脑上 —— TikTok 官方接口无法直连，**必须**有可用代理；
      · 部署在境外服务器（如 Render / 海外 VPS）—— 通常**直连即可**，留空更省事。
    所以这一项不等于「必需」，前端与文档里都别写成必需的。
    """

    proxy: str = ""


class ZipItem(BaseModel):
    """打包条目：url 为直链，name 为期望文件名（可含扩展名，缺省则自动推断）。"""

    url: str
    name: Optional[str] = None


class ZipRequest(BaseModel):
    """打包下载请求。

    items 优先（可精确指定文件名，用于图集里图片与实况动态视频混排）；
    urls 为兼容旧版的纯图片列表写法。
    """

    urls: Optional[List[str]] = None
    items: Optional[List[ZipItem]] = None
    name: str = "douyin_images"


#: 本进程「解析产出过」的媒体主机。
#:
#: ★ 为什么需要它（2026-09-30 云端实测）：
#: 白名单是**枚举**，而平台的 CDN 主机是**漂移**的 —— 同一条 B站 视频，本地出口
#: 分到 `*.bilivideo.com`，云端（境内机房）却分到 `b-xxxx.edge.mountaintoys.cn`
#: （B站 自建 PCDN），于是「解析成功、下载 400 不支持代理该域名」。
#: 枚举永远追不上漂移，而**「这条直链是本服务自己解析出来的」本身就是最强的白名单
#: 依据** —— 解析那一刻就知道主机是谁，不该到下载再靠猜。
#:
#: 安全性：只登记 `_remember_media_hosts` 收到的、来自解析结果的地址。
#: 调用方**无法**通过构造 url 参数把任意主机塞进来（那条路只走白名单）。
#: 表随进程存活：真实顺序是「先解析、再下载」，下载时它一定是热的。
_MEDIA_HOSTS_LOCK = threading.Lock()
_MEDIA_HOSTS: "OrderedDict[str, None]" = OrderedDict()
MEDIA_HOSTS_LIMIT = 400


def _walk_hosts(node: Any, found: List[str], depth: int = 0) -> None:
    """从解析结果里递归收集所有 URL 的主机名（覆盖 qualities / images / music 等）。"""
    if depth > 12:
        return
    if isinstance(node, str):
        if node.startswith("http://") or node.startswith("https://"):
            host = (urlparse(node).hostname or "").lower()
            if host:
                found.append(host)
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk_hosts(value, found, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk_hosts(value, found, depth + 1)


def _remember_media_hosts(payload: Any) -> None:
    """登记一次解析结果里出现过的媒体主机（供 `_assert_allowed` 放行）。

    登记失败绝不影响解析：这里出问题只是下载时回到枚举白名单，
    不该把一次正常的解析拖挂。
    """
    try:
        found: List[str] = []
        _walk_hosts(payload, found)
        if not found:
            return
        with _MEDIA_HOSTS_LOCK:
            for host in found:
                _MEDIA_HOSTS[host] = None
                _MEDIA_HOSTS.move_to_end(host)
            while len(_MEDIA_HOSTS) > MEDIA_HOSTS_LIMIT:
                _MEDIA_HOSTS.popitem(last=False)
    except Exception:
        pass


def _assert_allowed(url: str) -> None:
    """校验目标地址是否在允许代理的域名白名单内。

    两级判据，任一命中即放行：
      1. 静态白名单（`ALLOWED_HOST_SUFFIXES`）—— 覆盖各平台常态域名；
      2. 本进程解析产出过的主机（`_MEDIA_HOSTS`）—— 覆盖会漂移的 CDN/PCDN 域名。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        raise HTTPException(status_code=400, detail="非法地址")
    if not host:
        raise HTTPException(status_code=400, detail="非法地址")
    if any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        return
    with _MEDIA_HOSTS_LOCK:
        if host in _MEDIA_HOSTS:
            return
    raise HTTPException(status_code=400, detail=f"不支持代理该域名：{host}")


BILI_HOST_SUFFIXES = (
    "bilibili.com",
    "hdslb.com",
    "bilivideo.com",
    "bilivideo.cn",
    "biliapi.net",
    "akamaized.net",
    "bstarstatic.com",
    # B站 PCDN（见 ALLOWED_HOST_SUFFIXES 内的同名注释）。
    # 加在这里是为了「前端没带 platform 时」也能选中 B站 的 Referer —— 否则
    # 这类直链会退回通用头，被 CDN 以 403 拒掉。
    "mountaintoys.cn",
)

TIKTOK_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokv.com",
    "tiktokv.us",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "ttwstatic.com",
    "byteoversea.com",
    "ibyteimg.com",
    "muscdn.com",
    "musical.ly",
)

XHS_HOST_SUFFIXES = (
    "xhscdn.com",
    "xiaohongshu.com",
    "xhs.cn",
)


def _platform_of(url: str, platform: str = "") -> str:
    """归一化「这条直链属于哪个平台」：调用方显式给了就用它，否则按域名推断。

    ★ 为什么必须让调用方把平台带下来（2026-09-26 真机实测踩坑）：

    **域名推不出平台。** TikTok 的视频有一大部分并不落在 tiktokcdn 上，而是落在
    Akamai（`*.akamaized.net`）—— 而 B站 同样在用 Akamai，所以 `akamaized.net` 一直
    被归在 B站 那一组。后果是这类 TikTok 直链会被「用 B站 的 Referer 去拉、还不挂
    代理」：国内直连 Akamai 边缘拿不到 TikTok 的源站，边缘服务器回的就是
    **HTTP 502**（真机上表现为「解析成功，但每条下载都是 502」）。

    解析那一刻明明知道是哪个平台，这个信息就该一路带到取流这一层，
    而不是到了下载再靠域名猜一次。
    """
    kind = (platform or "").strip().lower()
    if kind in ("douyin", "bilibili", "tiktok", "xiaohongshu"):
        return kind
    if kind and kind not in ("auto", "all"):
        return kind

    host = (urlparse(url).hostname or "").lower()
    for name, suffixes in (
        ("tiktok", TIKTOK_HOST_SUFFIXES),
        ("bilibili", BILI_HOST_SUFFIXES),
        ("xiaohongshu", XHS_HOST_SUFFIXES),
    ):
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    return ""


def _headers_for(url: str, platform: str = "") -> Dict[str, str]:
    """按平台选择请求头（平台未知时退回按域名判断）。

    三家 CDN 都做防盗链且只认各自的 Referer：用抖音的 Referer 去拉 B站 图片会 403，
    用 B站 的 Referer 去拉 TikTok 视频同样会 403（Akamai 上甚至直接 502）。
    所以这里按平台切换，而不是全局一套头，也不是只看域名。
    """
    kind = _platform_of(url, platform)
    if kind == "tiktok":
        return dict(TIKTOK_HEADERS)
    if kind == "bilibili":
        return dict(BILI_HEADERS)
    if kind == "xiaohongshu":
        return dict(XHS_HEADERS)
    return dict(CDN_HEADERS)


#: 出站用的两个 Session。**两者都必须关掉 trust_env** —— 这不是安全设置，是语义设置。
#:
#: 「不走代理」必须是真的不走。requests 在 proxies 为空时会去读 HTTP_PROXY /
#: HTTPS_PROXY / ALL_PROXY（`merge_environment_settings`），而本工具自己恰恰会通过
#: PROXY_ENV_KEYS 读这些变量去找 TikTok 代理 —— 于是在装了企业代理、抓包工具或
#: 沙箱（例如 WorkBuddy 会注入 `HTTP_PROXY=127.0.0.1:62800`）的机器上，
#: 「非 TikTok 直连」会**悄悄变成走代理**，表现为「抖音/B站 下载奇慢或莫名失败」。
#: 用户要的契约是「只有 TikTok 走代理」，那就得让直连这条路真的直连。
_DIRECT = requests.Session()
_DIRECT.trust_env = False
_TUNNEL = requests.Session()
_TUNNEL.trust_env = False


def _proxy_plan(url: str, platform: str = ""):
    """返回 `(session, proxies, 出口说明)`：TikTok 走用户配置的代理，其余一律直连。

    `proxies` 恒为 dict（非 None）—— 空字典表示「不挂代理」，配合上面的
    `trust_env=False` 才是真正的直连；返回 None 会让 requests 回退去读环境变量。
    """
    if _platform_of(url, platform) != "tiktok":
        return _DIRECT, {}, "直连"
    try:
        proxy, _source = tiktok_parser.current_proxy()
    except Exception:
        proxy = ""
    if not proxy:
        return _DIRECT, {}, "直连（没有探测到可用的 TikTok 代理）"
    return _TUNNEL, {"http": proxy, "https": proxy}, "经代理 %s" % proxy


def _proxies_for(url: str, platform: str = "") -> Dict[str, str]:
    """只取代理配置；空字典 = 不走代理。"""
    return _proxy_plan(url, platform)[1]


def _outlet(url: str, platform: str = "") -> str:
    """这次取流是「经代理 x」还是「直连」—— 只用于错误信息，让用户一眼看出走没走代理。"""
    return _proxy_plan(url, platform)[2]


def _describe(url: str, platform: str = "") -> str:
    """错误信息里的定位串：哪个域名、经谁取的。"""
    host = (urlparse(url).hostname or "").lower() or "?"
    return "%s · %s" % (host, _outlet(url, platform))


#: TikTok 经代理取流的尝试次数。链路比直连长（本机代理 → 境外节点 → CDN），
#: 偶发 5xx 很常见，重一次就能过的比例不低。其余平台保持单次 ——
#: 它们直连，失败基本是确定性的（403 防盗链 / 404 直链过期），重试只会拖慢报错。
TIKTOK_ATTEMPTS = 2
RETRY_PAUSE = 0.6

#: 「这条直链对本条请求是死的」的状态码 —— 重试同一条毫无意义，**换一条**才有意义。
REJECT_CODES = (403, 404)

#: 换用同组备胎直链的条数上限。实测一个档位给 3 条（webapp-prime ×2 + www ×1），
#: 多留一条余量防止平台以后加主机。
TIKTOK_ALT_LIMIT = 3


def _candidates(url: str, platform: str = "") -> List[str]:
    """这次取流可依次尝试的直链：首选 + 同组备胎。

    实测（2026-09-26）：TikTok 一个档位给 3 条不同主机的直链，Akamai 的
    `webapp-prime` 那两条**恒定 403**（与请求头无关），而同组的
    `www.tiktok.com` 会 302 到可用的 `*.tiktokcdn-us.com`。
    所以「换一条」是这类 403 的正解，重试同一条不是。
    """
    if _platform_of(url, platform) != "tiktok":
        return [url]
    rest = [u for u in tiktok_parser.alternatives(url) if u and u != url]
    return [url] + rest[:TIKTOK_ALT_LIMIT]


def _open_url(
    url: str,
    platform: str = "",
    headers: Optional[Dict[str, str]] = None,
    stream: bool = True,
    timeout: Any = 60,
    trace: Optional[List[str]] = None,
):
    """统一取流入口：选出口（代理/直连）→ 选请求头 → 必要时重试 → 换下一条直链。

    返回 requests.Response；连请求都发不出去时抛最后一次的 RequestException，
    4xx/5xx 响应原样返回（由调用方决定怎么报错，好带上 `_describe` 的定位串）。

    `trace` 传列表时，会把「每条直链分别试出了什么」记进去。这条诊断能力
    在真机上很关键：用户截图就能看出到底是主机被拒（403）还是出口不通
    （ProxyError / ConnectTimeout），而不是只看到一句 HTTP 状态码。
    """
    is_tiktok = _platform_of(url, platform) == "tiktok"
    attempts = TIKTOK_ATTEMPTS if is_tiktok else 1
    candidates = _candidates(url, platform)

    last_exc: Optional[Exception] = None
    for index, candidate in enumerate(candidates):
        has_next = index + 1 < len(candidates)
        session, proxies, _outlet = _proxy_plan(candidate, platform)
        cand_headers = dict(headers) if headers else _headers_for(candidate, platform)
        host = (urlparse(candidate).hostname or "?").lower()

        for attempt in range(attempts):
            last_attempt = attempt + 1 >= attempts
            try:
                resp = session.get(
                    candidate, headers=cand_headers, proxies=proxies,
                    stream=stream, timeout=timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if trace is not None:
                    trace.append("%s → %s" % (host, type(exc).__name__))
                if last_attempt:
                    if has_next:
                        break          # 这条彻底连不上，换下一条
                    raise              # 没有下一条了：保持原行为，抛出去
                time.sleep(RETRY_PAUSE)
                continue

            if trace is not None:
                trace.append("%s → HTTP %d" % (host, resp.status_code))

            # 403/404 对本条是确定性的：还有备胎就换，没有就原样交回调用方报错
            if resp.status_code in REJECT_CODES and has_next:
                resp.close()
                break
            # 只重试 5xx：403（防盗链）/404（直链过期）重试多少次都一样。
            if resp.status_code < 500 or last_attempt:
                return resp
            resp.close()
            if not last_attempt:
                time.sleep(RETRY_PAUSE)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("不可达")  # pragma: no cover - 上面的循环必然 return 或 raise


#: 读上游错误正文的上限。错误响应都是小 JSON，但**万一对端吐的是视频流**，
#: 也不能把 4GB 读进内存 —— 下载线程死在这里比报错更糟。
REASON_MAX_BYTES = 8192


def _reason_from(resp) -> str:
    """把上游 4xx/5xx 正文里的中文原因读出来，读不到返回空串。

    上游大多是自家的 /api/download，报错体是 FastAPI 的 `{"detail": "..."}`。
    这里刻意不复用 resp.text —— 它对 stream=True 的响应会读完整条流。
    """
    length = resp.headers.get("Content-Length")
    try:
        if length is not None and int(length) > REASON_MAX_BYTES:
            return ""
    except (TypeError, ValueError):
        return ""
    try:
        raw = resp.raw.read(REASON_MAX_BYTES, decode_content=True) or b""
    except Exception:  # noqa: BLE001 - 拿不到原因不该盖过原始错误
        return ""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:300]
    if isinstance(data, dict):
        detail = data.get("detail", data.get("error", ""))
        if isinstance(detail, (dict, list)):
            return json.dumps(detail, ensure_ascii=False)[:300]
        return str(detail)[:300]
    return text[:300]


def _error(message: str, platform: str = "") -> JSONResponse:
    """统一错误响应：始终 200，让前端能读到中文原因而不是网络错误。"""
    return JSONResponse(
        status_code=200, content={"success": False, "platform": platform, "error": message}
    )


@app.post("/api/parse")
def api_parse(req: ParseRequest):
    """解析入口：按 platform（或自动识别）分发到对应平台的解析器。"""
    text = (req.text or "").strip()
    platform = (req.platform or "").strip().lower()
    if platform in ("", "auto", "all"):
        platform = detect_platform(text)

    if platform == "bilibili":
        result = _parse_bilibili(text, req.page)
    elif platform == "tiktok":
        result = _parse_tiktok(text)
    elif platform == "xiaohongshu":
        result = _parse_xiaohongshu(text)
    elif platform == "douyin":
        result = _parse_douyin(text)
    # 裸数字 id 无法可靠区分平台（TikTok/Douyin 都是纯数字），只能提示用户显式选平台
    elif re.fullmatch(r"\d{15,25}", text):
        result = _error(
            "这看起来是一个纯数字作品 id，但光凭 id 分不清是哪个平台。"
            "请先把上方横条切到对应平台（TikTok 或 抖音），再粘贴 id。",
            platform,
        )
    else:
        result = _error("没识别出链接所属平台，目前支持抖音、B站、TikTok 与 小红书 链接")

    # 解析产出的直链主机当场登记，供取流层放行（见 _MEDIA_HOSTS 的说明）。
    # 放在这个唯一入口而不是各 parser 里：一条链路一个登记点，任何平台都不会漏。
    _remember_media_hosts(result)
    return result


def _parse_douyin(text: str):
    try:
        data = douyin_parse(text)
    except ParseError as exc:
        return _error(str(exc), "douyin")
    except Exception as exc:  # 兜底，避免把堆栈抛给前端
        return _error(f"解析异常：{type(exc).__name__}: {exc}", "douyin")

    if not data.get("video_urls") and not data.get("images"):
        return _error("未获取到可下载的视频或图片地址", "douyin")

    return {"success": True, "platform": "douyin", "data": data}


def _parse_bilibili(text: str, page: int = 0):
    try:
        data = bili_parse(text, page=page)
    except BiliError as exc:
        return _error(str(exc), "bilibili")
    except Exception as exc:
        return _error(f"解析异常：{type(exc).__name__}: {exc}", "bilibili")

    if not data.get("qualities"):
        return _error(data.get("hint") or "未获取到可下载的清晰度", "bilibili")

    # 告诉前端「这一档能不能在手机本机合流」。
    # B站 的 4K 常是 AV1，老设备上 MediaMuxer 封不了；提前标注比点下去才失败友好。
    # capability() 会把探测结果缓存，正常只在进程内第一次解析时真正探测一次。
    muxer = dash_muxer.capability()
    data["muxer"] = {
        "available": bool(muxer.get("available")),
        "backend": muxer.get("backend"),
        "reason": muxer.get("reason") or "",
    }
    ffmpeg = find_ffmpeg()
    data["ffmpeg"] = {"available": bool(ffmpeg), "path": ffmpeg}
    if muxer.get("available"):
        for item in data.get("qualities") or []:
            item["muxable"] = dash_muxer.video_codec_supported(item.get("codec"))
    data["cover_proxy"] = "/api/proxy?url=" + quote(data.get("cover") or "", safe="")
    return {"success": True, "platform": "bilibili", "data": data}


def _parse_tiktok(text: str):
    try:
        data = tiktok_parse(text)
    except TikTokError as exc:
        return _error(str(exc), "tiktok")
    except Exception as exc:
        return _error(f"解析异常：{type(exc).__name__}: {exc}", "tiktok")

    if not data.get("qualities"):
        return _error(data.get("hint") or "未获取到可下载的清晰度", "tiktok")

    # TikTok 的播放源是「音视频合一的渐进式 mp4」，单条直链即可播放，
    # 不像 B站 那样需要 ffmpeg 合流 —— 所以这里不返回 ffmpeg 信息。
    data["cover_proxy"] = "/api/proxy?url=" + quote(data.get("cover") or "", safe="")
    return {"success": True, "platform": "tiktok", "data": data}


def _parse_xiaohongshu(text: str):
    try:
        data = xhs_parse(text)
    except XHSError as exc:
        return _error(str(exc), "xiaohongshu")
    except Exception as exc:
        return _error(f"解析异常：{type(exc).__name__}: {exc}", "xiaohongshu")

    if not data.get("images") and not data.get("video_urls"):
        return _error("未获取到可下载的图片或视频地址", "xiaohongshu")

    # 小红书同为渐进式 mp4（无需合流）；封面走代理以带上自家 Referer
    if data.get("cover"):
        data["cover_proxy"] = "/api/proxy?url=" + quote(data["cover"], safe="")
    return {"success": True, "platform": "xiaohongshu", "data": data}


def _upstream_detail(url: str, platform: str, status: int,
                     trace: Optional[List[str]] = None) -> str:
    """上游 4xx/5xx 的中文原因：必须带「哪个域名、经谁取的」。

    只报一句「资源服务器返回 HTTP 502」是没法排查的 —— 用户和我们都看不出
    这次到底走没走代理、是哪个 CDN 回的。把出口和域名写进原因是成本最低、
    收益最高的一步（真机上用户截图即可定位）。

    `trace` 是 `_open_url` 记下的「每条直链试出了什么」。TikTok 一个档位会给
    3 条不同主机的直链，只有摊开看才知道是「主机被拒」还是「出口不通」。
    """
    text = "资源服务器返回 HTTP %d（%s）" % (status, _describe(url, platform))
    if trace and len(trace) > 1:
        text += "。依次试过的直链：%s" % "；".join(trace)
    if status >= 500 and _platform_of(url, platform) == "tiktok":
        text += (
            "。TikTok 这条是经代理取的，代理软件转发失败时自己也会回 502："
            "请确认它的分流规则覆盖了 TikTok 的 CDN 域名（tiktokcdn-us.com、"
            "tiktokv.com、akamaized.net 等），或把模式切到「全局」。"
        )
    elif status in REJECT_CODES and _platform_of(url, platform) == "tiktok":
        if trace and len(trace) > 1:
            text += (
                "。已把同一档位的其它直链都试过 —— 若全部被拒，说明这个出口被"
                "TikTok 的边缘节点挡住了，换一个代理节点通常就好了。"
            )
        else:
            # 只有一条可试（本进程没登记到备胎）时不能吹嘘「都试过了」，得说实话。
            # 备胎表是解析时登记的、只活在当前进程里 —— 所以这里要给一个能立刻
            # 照做的动作，而不是让用户对着 403 发呆。
            text += (
                "。该主机被 TikTok 的边缘节点直接拒绝（实测 Akamai 的 "
                "webapp-prime 主机对我们这类请求恒 403），且这次没有可换的备胎直链。"
                "请重新解析一次再下载 —— 解析时才会拿到同一档位的其它直链。"
            )
    return text


@app.get("/api/download")
def api_download(
    url: str = Query(..., description="媒体直链"),
    name: str = Query("douyin"),
    platform: str = Query("", description="平台，决定走代理还是直连（tiktok 之外的都直连）"),
):
    """流式代理下载：自动跟随重定向，并把中文文件名写入 Content-Disposition。"""
    _assert_allowed(url)

    trace: List[str] = []
    try:
        upstream = _open_url(url, platform, stream=True, timeout=60, trace=trace)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="拉取资源失败（%s）：%s" % (_describe(url, platform), exc),
        )

    if upstream.status_code >= 400:
        code = upstream.status_code
        upstream.close()
        raise HTTPException(
            status_code=502, detail=_upstream_detail(url, platform, code, trace)
        )

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    length = upstream.headers.get("Content-Length")

    headers = {
        "Content-Disposition": _content_disposition(name),
        "Cache-Control": "no-cache",
    }
    if length:
        headers["Content-Length"] = length

    return StreamingResponse(
        upstream.iter_content(chunk_size=65536),
        media_type=content_type,
        headers=headers,
    )


MAX_ZIP_ITEMS = 300
ZIP_FETCH_WORKERS = 8

_EXT_WHITELIST = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".mp3", ".m4a")


def _media_ext(url: str, content_type: Optional[str], hint: str = "") -> str:
    """推断扩展名：优先文件名提示，其次响应类型，最后按 URL 后缀。"""
    hinted = os.path.splitext(hint or "")[1].lower()
    if hinted in _EXT_WHITELIST:
        return ".jpg" if hinted == ".jpeg" else hinted
    ctype = (content_type or "").lower()
    if "mp4" in ctype or "video/" in ctype:
        return ".mp4"
    if "mpeg" in ctype or "mp3" in ctype or "audio/" in ctype:
        return ".mp3"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "webp" in ctype:
        return ".webp"
    if "png" in ctype:
        return ".png"
    path = urlparse(url).path.lower()
    for ext in (".jpeg", ".jpg", ".webp", ".png", ".mp4", ".mov", ".mp3", ".m4a"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _fetch_bytes(url: str, platform: str = "") -> Optional[tuple]:
    """下载单个资源，失败返回 None（不中断整包）。"""
    try:
        _assert_allowed(url)
        resp = _open_url(url, platform, stream=False, timeout=60)
    except (HTTPException, requests.RequestException):
        return None
    if resp.status_code >= 400 or not resp.content:
        return None
    return resp.content, resp.headers.get("Content-Type")


def _zip_entries(req: ZipRequest) -> List[tuple]:
    """把请求归一化为 [(url, 期望文件名)]，items 优先、urls 兼容。"""
    entries: List[tuple] = []
    if req.items:
        for it in req.items:
            url = (it.url or "").strip()
            if url.startswith("http"):
                entries.append((url, (it.name or "").strip()))
    elif req.urls:
        for url in req.urls:
            if isinstance(url, str) and url.startswith("http"):
                entries.append((url, ""))
    return entries[:MAX_ZIP_ITEMS]


def _build_zip_spool(entries, base: str):
    """把条目并行抓取并写成一个 ZIP，返回 (spool, 成功文件数)；失败时抛异常。

    调用方负责在适当时机关闭 spool。
    """
    spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    saved = 0
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_STORED) as archive:
            for start in range(0, len(entries), ZIP_FETCH_WORKERS):
                batch = entries[start : start + ZIP_FETCH_WORKERS]
                with ThreadPoolExecutor(max_workers=ZIP_FETCH_WORKERS) as pool:
                    results = list(pool.map(lambda e: _fetch_bytes(e[0]), batch))
                for offset, item in enumerate(results):
                    if not item:
                        continue
                    content, ctype = item
                    url, hint = batch[offset]
                    index = start + offset + 1
                    if hint:
                        name = re.sub(r'[\\/:*?"<>|]', "_", hint)
                    else:
                        name = f"{base}_{index:03d}{_media_ext(url, ctype)}"
                    archive.writestr(name, content)
                    saved += 1
        spool.seek(0)
    except Exception:
        spool.close()
        raise
    return spool, saved


@app.post("/api/zip")
def api_zip(req: ZipRequest):
    """把图集图片与实况动态视频打包为 ZIP 一次性下载。

    浏览器对连续触发的多文件下载有拦截（尤其 50 张以上），打包可规避；
    资源并行抓取、边写边落盘（超过 64MB 自动转为临时文件），避免占用过多内存。
    """
    entries = _zip_entries(req)
    if not entries:
        raise HTTPException(status_code=400, detail="没有可打包的资源地址")

    base = _safe_filename(req.name)
    spool, saved = _build_zip_spool(entries, base)

    if not saved:
        spool.close()
        raise HTTPException(status_code=502, detail="所有资源均下载失败，请稍后重试")

    headers = {
        "Content-Disposition": _content_disposition(f"{base}_打包_{saved}个文件.zip"),
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(
        iter(lambda: spool.read(1024 * 1024), b""),
        media_type="application/zip",
        headers=headers,
    )


# ---------------------------------------------------------------- 两段式打包
# 用于安卓（WebView / 系统下载器）场景：
#   WebView 的 DownloadListener 与系统下载器只接受 http(s) 直链，
#   既发不出 POST，也拿不到 blob: URL。所以拆成两步 ——
#     1) POST /api/zip/prepare  建任务，返回一次性 token（URL 里只带 token，避免超长）
#     2) GET  /api/zip/get?token=...  取走 zip 字节流
# 桌面浏览器仍走上面的单步 POST，行为不变。

ZIP_TASK_TTL = 10 * 60  # 秒；过期任务自动清理，避免内存里堆积播放列表 URL

_zip_tasks: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_zip_tasks_lock = threading.Lock()


def _zip_tasks_gc() -> None:
    now = time.time()
    with _zip_tasks_lock:
        stale = [k for k, (stamp, _) in _zip_tasks.items() if now - stamp > ZIP_TASK_TTL]
        for key in stale:
            _zip_tasks.pop(key, None)


@app.post("/api/zip/prepare")
def api_zip_prepare(req: ZipRequest):
    """建打包任务，返回给系统下载器用的一次性 token。"""
    entries = _zip_entries(req)
    if not entries:
        raise HTTPException(status_code=400, detail="没有可打包的资源地址")

    _zip_tasks_gc()
    base = _safe_filename(req.name)
    token = secrets.token_urlsafe(18)
    with _zip_tasks_lock:
        _zip_tasks[token] = (time.time(), {"entries": entries, "base": base})
    return {
        "token": token,
        "filename": f"{base}_打包.zip",
        "count": len(entries),
    }


@app.get("/api/zip/get")
def api_zip_get(token: str = Query("", description="由 /api/zip/prepare 返回的一次性 token")):
    """用 token 取走打包结果（一次性，取完即失效）。"""
    with _zip_tasks_lock:
        item = _zip_tasks.pop(token, None)
    if not item:
        raise HTTPException(
            status_code=404,
            detail="打包任务不存在或已过期，请回到页面重新点击「打包下载」",
        )

    payload = item[1]
    spool, saved = _build_zip_spool(payload["entries"], payload["base"])
    if not saved:
        spool.close()
        raise HTTPException(status_code=502, detail="所有资源均下载失败，请稍后重试")

    headers = {
        "Content-Disposition": _content_disposition(
            f"{payload['base']}_打包_{saved}个文件.zip"
        ),
        "Cache-Control": "no-cache",
    }
    return StreamingResponse(
        iter(lambda: spool.read(1024 * 1024), b""),
        media_type="application/zip",
        headers=headers,
        background=BackgroundTask(spool.close),
    )


# ---------------------------------------------------------------- 保存到本机
# 安卓端「点了下载没反应」的根治方案。
#
# 原先的链路是「WebView 点击 → DownloadListener → 系统 DownloadManager」，
# 这条链在安卓上有若干**静默**失败点（回调不来 / DownloadManager 取不到
# 127.0.0.1 的明文 HTTP / 失败不回到我们的代码也不出通知），详见 disk_saver
# 模块头注释。现在改为由本进程自己取流、自己落盘、自己报进度：
#
#     POST /api/save         建任务（取流 + 落盘都在服务端线程里做）
#     GET  /api/save/status  查单个任务进度（前端按 1 秒轮询）
#     GET  /api/save/list    查最近任务（页面刷新后据此重新挂上进度）
#     POST /api/save/cancel  取消
#
# 桌面上这套接口同样可用（写进用户目录的 Downloads），所以整条链路在本地就能
# 测完整，不必等一次 13 分钟的云端构建去试。


class SaveRequest(BaseModel):
    """保存请求。

    kind=url：source 为直链，或本机接口路径（以 /api/ 开头）。后者由下载线程
              回环请求本服务 —— 这样任何「流式返回字节」的既有接口都自动获得
              「存到手机」的能力，不必为每个接口各写一遍落盘逻辑。
    kind=zip：items 为图集条目，边抓边打进 zip，因此能报「已打包 12/30」。
    """

    kind: str = "url"
    source: Optional[str] = None
    items: Optional[List[ZipItem]] = None
    #: 不给名字也能用：会从 source（本机接口的 name 参数 / 直链路径末段）推断。
    #: 默认值刻意留空而不是 "download" —— 否则「推断」这一步永远不会被执行，
    #: 用户的视频会被一律存成 download。
    name: Optional[str] = None
    mime: Optional[str] = None


class SaveCancelRequest(BaseModel):
    id: str


_saver = disk_saver.SaveManager()

#: 回环请求专用会话。
#: 必须关掉 trust_env —— 否则机器上只要有 HTTP_PROXY（企业代理、抓包工具、
#: 或本工具的 TikTok 代理探测留下的环境变量），连 127.0.0.1 的请求也会被
#: 丢给那个代理，表现为「下载一直停在 0%」。
_LOOPBACK = requests.Session()
_LOOPBACK.trust_env = False


def _save_local_url(source: str) -> str:
    """本机接口路径 → 回环 URL。

    判据只有一条：必须以 `/api/` 开头。刻意不维护「哪些端点会吐字节」的白名单 ——
    那种清单一定会漏（第一版就漏了 `/api/bili/danmaku`，用户点「下载弹幕」
    照样没反应），而且每加一个端点都要回来改。
    唯一要挡的是 `/api/save` 自己：否则会自我递归。
    """
    if not source.startswith("/api/"):
        raise disk_saver.SaveError("只能把本机 /api/ 接口作为下载源：%s" % source)
    if source == "/api/save" or source.startswith("/api/save?") or source.startswith("/api/save/"):
        raise disk_saver.SaveError("不能把 /api/save 自己作为下载源")
    return "http://127.0.0.1:%d%s" % (ACTUAL_PORT, source)


def _name_from_source(source: str) -> str:
    """没显式给文件名时，从地址里推断一个。

    两种地址各有各的「名字藏在哪里」：
      * 本机接口（/api/download?url=...&name=xxx.mp4）—— 名字在自己的查询串里；
      * 远端直链（https://cdn/.../%E8%8B%8F%E5%B7%9E.mp4?a=b）—— 在路径末段，
        且必须解码百分号，否则会存出一个 `%E8%8B%8F%E5%B7%9E.mp4`。
    """
    if source.startswith("/"):
        values = parse_qs(urlparse(source).query).get("name") or []
        return values[0].strip() if values else ""
    base = unquote(os.path.basename(urlparse(source).path or ""))
    return base.strip().strip(".")[:100]


def _open_save_stream(payload, task, sink) -> disk_saver.Stream:
    """kind=url 的取流：直链或本机接口，两者最终都是「一个只读字节流」。"""
    source = (payload.get("source") or "").strip()
    trace: List[str] = []
    if source.startswith("/"):
        # 读超时给得很宽：/api/zip/get 要先抓完所有图片才开始吐字节，
        # 默认的 60 秒会在「打包 50 张图」时被误杀。
        resp = _LOOPBACK.get(_save_local_url(source), stream=True, timeout=(10, 900))
    else:
        _assert_allowed(source)
        resp = _open_url(
            source, payload.get("platform") or "", stream=True, timeout=(15, 120),
            trace=trace,
        )
    if resp.status_code >= 400:
        code = resp.status_code
        # 上游常是自家的 /api/download，它的 4xx/5xx 正文里带中文原因
        # （FastAPI 的 {"detail": "..."}）。早先这里只搬状态码，
        # 于是「拉取资源失败（域名 · 经代理 x）：ProxyError(...)」被压成一句
        # 「资源服务器返回 HTTP 502」，排查时等于什么都没说。
        detail = _reason_from(resp)
        if not detail and trace:
            # 拿不到上游正文时，至少把「试过哪些直链、各自什么结果」交代清楚
            detail = _upstream_detail(source, payload.get("platform") or "", code, trace)
        resp.close()
        raise disk_saver.SaveError(detail or "资源服务器返回 HTTP %d" % code)
    total = int(resp.headers.get("Content-Length") or 0)
    return disk_saver.Stream(total, resp.iter_content(disk_saver.CHUNK), resp.close)


def _open_save_zip(payload, task, sink) -> disk_saver.Stream:
    """kind=zip：边抓边写。字节由 write_zip_into 直接写进 sink，这里不产出块。"""
    entries = _zip_entries(
        ZipRequest(items=payload.get("items"), name=payload.get("name") or "")
    )
    if not entries:
        raise disk_saver.SaveError("没有可打包的资源地址")
    base = _safe_filename(payload.get("name") or "download")

    def chunks():
        disk_saver.write_zip_into(
            sink, entries, _fetch_bytes, task, base, _media_ext, _safe_filename
        )
        return
        yield  # pragma: no cover - 只为把本函数标记成生成器，不产出任何块

    return disk_saver.Stream(0, chunks())


_saver.register("url", _open_save_stream)
_saver.register("zip", _open_save_zip)


@app.post("/api/save")
def api_save(req: SaveRequest):
    """把资源保存到本机（安卓上即公共「下载」目录），返回任务 id。"""
    kind = (req.kind or "url").strip().lower()

    if kind == "zip":
        items = [it for it in (req.items or []) if (it.url or "").startswith("http")]
        if not items:
            raise HTTPException(status_code=400, detail="没有可打包的资源地址")
        base_name = (req.name or "").strip() or "download"
        payload = {"items": items, "name": base_name}
        name = "%s_打包.zip" % _safe_filename(base_name)
        mime = "application/zip"
    elif kind == "url":
        source = (req.source or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="缺少下载地址")
        if source.startswith("/"):
            # 提前校验前缀：否则错误要等下载线程跑起来才暴露，前端只看到「排队中」
            try:
                _save_local_url(source)
            except disk_saver.SaveError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        elif source.startswith("http"):
            _assert_allowed(source)
        else:
            raise HTTPException(status_code=400, detail="不支持的下载地址")
        payload = {"source": source}
        # 三层兜底：调用方给的名字 → 从地址推断 → 最后的死名字。
        # disk_saver 那边还会再兜一次，这里给出更贴近语义的名字。
        name = (req.name or "").strip() or _name_from_source(source) or "download"
        mime = req.mime or ""
    else:
        raise HTTPException(status_code=400, detail="不支持的保存类型：%s" % kind)

    task = _saver.start(kind, payload, name, mime)
    return task.snapshot()


@app.get("/api/save/status")
def api_save_status(id: str = Query("", description="由 /api/save 返回的任务 id")):
    """查单个保存任务的进度。"""
    task = _saver.get(id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task.snapshot()


@app.get("/api/save/list")
def api_save_list(limit: int = Query(20, ge=1, le=100)):
    """最近的保存任务。

    前端刷新页面后据此重新挂上「正在下载」的进度条 —— 否则一刷新
    进度就丢了，用户只能靠猜。
    """
    return {"tasks": _saver.listing(limit)}


@app.post("/api/save/cancel")
def api_save_cancel(req: SaveCancelRequest):
    """取消下载。临时文件会被删掉，公共目录里不会留下半个文件。"""
    return {"success": _saver.cancel(req.id)}


@app.get("/api/proxy")
def api_proxy(
    request: Request,
    url: str = Query(...),
    platform: str = Query("", description="平台，决定走代理还是直连（tiktok 之外的都直连）"),
):
    """媒体代理，支持 Range，用于前端预览视频与显示封面。"""
    _assert_allowed(url)
    return _proxy_response(url, request.headers.get("Range"), platform)



def _proxy_response(url: str, range_header: Optional[str], platform: str = ""):
    headers = _headers_for(url, platform)
    if range_header:
        headers["Range"] = range_header

    try:
        upstream = _open_url(url, platform, headers=headers, stream=True, timeout=60)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502, detail="代理请求失败（%s）：%s" % (_describe(url, platform), exc)
        )

    # 重定向后的最终地址同样需要校验，防止跳转到白名单外域名
    if upstream.url and upstream.url != url:
        try:
            _assert_allowed(upstream.url)
        except HTTPException:
            upstream.close()
            raise

    resp_headers = {
        "Cache-Control": "public, max-age=3600",
        "Accept-Ranges": "bytes",
    }
    for key in ("Content-Type", "Content-Length", "Content-Range", "ETag", "Last-Modified"):
        if key in upstream.headers:
            resp_headers[key] = upstream.headers[key]

    status = upstream.status_code
    return StreamingResponse(
        upstream.iter_content(chunk_size=65536),
        status_code=status,
        media_type=upstream.headers.get("Content-Type", "application/octet-stream"),
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# B站 专用接口
#
# 背景：B站 高清恒为 DASH（视频轨与音频轨分离），要交付「一个能直接播放的 mp4」
# 必须服务端用 ffmpeg 合流。因此下载不是简单反代，而是「取流 → 合流 → 回传」。
# ---------------------------------------------------------------------------

FFMPEG_TIMEOUT = 1800


def _ffmpeg_header_arg() -> str:
    """ffmpeg 的 -headers 参数：每行 `Key: Value\\r\\n`，用于过 B站 CDN 的 Referer 校验。"""
    return "".join(f"{key}: {value}\r\n" for key, value in BILI_HEADERS.items())


def _tail(raw: Optional[bytes], limit: int = 400) -> str:
    """取子进程 stderr 的末尾几行，用于给用户可读的失败原因。"""
    text = (raw or b"").decode("utf-8", "replace").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (" | ".join(lines[-4:]) or "未知错误")[:limit]


def _run_ffmpeg(args: List[str]) -> subprocess.CompletedProcess:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(
            status_code=503,
            detail=(
                "未找到 ffmpeg，无法合流 B站 高清（DASH 音视频分离）。"
                "请安装 ffmpeg，或用环境变量 FFMPEG_PATH 指向其可执行文件。"
            ),
        )
    try:
        return subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", *args],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="合流超时（视频过大或网络过慢）")


def _bili_play(bvid: str, cid: int) -> Dict[str, Any]:
    try:
        return bili_parser.get_client().playurl(bvid, cid)
    except BiliError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"请求 B站 接口失败：{exc}")


def _with_ext(name: str, ext: str) -> str:
    filename = _safe_filename(name)
    if not filename.lower().endswith(ext):
        filename += ext
    return filename


@app.get("/api/bili/tracks")
def api_bili_tracks(bvid: str = Query(...), cid: int = Query(...), duration: int = Query(0)):
    """切换分P后重新取该 P 的真实可用档位（B站 每个 cid 的档位可能不同）。"""
    play = _bili_play(bvid, cid)
    qualities = bili_parser.build_qualities(play, duration)
    return {
        "success": True,
        "platform": "bilibili",
        "qualities": qualities,
        "audio": bili_parser.audio_info(play),
    }


@app.get("/api/bili/download")
def api_bili_download(
    bvid: str = Query(...),
    cid: int = Query(...),
    qn: int = Query(0, description="目标清晰度，0 表示取该视频的最高可用档"),
    name: str = Query("bilibili"),
):
    """把选定清晰度的视频轨与音频轨用 ffmpeg 无损合流（-c copy），再回传 mp4。"""
    play = _bili_play(bvid, cid)
    tracks = bili_parser.extract_tracks(play)
    video_map = tracks["video"]
    if not video_map:
        raise HTTPException(status_code=502, detail="该视频没有可用的视频轨")

    target = qn if qn in video_map else max(video_map)
    video_url = bili_parser.track_url(video_map[target])
    audio = tracks["audio"]
    audio_url = bili_parser.track_url(audio) if audio else None
    if not video_url:
        raise HTTPException(status_code=502, detail="视频轨地址为空")

    workdir = tempfile.mkdtemp(prefix="bili_")
    out_path = os.path.join(workdir, "merged.mp4")
    header_arg = _ffmpeg_header_arg()
    args = ["-headers", header_arg, "-i", video_url]
    if audio_url:
        args += ["-headers", header_arg, "-i", audio_url, "-map", "0:v:0", "-map", "1:a:0"]
    args += ["-c", "copy", "-movflags", "+faststart", "-y", out_path]

    proc = _run_ffmpeg(args)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"ffmpeg 合流失败：{_tail(proc.stderr)}")

    headers = {
        "Content-Disposition": _content_disposition(_with_ext(name, ".mp4")),
        "Cache-Control": "no-cache",
    }
    return FileResponse(
        out_path,
        media_type="video/mp4",
        headers=headers,
        background=BackgroundTask(shutil.rmtree, workdir, True),
    )


@app.get("/api/bili/audio")
def api_bili_audio(
    bvid: str = Query(...), cid: int = Query(...), name: str = Query("bilibili")
):
    """把 B站 的音频轨转成 MP3（libmp3lame VBR 最高档）。"""
    play = _bili_play(bvid, cid)
    audio = bili_parser.extract_tracks(play)["audio"]
    audio_url = bili_parser.track_url(audio) if audio else None
    if not audio_url:
        raise HTTPException(status_code=502, detail="该视频没有独立音频轨")

    workdir = tempfile.mkdtemp(prefix="biliau_")
    out_path = os.path.join(workdir, "audio.mp3")
    args = [
        "-headers", _ffmpeg_header_arg(),
        "-i", audio_url,
        "-vn", "-c:a", "libmp3lame", "-q:a", "0",
        "-y", out_path,
    ]
    proc = _run_ffmpeg(args)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"音频转换失败：{_tail(proc.stderr)}")

    headers = {
        "Content-Disposition": _content_disposition(_with_ext(name, ".mp3")),
        "Cache-Control": "no-cache",
    }
    return FileResponse(
        out_path,
        media_type="audio/mpeg",
        headers=headers,
        background=BackgroundTask(shutil.rmtree, workdir, True),
    )


@app.get("/api/bili/danmaku")
def api_bili_danmaku(
    bvid: str = Query(...), cid: int = Query(0), name: str = Query("danmaku")
):
    """导出该分P的弹幕 XML。cid 缺省时自动从 view 接口取。"""
    client = bili_parser.get_client()
    try:
        target = cid or (client.view(bvid=bvid) or {}).get("cid")
        if not target:
            raise BiliError("没能确定该分P的弹幕编号（cid）")
        xml = client.danmaku(target)
    except BiliError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"弹幕拉取失败：{exc}")

    if not xml:
        raise HTTPException(status_code=502, detail="该视频没有弹幕")

    return Response(
        content=xml,
        media_type="application/xml; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(_with_ext(name, ".xml"))},
    )


# ---------------------------------------------------------------- 本机合流
# 安卓端把 DASH 音视频合流成一个 mp4 的接口。
#
# 桌面版走 /api/bili/download：服务端 ffmpeg `-c copy` 一步出片。
# APK 里没有 ffmpeg 命令行程序，改用系统自带的 MediaExtractor + MediaMuxer
# （实现见 dash_muxer.py）。能力差异只有一处：安卓端固定用 AAC 音轨。
#
# 为什么同样是两段式：
#   1) 合流要跑几十秒到几分钟（4K 长视频更久），不能让一个 HTTP 请求一直挂着；
#   2) 成品最终要交给 WebView 的 DownloadListener / 系统下载器，它只认
#      http(s) 直链，发不出 POST。
# 所以拆成 ——
#     POST /api/bili/mux/prepare   起任务，返回 token
#     GET  /api/bili/mux/status    轮询进度
#     GET  /api/bili/mux/get       取走成品 mp4（一次性）
#     POST /api/bili/mux/cancel    取消（顺带清掉几百 MB 的临时文件）

MUX_JOB_TTL = 30 * 60  # 秒；结束后超过这个时间未取走就回收，连同临时文件


class MuxRequest(BaseModel):
    bvid: str
    cid: int
    qn: int = 0
    name: str = "bilibili"
    #: 视频总时长（秒），只用于算进度百分比
    duration: int = 0


class MuxCancelRequest(BaseModel):
    token: str


class _MuxJob:
    """一次合流任务的状态。

    后台线程写、HTTP 轮询读，所以全部字段走锁 —— 否则前端会读到
    「state 还是 running 但 percent 已经 100」这种半截状态。
    """

    def __init__(self, token: str, filename: str, workdir: str) -> None:
        self.token = token
        self.filename = filename
        self.workdir = workdir
        self.path = os.path.join(workdir, "merged.mp4")
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self.created = time.time()
        self.finished = 0.0
        self.state = "running"  # running / ready / error / canceled
        self.phase = "prepare"  # prepare / download / mux / done
        self.percent = 0
        self.detail = "正在准备…"
        self.error = ""
        self.size = 0

    def update(self, phase: str, percent: int, detail: str) -> None:
        with self._lock:
            self.phase = phase
            self.percent = int(percent)
            self.detail = detail

    def finish(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self.state = "ready"
            self.phase = "done"
            self.percent = 100
            self.detail = "合流完成，正在交给系统下载器…"
            self.size = int(result.get("bytes") or 0)
            if not self.size and os.path.isfile(self.path):
                self.size = os.path.getsize(self.path)
            self.finished = time.time()

    def fail(self, message: str, state: str = "error") -> None:
        with self._lock:
            self.state = state
            self.error = message
            self.detail = message
            self.finished = time.time()
        # 失败/取消留下的只有半个 mp4 和一堆分片，没有保留价值
        shutil.rmtree(self.workdir, ignore_errors=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "state": self.state,
                "phase": self.phase,
                "percent": self.percent,
                "detail": self.detail,
                "error": self.error,
                "filename": self.filename,
                "size": self.size,
                "ready": self.state == "ready",
            }


_MUX_JOBS: Dict[str, _MuxJob] = {}
_MUX_LOCK = threading.Lock()


def _mux_jobs_gc() -> None:
    """回收已结束且长时间没被取走的任务（连临时目录一起删）。"""
    now = time.time()
    stale: List[_MuxJob] = []
    with _MUX_LOCK:
        for token, job in list(_MUX_JOBS.items()):
            if job.state == "running" or not job.finished:
                continue
            if now - job.finished > MUX_JOB_TTL:
                stale.append(_MUX_JOBS.pop(token))
    for job in stale:
        shutil.rmtree(job.workdir, ignore_errors=True)


def _mux_job(token: str) -> _MuxJob:
    with _MUX_LOCK:
        job = _MUX_JOBS.get(token)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="合流任务不存在或已过期，请回到页面重新点击下载",
        )
    return job


def _bili_mux_audio(play: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """给本机合流挑音轨：固定用普通 AAC。

    `extract_tracks` 会优先给「无损 / 杜比全景声」，但那两类编码塞进 MP4 的
    兼容性很差（不少设备直接不出声）。B站 的 AAC 本身有 192K，日常听感差异
    可以忽略，所以这里只认 mp4a/aac；一条都挑不到时才退回原来的选择。
    """
    dash = play.get("dash") or {}
    best: Optional[Dict[str, Any]] = None
    for track in dash.get("audio") or []:
        codec = str(track.get("codecs") or "").lower()
        if not codec.startswith(("mp4a", "aac")):
            continue
        if best is None or (track.get("bandwidth") or 0) > (best.get("bandwidth") or 0):
            best = track
    if best is None:
        return bili_parser.extract_tracks(play)["audio"]
    return best


def _pick_mux_quality(
    qualities: List[Dict[str, Any]], qn: int
) -> Tuple[Optional[Dict[str, Any]], str]:
    """定下要合流的档位。返回 (档位 或 None, 需要告诉用户的提示)。

    优先尊重前端选的档位；选的那档本机封不了（比如 4K 是 AV1）才自动降档，
    并且把「为什么降」讲清楚 —— 静默降档用户会以为是 bug。
    """
    supported = dash_muxer.video_codec_supported

    if qn:
        wanted = next((q for q in qualities if q.get("qn") == qn), None)
        if wanted is not None and supported(wanted.get("codec")):
            return wanted, ""

    chosen, note = dash_muxer.select_muxable_quality(qualities, supported)
    if chosen is None:
        return None, note

    if qn:
        wanted = next((q for q in qualities if q.get("qn") == qn), None)
        if wanted is not None:
            note = "所选清晰度 %s（%s）手机端无法封装，已改用 %s（%s）" % (
                wanted.get("label"),
                wanted.get("codec") or "未知编码",
                chosen.get("label"),
                chosen.get("codec") or "未知编码",
            )
    return chosen, note


def _run_mux_job(
    job: _MuxJob, video_url: str, audio_url: Optional[str], duration: int
) -> None:
    """后台线程：下载/取流 → 交错写样例 → 落盘 mp4。"""

    def on_progress(phase: str, percent: int, detail: str) -> None:
        job.update(phase, percent, detail)

    try:
        result = dash_muxer.mux(
            video_url,
            audio_url,
            job.path,
            headers=BILI_HEADERS,
            duration=duration,
            on_progress=on_progress,
            should_cancel=job.cancel.is_set,
        )
        job.finish(result)
    except dash_muxer.MuxCanceled:
        job.fail("已取消", state="canceled")
    except dash_muxer.MuxError as exc:
        job.fail(str(exc))
    except Exception as exc:  # 兜住没预料到的 JNI 异常，别让线程静默死掉
        job.fail("合流失败：%s: %s" % (type(exc).__name__, exc))


@app.get("/api/bili/mux/capability")
def api_bili_mux_capability():
    """本机合流能力（是否可用、系统版本、哪些编码能封）。用于远程排错。"""
    payload = dash_muxer.capability()
    payload.update({"success": True, "platform": "bilibili"})
    return payload


@app.post("/api/bili/mux/prepare")
def api_bili_mux_prepare(req: MuxRequest):
    """起一个本机合流任务，返回取件用的 token。"""
    if not dash_muxer.is_available():
        raise HTTPException(
            status_code=503,
            detail=dash_muxer.capability().get("reason") or "本机合流不可用",
        )

    play = _bili_play(req.bvid, req.cid)
    tracks = bili_parser.extract_tracks(play)
    if not tracks["video"]:
        raise HTTPException(status_code=502, detail="该视频没有可用的视频轨")

    qualities = bili_parser.build_qualities(play, req.duration)
    chosen, note = _pick_mux_quality(qualities, req.qn)
    if chosen is None:
        raise HTTPException(status_code=502, detail=note)

    video_url = chosen.get("url")
    if not video_url:
        raise HTTPException(status_code=502, detail="视频轨地址为空")

    audio = _bili_mux_audio(play)
    audio_url = bili_parser.track_url(audio) if audio else None

    _mux_jobs_gc()
    base = _safe_filename(req.name)
    filename = base + ".mp4"
    workdir = tempfile.mkdtemp(prefix="bili_mux_")
    token = secrets.token_urlsafe(18)
    job = _MuxJob(token, filename, workdir)
    with _MUX_LOCK:
        _MUX_JOBS[token] = job

    threading.Thread(
        target=_run_mux_job,
        args=(job, video_url, audio_url, req.duration),
        daemon=True,
    ).start()

    return {
        "success": True,
        "token": token,
        "filename": filename,
        "quality": {
            "qn": chosen.get("qn"),
            "label": chosen.get("label"),
            "codec": chosen.get("codec"),
        },
        "audio": (audio or {}).get("_label") or "",
        "note": note,
    }


@app.get("/api/bili/mux/status")
def api_bili_mux_status(token: str = Query("", description="prepare 返回的 token")):
    """查合流进度。前端按 1~2 秒轮询。"""
    return _mux_job(token).snapshot()


@app.get("/api/bili/mux/get")
def api_bili_mux_get(token: str = Query("", description="prepare 返回的 token")):
    """取走成品 mp4（一次性）。取完即回收任务与临时文件。"""
    job = _mux_job(token)
    state = job.snapshot()
    if state["state"] == "running":
        raise HTTPException(status_code=409, detail="合流还没完成，请稍候")
    if state["state"] != "ready" or not os.path.isfile(job.path):
        raise HTTPException(
            status_code=502, detail=state["error"] or "合流失败，请重试"
        )

    with _MUX_LOCK:
        _MUX_JOBS.pop(token, None)

    return FileResponse(
        job.path,
        media_type="video/mp4",
        headers={
            "Content-Disposition": _content_disposition(job.filename),
            "Cache-Control": "no-store",
        },
        background=BackgroundTask(shutil.rmtree, job.workdir, True),
    )


@app.post("/api/bili/mux/cancel")
def api_bili_mux_cancel(req: MuxCancelRequest):
    """取消合流。几百 MB 的临时文件会随任务一起清掉。"""
    job = _mux_job(req.token)
    job.cancel.set()
    return {"success": True, "state": job.snapshot()["state"]}


# ---------- B站 账号配置（SESSDATA / cookies） ----------


def _bili_config_snapshot(probe: bool = True) -> Dict[str, Any]:
    """汇总一次配置状态：来源摘要 + 真实账号信息。

    只回打码值（`_mask`）与 cookie 名列表 —— 前端需要「确认填的是哪一个」
    和「判断是不是复制不全」，但没有任何理由拿到明文。
    """
    status = bili_parser.cookie_status()
    account: Dict[str, Any] = {"logged_in": False, "vip": False, "uname": "", "checked": False, "error": ""}
    if probe and status.get("configured"):
        try:
            info = bili_parser.check_account()
            account.update({"logged_in": info["logged_in"], "vip": info["vip"], "uname": info["uname"]})
            account["checked"] = True
        except BiliError as exc:
            account["error"] = str(exc)
        except requests.RequestException as exc:
            account["error"] = f"网络异常，未能校验登录态：{exc}"
    return {"status": status, "account": account, "ffmpeg_available": bool(find_ffmpeg())}


@app.get("/api/bili/config")
def api_bili_config_get():
    """读取当前 B站 账号配置状态（顺带探一次 nav，确认登录态是否真的生效）。"""
    payload = _bili_config_snapshot()
    payload.update({"success": True, "platform": "bilibili"})
    return payload


@app.post("/api/bili/config")
def api_bili_config_save(req: BiliConfigRequest):
    """保存用户粘贴的 cookie。

    流程刻意做成「**先验证、后落盘**」：一份复制不全的 cookie 若直接覆盖，
    会把原本可用的配置毁掉，用户还看不出问题出在哪。只有通过 nav 校验
    （或用户显式点「仍然保存」）才写文件。
    """
    cookies = bili_parser.parse_cookie_input(req.text or "")
    if not cookies.get("SESSDATA"):
        return _error(
            "粘贴内容里没找到 SESSDATA。请确认复制的是 B站（bilibili.com）的 Cookie，"
            "且包含 SESSDATA 这一项 —— 插件导出的 cookies.txt 直接整份粘进来即可",
            "bilibili",
        )

    # 先验证：failed=明确不认；unknown=网络/风控导致测不出（不能归咎于凭据）
    verdict, detail = "unknown", ""
    try:
        info = bili_parser.check_account(cookies)
        if info["logged_in"]:
            verdict = "ok"
            detail = info.get("uname") or ""
        else:
            verdict = "invalid"
    except BiliError as exc:
        detail = str(exc)
    except requests.RequestException as exc:
        detail = f"网络异常：{exc}"

    parsed = {
        "cookie_names": sorted(cookies.keys()),
        "sessdata_length": len(cookies.get("SESSDATA") or ""),
    }

    if verdict == "invalid" and not req.force:
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "platform": "bilibili",
                "verified": verdict,
                "parsed": parsed,
                "error": "这份 Cookie 没通过 B站 登录校验"
                + (f"（{detail}）" if detail else "（B站 返回未登录）")
                + "。多半是复制不完整或已过期（改密码、退出登录都会让旧的立刻失效），"
                "建议重新导出一次；若确认无误仍要保存，请点「仍然保存」。",
            },
        )

    bili_parser.write_config_file(cookies)
    # cookie 变了必须清掉客户端缓存，否则下次解析还在用旧的登录态
    bili_parser.reset_clients()

    payload = _bili_config_snapshot()
    payload.update(
        {
            "success": True,
            "platform": "bilibili",
            "verified": verdict,
            "parsed": parsed,
            "saved": True,
            "warning": _bili_config_warning(verdict, detail),
        }
    )
    return payload


def _bili_config_warning(verdict: str, detail: str) -> str:
    if verdict == "ok":
        return f"已验证登录成功{f'（{detail}）' if detail else ''}"
    if verdict == "invalid":
        return "已按你的要求强制保存，但 B站 未认这份 Cookie，解析时仍会按未登录给到 480P"
    return f"已保存，但本次未能完成登录校验（{detail or '网络或风控异常'}），解析一次即可确认是否生效"


@app.delete("/api/bili/config")
def api_bili_config_clear():
    """清除已保存的 cookie（写空表，保留文件以便用户看到配置项确实存在）。"""
    bili_parser.write_config_file({})
    bili_parser.reset_clients()
    payload = _bili_config_snapshot(probe=False)
    payload.update({"success": True, "platform": "bilibili", "cleared": True})
    payload["warning"] = (
        "已清除本地保存的 Cookie；但环境变量 BILI_SESSDATA 仍然生效，清空它需修改系统环境变量"
        if payload["status"].get("env_override")
        else "已清除本地保存的 Cookie，当前回到未登录状态（最高 480P）"
    )
    return payload


# ---------- TikTok 代理配置 ----------


def _tiktok_config_snapshot(probe: bool = True) -> Dict[str, Any]:
    """代理状态摘要：来源、当前值、能否真的连上 TikTok。"""
    return {
        "proxy": tiktok_parser.proxy_status(probe=probe),
        "config_file": os.path.basename(tiktok_parser.CONFIG_PATH),
    }


@app.get("/api/tiktok/config")
def api_tiktok_config_get():
    """读取 TikTok 代理状态（顺带实测一次连通性，区分「端口开着」与「真能出网」）。"""
    payload = _tiktok_config_snapshot()
    payload.update({"success": True, "platform": "tiktok"})
    return payload


@app.post("/api/tiktok/config")
def api_tiktok_config_save(req: TikTokConfigRequest):
    """保存代理并**立即实测**，让用户当场知道填对了没有。

    「端口开着」不代表能出网（比如代理客户端启动了但订阅没连上），
    所以这里必须真发一次请求探 TikTok，而不是只做格式校验。
    """
    proxy = tiktok_parser._normalize_proxy(req.proxy or "")

    if proxy and not proxy.lower().startswith(("http://", "https://", "socks")):
        return _error("代理地址要以 http:// 或 socks5:// 开头，例如 http://127.0.0.1:7890", "tiktok")

    if proxy.lower().startswith("socks") and not tiktok_parser._socks_supported():
        return _error(
            "你填的是 SOCKS 代理，但当前 Python 环境没有 PySocks，无法使用。"
            "建议改用 HTTP 代理端口（Clash 的 7890 是混合端口，直接写 http://127.0.0.1:7890 即可）；"
            "或执行 pip install requests[socks] 后重启服务。",
            "tiktok",
        )

    tiktok_parser.write_config({"proxy": proxy})
    tiktok_parser.reset_session()

    # 落盘后立刻验证
    reachable, detail = (False, "未配置代理")
    if proxy:
        reachable, detail = tiktok_parser._proxy_reaches_tiktok(proxy)

    payload = _tiktok_config_snapshot(probe=not proxy)
    payload.update(
        {
            "success": True,
            "platform": "tiktok",
            "saved": True,
            "verified": "ok" if reachable else "unreachable",
        }
    )
    if not proxy:
        payload["warning"] = "已清空代理配置，改为自动探测常见本地端口（7890 / 7897 / 10809 …）"
    elif reachable:
        payload["warning"] = f"代理已保存且实测可用（{proxy}）"
    else:
        payload["warning"] = (
            f"代理已保存，但实测连不上 TikTok（{detail}）。请确认代理客户端已开启并已连上节点。"
        )
    return payload


@app.delete("/api/tiktok/config")
def api_tiktok_config_clear():
    """清除代理配置，回到自动探测。"""
    tiktok_parser.write_config({"proxy": ""})
    tiktok_parser.reset_session()
    payload = _tiktok_config_snapshot()
    payload.update({"success": True, "platform": "tiktok", "cleared": True})
    payload["warning"] = (
        "已清除代理配置，改为自动探测常见本地端口"
        + (
            f"；当前探测到可用代理 {payload['proxy'].get('value')}"
            if payload["proxy"].get("value")
            else "；当前没探测到可用代理，TikTok 将无法解析"
        )
    )
    return payload


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    return (name[:80].strip(" .")) or "douyin"


def _content_disposition(name: str, fallback: str = "download") -> str:
    """HTTP 头只能承载 latin-1，故同时给出 ASCII 回退名与 RFC 5987 编码名。

    浏览器优先采用 filename*，从而保留中文文件名。
    """
    filename = _safe_filename(name)
    root, ext = os.path.splitext(filename)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", root).strip("_") or fallback
    ascii_name = (ascii_name + ext)[:60]
    return (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )


# 前端从这里拿「后端真实端口」：端口可能因占用而顺延（见 pick_port），
# 把 8787 写死在 JS 里会在顺延后指向错误地址，所以改为渲染时注入。
SERVICE_PORT_PLACEHOLDER = "__SERVICE_PORT__"


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    """返回单页前端：注入实际端口，并显式禁止缓存。

    前端是「单文件 + 内联脚本」，若被浏览器缓存，改完代码后旧 JS 仍在内存里跑，
    表现为「明明改了却不生效」；这里强制每次回源。
    """
    path = os.path.join(STATIC_DIR, "index.html")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            html = handle.read()
    except OSError:
        raise HTTPException(
            status_code=500,
            detail=f"前端文件缺失：{path}（打包时请确认 static/index.html 已被打入）",
        )
    html = html.replace(SERVICE_PORT_PLACEHOLDER, str(ACTUAL_PORT))
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main():
    global ACTUAL_PORT
    # 别人电脑的控制台可能是非中文代码页；中文横幅一旦编码失败会直接抛
    # UnicodeEncodeError 把进程带崩。这里只放宽错误处理，不改编码。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

    # 端口被占用就顺延；实际端口会在 index_page 渲染时注入前端，所以不会打错接口
    ACTUAL_PORT = pick_port(PORT)
    display_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    url = f"http://{display_host}:{ACTUAL_PORT}"

    ffmpeg = find_ffmpeg()
    print("=" * 60)
    print("  多平台无水印下载站（抖音 / B站 / TikTok / 小红书）")
    print(f"  访问地址：{url}")
    if ACTUAL_PORT != PORT:
        print(f"  注意：默认端口 {PORT} 被占用，已自动改用 {ACTUAL_PORT}")
    print("  B站 高清合流（ffmpeg）：" + (ffmpeg if ffmpeg else "未找到，将只能分轨下载"))
    print(f"  配置保存目录：{runtime_paths.data_dir()}")
    if not runtime_paths.is_frozen():
        print("  运行方式：源码（尚未打包）")
    print("  关闭本窗口即可停止服务")
    print("=" * 60)

    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    # 默认只绑本机：打包分发时不会弹防火墙、也不会把服务暴露给同网段。
    # 云平台走 `uvicorn server:app --host 0.0.0.0`（见 Dockerfile 的 CMD），不经过这里。
    uvicorn.run(app, host=HOST, port=ACTUAL_PORT, log_level="warning")


if __name__ == "__main__":
    # 打包成 exe 后的标配防御：一旦有代码触发 multiprocessing，
    # 缺少这一行会导致子进程无限重启自身。当前代码未用多进程，加上无副作用。
    import multiprocessing

    multiprocessing.freeze_support()
    main()
