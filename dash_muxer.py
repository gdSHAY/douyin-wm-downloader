# -*- coding: utf-8 -*-
"""安卓本机 DASH 合流：用系统 MediaExtractor + MediaMuxer 替代 ffmpeg。

背景
----
B站 的高清恒为 DASH —— 视频轨与音频轨是两个独立文件，要交付「一个能直接播放的
mp4」必须合流。桌面版靠 ffmpeg `-c copy` 无损做完，但 APK 里没有 ffmpeg 命令行
程序（p4a 的 ffmpeg recipe 只产出库文件，不出可执行程序），过去只能降级成
「视频轨 / 音频轨」两个按钮让用户自己去合。

其实 Android 系统自带一对 API，合起来正好等价于 `ffmpeg -c copy`：

    MediaExtractor   解复用 m4s，逐样例吐出编码后的数据（不解码）
    MediaMuxer       把样例按 MP4 重新封装（不编码）

所以画质零损失、速度只受 I/O 限制，也不需要任何外部程序。

设计要点（每条都是踩过坑才写下的）
----------------------------------
1. **优先直连 CDN，失败再退回本地下载**。`MediaExtractor.setDataSource(url, headers)`
   能带自定义请求头，足以过 B站 CDN 的 Referer 校验，省掉一次落盘；但不同 ROM 的
   HTTP 数据源实现有差异，所以保留「先用 requests 下到本地、再合本地文件」这条
   备用路。只有**打开输入源**这一步失败才降级 —— 编码不支持之类的问题降级也没用，
   不能白白多下一个 GB。

2. **样例级交错写入**。两条轨各自按时间递增，必须交替写（每次各写一个样例），
   否则 PTS 在输出里会交错乱序，部分播放器会判定文件损坏。

3. **样例缓冲要会自适应**。单样例大小由 `KEY_MAX_INPUT_SIZE` 给出，但 fMP4 的
   init 段里不一定带这个键；给少了会在底层 `setLimit` 抛 IllegalArgumentException。
   做法：取该键与默认值的较大者，真的抛了再翻倍重试（读失败不会推进样例下标，
   重试不会丢帧）。

4. **编解码兼容性不靠猜**。MediaMuxer 只认一部分编码，B站 的 4K 可能是 AV1，
   在旧系统上封不了。这里不查资料也不硬编码 API 等级，而是拿一个极小的
   MediaFormat 真的去 `addTrack` 试一次 —— 平台自己说了算，结果进程内缓存。

5. **音轨固定用 AAC**。`bili_parser.extract_tracks` 会优先给无损/杜比轨，但那类
   编码塞进 MP4 兼容性差；B站 的 AAC 本身有 192K，实际听感差异可以忽略。
   选轨逻辑放在 server 侧（`_bili_mux_audio`），这里只负责封装。

本机（Windows）没有安卓设备，真机验证只能靠云端构建后手测；因此所有纯逻辑
（选轨、编解码筛选、进度、缓冲扩容、失败清理）都通过注入替身做了离线单测，
见 `安卓合流单测.py`。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import runtime_paths

__all__ = [
    "MuxError",
    "SourceOpenError",
    "CodecUnsupportedError",
    "MuxCanceled",
    "is_available",
    "capability",
    "video_codec_supported",
    "video_mime",
    "audio_mime",
    "select_muxable_quality",
    "bili_headers",
    "mux",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 单次合流的墙钟上限（秒）。4K 长视频在手机上也可能跑十几分钟，给足余量。
MUX_TIMEOUT = 3600.0

DOWNLOAD_CHUNK = 262144
#: 单次读取超时。stream 模式下只要持续有数据就不会触发，用于兜住「连上了但不发数据」。
DOWNLOAD_TIMEOUT = 120.0

#: KEY_MAX_INPUT_SIZE 缺失时的默认样例缓冲。4MB 是为了兜住 4K 关键帧
#: （1080P 关键帧通常 100~300KB，4K 可能到 1~2MB）。
SAMPLE_DEFAULT_BUFFER = 4 * 1024 * 1024
#: 病态样例的上限，防止把内存吃光。
SAMPLE_MAX_BUFFER = 64 * 1024 * 1024

#: 进度回调的最小间隔，避免把 WebView 的 JS 线程刷爆。
PROGRESS_MIN_INTERVAL = 0.35

#: B站 codecs 字段 → MediaMuxer 认识的 MIME。
#: 取值来自 playurl 的 `dash.video[].codecs` / `dash.audio[].codecs`，
#: 形如 `avc1.640032`、`hev1.1.6.L150.90`、`av01.0.08M.08`、`mp4a.40.2`、`fLaC`、`ec-3`。
VIDEO_MIME_BY_CODEC = {
    "avc1": "video/avc",
    "avc3": "video/avc",
    "avc": "video/avc",
    "h264": "video/avc",
    "hvc1": "video/hevc",
    "hev1": "video/hevc",
    "hevc": "video/hevc",
    "h265": "video/hevc",
    "av01": "video/av1",
    "av1": "video/av1",
}

AUDIO_MIME_BY_CODEC = {
    "mp4a": "audio/mp4a-latm",
    "aac": "audio/mp4a-latm",
    "ec-3": "audio/eac3",
    "ac-3": "audio/ac3",
    "flac": "audio/flac",
    "opus": "audio/opus",
}

#: capability() 里会逐个探测的编码（覆盖 B站 实际会下发的种类）
BILI_VIDEO_CODECS = ("avc1", "hvc1", "hev1", "av01")
BILI_AUDIO_CODECS = ("mp4a", "ec-3", "ac-3", "fLaC")

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class MuxError(Exception):
    """合流失败。message 直接面向用户，会原样出现在界面提示里。"""


class SourceOpenError(MuxError):
    """打不开输入源（网络/请求头/文件不存在）。

    单独一个类型是为了区分「该不该退回本地下载」：只有这一类失败降级才有意义，
    编码不支持之类的失败降级也救不回来，只会白白多下一个 GB。
    """


class CodecUnsupportedError(MuxError):
    """MediaMuxer 拒绝该编码（典型：旧系统 + AV1 4K）。"""


class MuxCanceled(MuxError):
    """用户主动取消。"""


# ---------------------------------------------------------------------------
# Java 门面
# ---------------------------------------------------------------------------


class _Java:
    """把所有 jnius 调用收在一处。

    单独抽出来的现实原因：开发机上没有安卓设备，只能靠替身做离线单测。
    测试里用 `_install_java()` 换掉整个门面，就能覆盖除「调平台 API」以外的
    全部分支逻辑。
    """

    def __init__(self) -> None:
        from jnius import autoclass

        self.autoclass = autoclass
        self.MediaExtractor = autoclass("android.media.MediaExtractor")
        self.MediaMuxer = autoclass("android.media.MediaMuxer")
        self.MediaFormat = autoclass("android.media.MediaFormat")
        self.OutputFormat = autoclass("android.media.MediaMuxer$OutputFormat")
        self.BufferInfo = autoclass("android.media.MediaCodec$BufferInfo")
        self.ByteBuffer = autoclass("java.nio.ByteBuffer")
        self.HashMap = autoclass("java.util.HashMap")
        # Build.VERSION 是内部类，必须用 $ 取，不能在 Build 上取属性
        self.Version = autoclass("android.os.Build$VERSION")

    @property
    def sdk_int(self) -> int:
        try:
            return int(self.Version.SDK_INT)
        except Exception:
            return 0


_JAVA_LOCK = threading.Lock()
_JAVA_STATE: Dict[str, Any] = {"loaded": False, "java": None, "error": ""}


def _install_java(java: Any, error: str = "") -> None:
    """测试用：注入 Java 门面替身，并清掉探测缓存。

    正常运行时由 `_java()` 自己加载，不会走到这里。
    """
    with _JAVA_LOCK:
        _JAVA_STATE["loaded"] = True
        _JAVA_STATE["java"] = java
        _JAVA_STATE["error"] = error
    with _MIME_LOCK:
        _MIME_CACHE.clear()


def _java() -> _Java:
    """加载并缓存 Java 门面；不可用时抛 MuxError（带可读原因）。"""
    with _JAVA_LOCK:
        if not _JAVA_STATE["loaded"]:
            _JAVA_STATE["loaded"] = True
            if not runtime_paths.is_android():
                _JAVA_STATE["error"] = (
                    "仅安卓 APK 内可用（桌面版请用 ffmpeg 合流）"
                )
            else:
                try:
                    _JAVA_STATE["java"] = _Java()
                except Exception as exc:  # jnius 缺失 / 类加载失败
                    _JAVA_STATE["error"] = "无法加载 android.media 相关类：%s: %s" % (
                        type(exc).__name__,
                        exc,
                    )
        if _JAVA_STATE["java"] is None:
            raise MuxError(_JAVA_STATE["error"] or "本机合流不可用")
        return _JAVA_STATE["java"]


def is_available() -> bool:
    """本机合流是否可用。结果进程内缓存，可放心在解析路径上调用。"""
    try:
        _java()
        return True
    except MuxError:
        return False


def _log(message: str) -> None:
    try:
        print("[dash_muxer] %s" % message)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 编解码兼容性：真的问平台，不猜
# ---------------------------------------------------------------------------

_MIME_LOCK = threading.Lock()
_MIME_CACHE: Dict[str, bool] = {}


def video_mime(codec: str) -> Optional[str]:
    return VIDEO_MIME_BY_CODEC.get((codec or "").strip().lower())


def audio_mime(codec: str) -> Optional[str]:
    return AUDIO_MIME_BY_CODEC.get((codec or "").strip().lower())


def _probe_mime(java: Any, mime: str) -> bool:
    """拿一个极小的 MediaFormat 真的 addTrack 一次，问平台认不认这种编码。

    比硬编码「AV1 要 API 29 以上」之类的资料可靠：判断权交给设备自己，
    而且能顺带覆盖各家 ROM 的差异。代价是一次毫秒级的建文件 + 删除。
    """
    probe_dir = tempfile.mkdtemp(prefix="muxprobe_")
    path = os.path.join(probe_dir, "probe.mp4")
    muxer = None
    try:
        if mime.startswith("video/"):
            fmt = java.MediaFormat.createVideoFormat(mime, 320, 240)
        else:
            fmt = java.MediaFormat.createAudioFormat(mime, 44100, 1)
        muxer = java.MediaMuxer(path, java.OutputFormat.MUXER_OUTPUT_MPEG_4)
        muxer.addTrack(fmt)
        return True
    except Exception:
        return False
    finally:
        if muxer is not None:
            try:
                muxer.release()
            except Exception:
                pass
        shutil.rmtree(probe_dir, ignore_errors=True)


def _supports_mime(java: Any, mime: Optional[str]) -> bool:
    if not mime:
        return False
    with _MIME_LOCK:
        if mime in _MIME_CACHE:
            return _MIME_CACHE[mime]
    ok = _probe_mime(java, mime)
    with _MIME_LOCK:
        _MIME_CACHE[mime] = ok
    _log("MediaMuxer 封装 %s ：%s" % (mime, "支持" if ok else "不支持"))
    return ok


def video_codec_supported(codec: str) -> bool:
    """某个 B站 codecs 值（avc1/hev1/av01…）能否被本机封装。"""
    try:
        java = _java()
    except MuxError:
        return False
    return _supports_mime(java, video_mime(codec))


def capability() -> Dict[str, Any]:
    """平台能力，结果可直接 JSON 化给前端。"""
    try:
        java = _java()
    except MuxError as exc:
        return {
            "available": False,
            "backend": None,
            "reason": str(exc),
            "sdk_int": 0,
            "video_codecs": {},
            "audio_codecs": {},
        }

    video: Dict[str, bool] = {}
    for codec in BILI_VIDEO_CODECS:
        video[codec] = _supports_mime(java, video_mime(codec))
    audio: Dict[str, bool] = {}
    for codec in BILI_AUDIO_CODECS:
        audio[codec] = _supports_mime(java, audio_mime(codec))

    return {
        "available": True,
        "backend": "MediaExtractor+MediaMuxer",
        "reason": "",
        "sdk_int": java.sdk_int,
        "video_codecs": video,
        "audio_codecs": audio,
    }


def select_muxable_quality(
    qualities: List[Dict[str, Any]],
    supported: Callable[[str], bool],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """从高到低挑第一个「本机能封装」的档位，并说明是否发生了降档。

    B站 的 4K 常是 AV1，老设备上 MediaMuxer 封不了。直接报错对用户太粗暴，
    自动降到仍可封装的最清晰档并把原因讲清楚，比丢一句「失败」有用得多。

    `supported` 接收的是 **B站 的 codecs 值**（`avc1` / `hev1` / `av01`），
    与档位列表里的 `codec` 字段同一语义 —— 直接传
    `dash_muxer.video_codec_supported` 即可，不需要自己转换。

    返回 (选中的档位对象 或 None, 提示文案)。
    """
    if not qualities:
        return None, "没有可用的清晰度"

    rejected: List[str] = []
    for item in qualities:
        codec = str(item.get("codec") or "")
        if codec and supported(codec):
            note = ""
            if rejected:
                top = qualities[0]
                note = "最高档 %s（%s）手机端无法封装，已自动改用 %s（%s）" % (
                    top.get("label"),
                    top.get("codec") or "未知编码",
                    item.get("label"),
                    codec,
                )
            return item, note
        rejected.append("%s(%s)" % (item.get("label"), codec or "未知编码"))

    return None, "该视频的所有清晰度在手机端都无法封装：" + "、".join(rejected)


# ---------------------------------------------------------------------------
# 请求头 / 小工具
# ---------------------------------------------------------------------------


def bili_headers() -> Dict[str, str]:
    """B站 CDN 校验 Referer，不带会 403。

    与 `server.BILI_HEADERS` 必须保持一致 —— 单测里有一条专门盯着这个，
    避免两处改动不同步。
    """
    import bili_parser

    return {
        "User-Agent": bili_parser.UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
    }


def _is_url(source: str) -> bool:
    return str(source or "").lower().startswith(("http://", "https://"))


def _mb(size: float) -> str:
    if size >= 1024 * 1024 * 1024:
        return "%.2f GB" % (size / 1073741824)
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / 1048576)
    return "%.0f KB" % (size / 1024)


def _hms(microseconds: float) -> str:
    total = max(0, int(microseconds // 1000000))
    return "%d:%02d" % (total // 60, total % 60)


class _Progress:
    """把「已处理到哪」翻译成 (阶段, 百分比, 文案)，并按时间节流回调。"""

    def __init__(
        self,
        duration: float,
        callback: Optional[Callable[[str, int, str], None]],
    ) -> None:
        self.callback = callback
        self.total_us = int(duration * 1000000) if duration and duration > 0 else 0
        self._last_at = 0.0
        self._last_phase = ""

    def emit(self, phase: str, percent: float, detail: str, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.time()
        switched = phase != self._last_phase
        if not force and not switched and now - self._last_at < PROGRESS_MIN_INTERVAL:
            return
        self._last_at = now
        self._last_phase = phase
        value = max(0, min(100, int(percent)))
        try:
            self.callback(phase, value, detail)
        except Exception:
            # 进度回调是「锦上添花」，任何异常都不能影响合流本身
            pass

    def mux(self, pts_us: int, samples: int, force: bool = False) -> None:
        if self.total_us > 0:
            percent = pts_us * 100.0 / self.total_us
            if force:
                percent = 100.0
            else:
                percent = min(percent, 99.0)  # 100% 留给真正写完的那一刻
            detail = "已处理 %s / %s" % (_hms(pts_us), _hms(self.total_us))
        else:
            percent = 100.0 if force else 0.0
            detail = "已写入 %d 个样例" % samples
        self.emit("mux", percent, detail, force=force)


# ---------------------------------------------------------------------------
# 轨读取
# ---------------------------------------------------------------------------


def _find_track(java: Any, extractor: Any, prefix: str) -> Tuple[int, Any]:
    """按 mime 前缀找第一条轨道。返回 (下标, MediaFormat)，找不到给 (-1, None)。"""
    count = int(extractor.getTrackCount())
    for index in range(count):
        fmt = extractor.getTrackFormat(index)
        mime = fmt.getString(java.MediaFormat.KEY_MIME) or ""
        if mime.startswith(prefix):
            return index, fmt
    return -1, None


def _sample_buffer_size(java: Any, fmt: Any) -> int:
    """样例缓冲大小。KEY_MAX_INPUT_SIZE 是提示值，取默认值的较大者以防低估。"""
    try:
        key = java.MediaFormat.KEY_MAX_INPUT_SIZE
        if fmt.containsKey(key):
            value = int(fmt.getInteger(key))
            if value > 0:
                return min(max(value, SAMPLE_DEFAULT_BUFFER), SAMPLE_MAX_BUFFER)
    except Exception:
        pass
    return SAMPLE_DEFAULT_BUFFER


class _Reader:
    """一条输入轨的读取游标。"""

    __slots__ = (
        "java",
        "extractor",
        "index",
        "out_index",
        "label",
        "mime",
        "size",
        "buffer",
        "done",
        "samples",
        "last_pts",
    )

    def __init__(
        self,
        java: Any,
        extractor: Any,
        track_index: int,
        fmt: Any,
        label: str,
        out_index: int,
    ) -> None:
        self.java = java
        self.extractor = extractor
        self.index = track_index
        self.out_index = out_index
        self.label = label
        self.mime = fmt.getString(java.MediaFormat.KEY_MIME) or ""
        self.size = _sample_buffer_size(java, fmt)
        self.buffer = java.ByteBuffer.allocateDirect(self.size)
        self.done = False
        self.samples = 0
        self.last_pts = 0

    def write_next(self, muxer: Any, info: Any) -> bool:
        """写一个样例。返回 False 表示这条轨已经读完。"""
        if self.done:
            return False

        size = self._read()
        if size < 0:
            self.done = True
            return False

        # 显式摆正 position/limit：读和写两端对游标状态的约定不完全一致，
        # 明确设一遍最稳（多余时是幂等的）。
        self.buffer.position(0)
        self.buffer.limit(size)

        info.offset = 0
        info.size = size
        info.presentationTimeUs = int(self.extractor.getSampleTime())
        info.flags = self.extractor.getSampleFlags()

        muxer.writeSampleData(self.out_index, self.buffer, info)
        self.extractor.advance()

        self.samples += 1
        self.last_pts = info.presentationTimeUs
        return True

    def _read(self) -> int:
        """读一个样例；缓冲装不下就翻倍重试。

        底层在装不下时由 `ByteBuffer.setLimit` 抛 IllegalArgumentException，
        而样例下标的推进发生在 `advance()` —— 读失败不丢帧，重试是安全的。
        只对 IllegalArgumentException 扩容：其他异常（IO 错误等）扩容也没用，
        直接报出来更省事。
        """
        while True:
            try:
                return int(self.extractor.readSampleData(self.buffer, 0))
            except Exception as exc:
                if "IllegalArgument" not in type(exc).__name__:
                    raise MuxError(
                        "%s轨读取失败：%s: %s" % (self.label, type(exc).__name__, exc)
                    ) from exc
                bigger = self.size * 2
                if bigger > SAMPLE_MAX_BUFFER:
                    raise MuxError(
                        "%s轨单个样例超过 %d MB 上限（%s）"
                        % (self.label, SAMPLE_MAX_BUFFER >> 20, exc)
                    ) from exc
                _log("样例缓冲不足，扩到 %d MB 后重试" % (bigger >> 20))
                self.size = bigger
                self.buffer = self.java.ByteBuffer.allocateDirect(bigger)


# ---------------------------------------------------------------------------
# 下载（备用路径）
# ---------------------------------------------------------------------------


def _fetch(
    url: str,
    path: str,
    headers: Dict[str, str],
    label: str,
    reporter: _Progress,
    should_cancel: Optional[Callable[[], bool]],
) -> int:
    """把一条轨道下到本地。只在「直连 CDN 打不开」的兜底路径里用。"""
    import requests

    session = requests.Session()
    # 环境里的 HTTP(S)_PROXY 会劫持请求（沙箱里实测过），一律忽略环境代理
    session.trust_env = False

    with session.get(
        url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT
    ) as resp:
        if resp.status_code >= 400:
            raise MuxError("下载%s失败：HTTP %d" % (label, resp.status_code))
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(path, "wb") as handle:
            for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                if should_cancel is not None and should_cancel():
                    raise MuxCanceled("已取消")
                handle.write(chunk)
                done += len(chunk)
                percent = (done * 100.0 / total) if total else 0.0
                tail = (" / " + _mb(total)) if total else ""
                reporter.emit("download", percent, "正在下载%s %s%s" % (label, _mb(done), tail))

    if done <= 0:
        raise MuxError("下载%s失败：内容为空" % label)
    return done


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _attach(
    java: Any, extractor: Any, source: str, headers: Dict[str, str]
) -> None:
    """给 extractor 挂上输入源。URL 走带请求头的 HTTP 数据源，其余按本地文件。"""
    if _is_url(source):
        try:
            table = java.HashMap()
            for key, value in headers.items():
                table.put(key, value)
            extractor.setDataSource(source, table)
            return
        except Exception as exc:
            raise SourceOpenError("无法直连媒体地址：%s: %s" % (type(exc).__name__, exc)) from exc
    try:
        extractor.setDataSource(source)
    except Exception as exc:
        raise SourceOpenError("无法打开本地文件 %s：%s" % (source, exc)) from exc


def _codec_error(v_format: Any, a_format: Any, exc: Exception) -> str:
    """把 addTrack 的异常翻译成用户能行动的话。"""
    java = _JAVA_STATE.get("java")
    names = []
    for fmt, label in ((v_format, "视频"), (a_format, "音频")):
        if fmt is None:
            continue
        try:
            names.append("%s=%s" % (label, fmt.getString(java.MediaFormat.KEY_MIME)))
        except Exception:
            names.append(label)
    return (
        "手机自带的封装器不支持该编码（%s）。"
        "换一个清晰度档位再试（通常 1080P 及以下是 AVC，兼容性最好）。原始错误：%s"
        % ("，".join(names), exc)
    )


def _mux_streams(
    java: Any,
    video_source: str,
    audio_source: Optional[str],
    out_path: str,
    headers: Dict[str, str],
    duration: float,
    on_progress: Optional[Callable[[str, int, str], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> Dict[str, Any]:
    """核心：打开两条轨 → 交错写样例 → 收尾。输入可以是 URL 也可以是本地路径。"""
    extractors: List[Any] = []
    muxer = None
    started = False
    ok = False

    try:
        video = java.MediaExtractor()
        extractors.append(video)
        _attach(java, video, video_source, headers)
        v_index, v_format = _find_track(java, video, "video/")
        if v_index < 0:
            raise MuxError("输入里没有找到视频流（mime 前缀 video/）")
        video.selectTrack(v_index)

        audio = None
        a_index, a_format = -1, None
        if audio_source:
            audio = java.MediaExtractor()
            extractors.append(audio)
            _attach(java, audio, audio_source, headers)
            a_index, a_format = _find_track(java, audio, "audio/")
            if a_index < 0:
                # 输入本来就没音轨：退化成「只封装画面」，而不是整个失败
                audio = None
                a_format = None
            else:
                audio.selectTrack(a_index)

        muxer = java.MediaMuxer(out_path, java.OutputFormat.MUXER_OUTPUT_MPEG_4)
        try:
            out_v = int(muxer.addTrack(v_format))
            out_a = int(muxer.addTrack(a_format)) if a_format is not None else -1
        except Exception as exc:
            raise CodecUnsupportedError(_codec_error(v_format, a_format, exc)) from exc

        muxer.start()
        started = True

        video_reader = _Reader(java, video, v_index, v_format, "视频", out_v)
        audio_reader = (
            _Reader(java, audio, a_index, a_format, "音频", out_a)
            if (audio is not None and a_index >= 0)
            else None
        )
        readers = [r for r in (video_reader, audio_reader) if r is not None]

        reporter = _Progress(duration, on_progress)
        info = java.BufferInfo()
        deadline = time.time() + MUX_TIMEOUT

        while True:
            if should_cancel is not None and should_cancel():
                raise MuxCanceled("已取消")
            if time.time() > deadline:
                raise MuxError("合流超时（超过 %d 秒）" % int(MUX_TIMEOUT))

            alive = 0
            for reader in readers:
                if reader.write_next(muxer, info):
                    alive += 1

            merged = min(r.last_pts for r in readers if r.samples) if any(
                r.samples for r in readers
            ) else 0
            reporter.mux(merged, sum(r.samples for r in readers))

            if alive == 0:
                break

        reporter.mux(merged, sum(r.samples for r in readers), force=True)

        if not any(r.samples for r in readers):
            raise MuxError("没有读到任何样例，输入轨可能损坏或不是有效的 DASH 分片")
        if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            raise MuxError("封装结果为空，可能输入轨损坏")

        ok = True
        return {
            "samples": sum(r.samples for r in readers),
            "bytes": int(os.path.getsize(out_path)),
            "duration_us": int(merged),
            "video_mime": video_reader.mime,
            "audio_mime": audio_reader.mime if audio_reader is not None else None,
        }
    finally:
        if muxer is not None:
            if started:
                try:
                    muxer.stop()
                except Exception:
                    pass
            try:
                muxer.release()
            except Exception:
                pass
        for extractor in extractors:
            try:
                extractor.release()
            except Exception:
                pass
        if not ok:
            # 半成品文件留着只会占空间，而且可能被误当成有效结果
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
            except OSError:
                pass


def _mux_local(
    java: Any,
    video_url: str,
    audio_url: Optional[str],
    out_path: str,
    headers: Dict[str, str],
    duration: float,
    on_progress: Optional[Callable[[str, int, str], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> Dict[str, Any]:
    """兜底路径：URL 先用 requests 下到本地，再合本地文件。

    比直连多一份临时空间，但数据获取走的是项目里已被验证过的 requests 路径，
    在某些 ROM 的 HTTP 数据源上更稳。传入本地路径时不重复下载，直接用。
    """
    base = tempfile.mkdtemp(prefix="dashmux_")
    try:
        reporter = _Progress(duration, on_progress)

        video_file = video_url
        if _is_url(video_url):
            video_file = os.path.join(base, "video.m4s")
            _fetch(video_url, video_file, headers, "视频轨", reporter, should_cancel)

        audio_file = None
        if audio_url:
            if _is_url(audio_url):
                audio_file = os.path.join(base, "audio.m4s")
                _fetch(audio_url, audio_file, headers, "音频轨", reporter, should_cancel)
            else:
                audio_file = audio_url

        reporter.emit("mux", 0, "正在封装 mp4", force=True)
        return _mux_streams(
            java,
            video_file,
            audio_file,
            out_path,
            headers,
            duration,
            on_progress,
            should_cancel,
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def mux(
    video_url: str,
    audio_url: Optional[str],
    out_path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    duration: float = 0.0,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    source_mode: str = "auto",
) -> Dict[str, Any]:
    """把视频轨与音频轨无损封装成一个 mp4。

    参数
    ----
    video_url / audio_url : DASH 轨地址（也接受本地路径，便于单测与排错）
    out_path              : 输出 mp4 路径；所在目录必须已存在
    duration              : 视频总时长（秒），仅用于算进度百分比，可缺省
    on_progress           : `(phase, percent, detail)`，phase ∈ download/mux
    should_cancel         : 返回 True 时尽快中断并抛 MuxCanceled
    source_mode           : auto（直连失败自动退本地）/ url / local

    返回 {samples, bytes, duration_us, video_mime, audio_mime}。
    """
    java = _java()
    headers = dict(headers or bili_headers())

    if source_mode == "local":
        return _mux_local(
            java, video_url, audio_url, out_path, headers, duration, on_progress, should_cancel
        )

    if source_mode in ("auto", "url"):
        try:
            return _mux_streams(
                java, video_url, audio_url, out_path, headers, duration, on_progress, should_cancel
            )
        except SourceOpenError as exc:
            if source_mode == "url":
                raise
            _log("直连 CDN 打不开输入流（%s），改用「先下载到本地再合流」" % exc)
            if on_progress is not None:
                try:
                    on_progress("download", 0, "直连受限，改为先下载到本机")
                except Exception:
                    pass

    return _mux_local(
        java, video_url, audio_url, out_path, headers, duration, on_progress, should_cancel
    )
