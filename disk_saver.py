# -*- coding: utf-8 -*-
"""把「下载」从系统下载器搬回本进程自己完成。

为什么不用 android.app.DownloadManager
--------------------------------------
安卓端最初的实现是：

    WebView 点击链接 → DownloadListener.onDownloadStart → 系统 DownloadManager

实测这条链有至少三个**各自独立**的断点，而它们的表现完全一样 ——
「点了下载，什么也没发生」，因为三处都是静默失败：

1. **回调不一定来**。onDownloadStart 只在 WebView 自己判定「这个响应我渲染不了」
   时才触发。监听器对象被 Python GC 回收、WebViewClient 提前接管导航、
   程序化点击（`a.click()`）走 blob: 分支……任何一环都会让回调根本不发生，
   而界面此时没有任何依据可以提示用户。

2. **DownloadManager 取不到 127.0.0.1 的明文 HTTP**。Android 9 起明文默认禁止，
   而 DownloadManager 跑在 `com.android.providers.downloads` 进程里，
   不一定跟随本应用的 usesCleartextTraffic。logcat 里的原话是：
       DownloadManager: Stop requested with status HTTP_DATA_ERROR:
       Cleartext HTTP traffic to 127.0.0.1 not permitted
   本工具的媒体流必须经由本机服务端（要带平台 Referer/UA/签名），
   所以这条路天然踩在这个限制上。

3. **失败是静默的**。任务在 DownloadManager 内部失败不会回到我们的代码；
   而 `setNotificationVisibility` 若用「只在完成时通知」这一档，失败连通知都没有。
   异常又被 try/except 吞进日志文件 —— 用户看不到，我们也拿不到。

改成「自己取流、自己落盘、自己报进度」之后，每一点都是可观测的：
字节流由本进程的 requests 读取（与 /api/download 同一套请求头与代理逻辑），
落盘走平台各自的正当接口，进度经 /api/save/status 回给前端。
任何一步失败都会带着异常原文出现在界面上 —— 这是这个模块存在的首要理由。

平台差异
--------
- 桌面：直接写用户目录下的 `Downloads`（本模块对桌面同样可用，便于单测）。
- 安卓：先写**应用自己的外部目录**（/sdcard/Android/data/<包名>/files，无需任何权限，
  这一步几乎不可能失败），完成后发布到公共「下载」目录：
    API 29+ → MediaStore（分区存储下写公共目录的唯一正路，同样不需要权限）
    API <29 → 直接写公共 Downloads 目录（需要 WRITE_EXTERNAL_STORAGE，
              拿不到时会降级：保留文件并把真实路径告诉用户，而不是丢一句「失败」）

分两步而不是直接往 MediaStore 里流，是为了「失败可降级」：发布这一步万一被 ROM
拦下，字节还在，我们能告诉用户文件在哪；直接流则只会在中途断掉、什么都不剩。
代价是大文件会有一份临时副本（见 commit 里的说明）。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from typing import Any, Callable, Dict, Iterable, List, Optional

import runtime_paths

# 任务记录在内存里保留多久（秒）。文件此时已经落地，回收的只是「进度记录」。
TASK_TTL = 30 * 60
# 内存里最多保留多少条任务记录
MAX_TASKS = 60
# 同时下载几个。前端「逐张下载」会连点上几十次，不限并发会把流量和磁盘打满。
CONCURRENCY = 2
# 读写块大小
CHUNK = 256 * 1024
# 安卓：公共「下载」目录下再建一个子目录，避免和用户自己的文件混在一起
ANDROID_SUBDIR = "multipldl"


def _log(message: str) -> None:
    """打日志。

    stdout 会被 p4a 转发到 logcat；安卓上 main.py 还会把启动日志落盘，
    所以这里 print 就够了，不额外引入日志框架。
    """
    try:
        print("[save] %s" % message)
        import sys

        sys.stdout.flush()
    except Exception:
        pass


class SaveError(Exception):
    """取流阶段的可读错误。消息会原样显示给用户，所以要用中文说人话。"""


class Canceled(Exception):
    """用户在界面上点了取消。"""


# ----------------------------------------------------------------- 文件名/类型
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_filename(name: str, fallback: str = "download") -> str:
    """清掉文件系统不接受的字符，并限制长度。

    注意**保留中文** —— 抖音/B站 的作品标题就是中文，抹掉等于让用户
    拿到一堆 download_1.mp4。
    """
    cleaned = _ILLEGAL.sub("_", (name or "").strip()).strip(" .")
    return cleaned[:100] or fallback


_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".zip": "application/zip",
    ".json": "application/json",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".ass": "text/plain",
}


def guess_mime(name: str) -> str:
    """按扩展名猜 MIME。MediaStore 需要它来决定文件归入哪类媒体。"""
    ext = os.path.splitext(name or "")[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _unique_path(path: str) -> str:
    """同名时追加 ` (2)`、` (3)` … 而不是覆盖用户已有的文件。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for i in range(2, 1000):
        candidate = "%s (%d)%s" % (root, i, ext)
        if not os.path.exists(candidate):
            return candidate
    return "%s (%s)%s" % (root, secrets.token_hex(3), ext)


def _writable(directory: str) -> bool:
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".wm_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------- 取流结果
class Stream:
    """一次取流的结果。

    total = 0 表示长度未知（分块返回、或边算边给，例如打包 zip）——
    此时前端显示不确定进度，而不是假的百分比。
    """

    def __init__(
        self,
        total: int = 0,
        chunks: Optional[Iterable[bytes]] = None,
        close: Optional[Callable[[], None]] = None,
    ) -> None:
        self.total = int(total or 0)
        self.chunks = chunks if chunks is not None else iter(())
        self.close = close or (lambda: None)


# ----------------------------------------------------------------- 落盘目标
class _Sink:
    """落盘目标：write() 收字节，commit() 收尾并回答「文件在哪」。"""

    def __init__(self, filename: str, mime: str) -> None:
        self.filename = filename
        self.mime = mime
        self.fp = None  # type: ignore[assignment]
        self.location = ""
        #: 非空表示「存下来了，但不在预期位置」，前端会把它显示成提示
        self.warning = ""

    def write(self, chunk: bytes) -> None:
        self.fp.write(chunk)

    def commit(self) -> str:
        raise NotImplementedError

    def abort(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        try:
            if self.fp is not None:
                self.fp.close()
        except Exception:
            pass
        self.fp = None


class _DesktopSink(_Sink):
    """桌面：直接写用户目录下的 Downloads。"""

    def __init__(self, filename: str, mime: str) -> None:
        super().__init__(filename, mime)
        self.directory = _desktop_download_dir()
        self.path = _unique_path(os.path.join(self.directory, filename))
        self.fp = open(self.path, "wb")

    def commit(self) -> str:
        self._close()
        self.location = self.path
        return self.location

    def abort(self) -> None:
        self._close()
        try:
            os.remove(self.path)
        except OSError:
            pass


def _desktop_download_dir() -> str:
    """桌面的「下载」目录。中文系统上可能叫「下载」，两个都试。"""
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, "Downloads"),
        os.path.join(home, "下载"),
    ):
        if os.path.isdir(candidate) and _writable(candidate):
            return candidate
    fallback = os.path.join(runtime_paths.data_dir(), "downloads")
    os.makedirs(fallback, exist_ok=True)
    return fallback


class _AndroidSink(_Sink):
    """安卓：临时文件 → 公共「下载」目录。

    临时文件用 ASCII 名（`<token>.part`），中文名只在发布时使用 ——
    这样即便某台 ROM 的 Python 层对中文路径有问题，也不会影响下载本身。
    """

    def __init__(self, filename: str, mime: str) -> None:
        super().__init__(filename, mime)
        self.directory = _android_tmp_dir()
        os.makedirs(self.directory, exist_ok=True)
        self.tmp = os.path.join(self.directory, "%s.part" % secrets.token_hex(8))
        self.fp = open(self.tmp, "wb")
        self.size = 0
        #: 是否已经把临时文件保留下来当作最终产物（发布失败时的降级）
        self.kept = ""

    def write(self, chunk: bytes) -> None:
        self.fp.write(chunk)
        self.size += len(chunk)

    def commit(self) -> str:
        self._close()
        try:
            self.location = _android_publish(self.tmp, self.filename, self.mime)
            return self.location
        except Exception as exc:  # noqa: BLE001 - 发布失败不能让已下载的字节白费
            keep = _unique_path(os.path.join(self.directory, self.filename))
            try:
                shutil.move(self.tmp, keep)
            except OSError:
                keep = self.tmp
            self.kept = keep
            self.location = keep
            self.warning = (
                "文件已下载，但没能放进公共「下载」目录（%s）。"
                "它保存在应用目录里：%s" % (exc, keep)
            )
            _log("发布到公共目录失败，已保留临时文件：%s" % exc)
            return self.location

    def abort(self) -> None:
        self._close()
        try:
            os.remove(self.tmp)
        except OSError:
            pass


def _make_sink(filename: str, mime: str) -> _Sink:
    if runtime_paths.is_android():
        return _AndroidSink(filename, mime)
    return _DesktopSink(filename, mime)


# ----------------------------------------------------------------- 安卓专有
def _jnius():
    from jnius import autoclass  # type: ignore

    return autoclass


def _android_activity():
    autoclass = _jnius()
    return autoclass("org.kivy.android.PythonActivity").mActivity


def _android_tmp_dir() -> str:
    """应用自己的外部目录 —— 无需任何权限，且通常比内部存储宽裕得多。"""
    try:
        autoclass = _jnius()
        Environment = autoclass("android.os.Environment")
        base = _android_activity().getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
        if base is not None:
            return os.path.join(base.getAbsolutePath(), "tmp")
    except Exception as exc:  # noqa: BLE001
        _log("取应用外部目录失败，退回内部存储：%s" % exc)
    return os.path.join(runtime_paths.data_dir(), "tmp")


def _android_sdk() -> int:
    try:
        return int(_jnius()("android.os.Build$VERSION").SDK_INT)
    except Exception:
        # 判断不出来时按「新系统」处理：MediaStore 是 API 29+ 的正路，
        # 真在旧系统上会抛异常，再由 commit 的降级分支兜住。
        return 29


def _android_publish(src: str, filename: str, mime: str) -> str:
    """把临时文件发布到公共「下载」目录，返回展示用位置。"""
    if _android_sdk() >= 29:
        return _publish_via_mediastore(src, filename, mime)
    return _publish_via_public_dir(src, filename)


def _publish_via_mediastore(src: str, filename: str, mime: str) -> str:
    """API 29+：写进 MediaStore 的 Downloads 集合。

    列名用字符串字面量而不是 `MediaStore.MediaColumns.*` 常量：
    那些常量定义在 Java 接口里，pyjnius 读接口静态字段并不可靠，
    而列名本身是公开且长期稳定的契约。
    """
    autoclass = _jnius()
    ContentValues = autoclass("android.content.ContentValues")
    Downloads = autoclass("android.provider.MediaStore$Downloads")
    Integer = autoclass("java.lang.Integer")

    activity = _android_activity()
    resolver = activity.getContentResolver()

    values = ContentValues()
    values.put("_display_name", filename)
    values.put("mime_type", mime)
    values.put("relative_path", "Download/" + ANDROID_SUBDIR)
    # is_pending=1：写完成之前对其他应用不可见，避免扫描到半个文件
    values.put("is_pending", Integer(1))

    uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
    if uri is None:
        raise RuntimeError("系统媒体库拒绝创建文件（insert 返回空）")

    stream = None
    try:
        stream = resolver.openOutputStream(uri)
        if stream is None:
            raise RuntimeError("系统媒体库拒绝打开写入流（openOutputStream 返回空）")
        with open(src, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                stream.write(chunk)
        stream.flush()
    except Exception:
        try:
            if stream is not None:
                stream.close()
        finally:
            # 失败必须把半截条目删掉，否则「下载」里会留下一个打不开的空文件
            try:
                resolver.delete(uri, None, None)
            except Exception:
                pass
        raise
    finally:
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass

    done = ContentValues()
    done.put("is_pending", Integer(0))
    resolver.update(uri, done, None, None)

    try:
        os.remove(src)
    except OSError:
        pass
    _log("已发布到媒体库：下载/%s/%s" % (ANDROID_SUBDIR, filename))
    return "下载/%s/%s" % (ANDROID_SUBDIR, filename)


def _publish_via_public_dir(src: str, filename: str) -> str:
    """API <29：公共 Downloads 目录可以直接写文件（需 WRITE_EXTERNAL_STORAGE）。

    拿不到权限时会抛异常，由 commit 的降级分支保留文件并告知真实路径。
    """
    autoclass = _jnius()
    Environment = autoclass("android.os.Environment")
    base = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
    target_dir = os.path.join(base.getAbsolutePath(), ANDROID_SUBDIR)
    os.makedirs(target_dir, exist_ok=True)
    target = _unique_path(os.path.join(target_dir, filename))
    shutil.move(src, target)
    _log("已移动到公共下载目录：%s" % target)
    return "下载/%s/%s" % (ANDROID_SUBDIR, os.path.basename(target))


# ----------------------------------------------------------------- 任务
class SaveTask:
    """一次保存任务的状态。

    后台线程写、HTTP 轮询读，所以所有字段都走同一把锁 —— 否则前端会看到
    「state 还是 running 但 percent 已经 100」这类半截状态。
    """

    def __init__(self, token: str, filename: str, mime: str, kind: str) -> None:
        self.token = token
        self.filename = filename
        self.mime = mime
        self.kind = kind
        self.state = "queued"  # queued / running / done / error / canceled
        self.detail = "排队中…"
        self.received = 0
        self.total = 0
        self.step_done = 0
        self.step_total = 0
        self.error = ""
        self.warning = ""
        self.location = ""
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    # ---- 后台线程写
    def running(self, detail: str = "正在取流…") -> None:
        with self._lock:
            self.state = "running"
            self.started = self.started or time.time()
            self.detail = detail

    def set_total(self, total: int) -> None:
        with self._lock:
            self.total = int(total or 0)

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.received += int(count)
            if self.total:
                self.detail = "已下载 %s / %s" % (_human(self.received), _human(self.total))
            else:
                # 长度未知（分块流）：只报已收到的量，不编一个假的百分比
                self.detail = "已下载 %s" % _human(self.received)

    def set_step(self, done: int, total: int, detail: str = "") -> None:
        """按「条目数」报进度（打包 zip 用）。"""
        with self._lock:
            self.step_done = int(done)
            self.step_total = int(total)
            self.detail = detail or "正在打包 %d/%d" % (done, total)

    def saving(self, detail: str) -> None:
        with self._lock:
            self.detail = detail

    def finish(self, location: str, warning: str = "") -> None:
        with self._lock:
            self.state = "done"
            self.location = location
            self.warning = warning
            self.detail = "已保存到 %s" % location
            self.finished = time.time()

    def fail(self, message: str) -> None:
        with self._lock:
            self.state = "error"
            self.error = message
            self.detail = message
            self.finished = time.time()

    def canceled(self) -> None:
        with self._lock:
            self.state = "canceled"
            self.detail = "已取消"
            self.finished = time.time()

    # ---- HTTP 读
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            percent = -1
            if self.total > 0:
                percent = min(100, int(self.received * 100 / self.total))
            elif self.step_total > 0:
                percent = min(100, int(self.step_done * 100 / self.step_total))
            if self.state in ("done",) and percent < 0:
                percent = 100
            elapsed = (self.finished or time.time()) - (self.started or self.created)
            return {
                "id": self.token,
                "name": self.filename,
                "kind": self.kind,
                "state": self.state,
                "detail": self.detail,
                "received": self.received,
                "total": self.total,
                "percent": percent,
                "step_done": self.step_done,
                "step_total": self.step_total,
                "speed": int(self.received / elapsed) if elapsed > 0.5 and self.received else 0,
                "error": self.error,
                "warning": self.warning,
                "location": self.location,
                "seconds": int(elapsed),
            }


def _human(count: int) -> str:
    count = int(count or 0)
    if count >= 1073741824:
        return "%.2f GB" % (count / 1073741824.0)
    if count >= 1048576:
        return "%.1f MB" % (count / 1048576.0)
    if count >= 1024:
        return "%.1f KB" % (count / 1024.0)
    return "%d B" % count


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, SaveError):
        return str(exc)
    text = str(exc).strip() or exc.__class__.__name__
    return "%s（%s）" % (text, exc.__class__.__name__)


# ----------------------------------------------------------------- 管理器
class SaveManager:
    """按 token 管理下载任务。

    取流逻辑由调用方通过 `register(kind, handler)` 注入 —— 这个模块因此
    不认识 requests、草稿白名单、平台请求头，也不知道 zip 该怎么打；
    它只负责「排队、写盘、报进度、收尾」。这样它可以在桌面上直接单测。
    """

    #: handler(payload, task, sink) -> Stream
    HANDLER = Callable[[Dict[str, Any], SaveTask, _Sink], Stream]

    def __init__(self, concurrency: int = CONCURRENCY) -> None:
        self._handlers: Dict[str, "SaveManager.HANDLER"] = {}
        self._tasks: Dict[str, SaveTask] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max(1, int(concurrency)))
        cleanup_stale_temps()

    def register(self, kind: str, handler: "SaveManager.HANDLER") -> None:
        self._handlers[kind] = handler

    def start(self, kind: str, payload: Dict[str, Any], name: str, mime: str = "") -> SaveTask:
        handler = self._handlers.get(kind)
        if handler is None:
            raise SaveError("不支持的保存类型：%s" % kind)
        filename = safe_filename(name, "download")
        task = SaveTask(secrets.token_urlsafe(12), filename, mime or guess_mime(filename), kind)
        self._gc()
        with self._lock:
            self._tasks[task.token] = task
            self._trim()
        threading.Thread(
            target=self._run, args=(task, handler, payload), name="save-%s" % task.token, daemon=True
        ).start()
        _log("任务已建立 %s → %s" % (task.token, filename))
        return task

    def get(self, token: str) -> Optional[SaveTask]:
        with self._lock:
            return self._tasks.get(token or "")

    def listing(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._tasks.values())
        items.sort(key=lambda t: t.created, reverse=True)
        return [t.snapshot() for t in items[:limit]]

    # ---- 内部
    def _trim(self) -> None:
        """记录数超限时，先丢最老的已结束任务。"""
        if len(self._tasks) <= MAX_TASKS:
            return
        finished = [t for t in self._tasks.values() if t.state not in ("queued", "running")]
        finished.sort(key=lambda t: t.created)
        for task in finished[: max(0, len(self._tasks) - MAX_TASKS)]:
            self._tasks.pop(task.token, None)

    def _gc(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                token
                for token, task in self._tasks.items()
                if task.finished and now - task.finished > TASK_TTL
            ]
            for token in stale:
                self._tasks.pop(token, None)

    def _run(self, task: SaveTask, handler: "SaveManager.HANDLER", payload: Dict[str, Any]) -> None:
        # 排队期间被取消：直接结束，不必占并发位
        if task.cancel.is_set():
            task.canceled()
            return
        if not self._slots.acquire(timeout=1800):
            task.fail("排队超时：前面的下载长时间没有结束")
            return

        sink: Optional[_Sink] = None
        try:
            if task.cancel.is_set():
                raise Canceled()
            task.running("正在取流…")
            sink = _make_sink(task.filename, task.mime)
            stream = handler(payload, task, sink)
            task.set_total(stream.total)
            for chunk in stream.chunks:
                if task.cancel.is_set():
                    raise Canceled()
                if chunk:
                    sink.write(chunk)
                    task.add_bytes(len(chunk))
            stream.close()
            task.saving(
                "正在保存到「下载」目录…" if runtime_paths.is_android() else "正在收尾…"
            )
            location = sink.commit()
            task.finish(location, sink.warning)
            _log("任务完成 %s → %s" % (task.token, location))
        except Canceled:
            if sink is not None:
                sink.abort()
            task.canceled()
            _log("任务已取消 %s" % task.token)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要变成界面上的文字
            if sink is not None:
                sink.abort()
            task.fail(_friendly(exc))
            _log("任务失败 %s：%s" % (task.token, _friendly(exc)))
        finally:
            self._slots.release()

    def cancel(self, token: str) -> bool:
        task = self.get(token)
        if task is None or task.state in ("done", "error", "canceled"):
            return False
        task.cancel.set()
        return True


def write_zip_into(
    sink: "_Sink",
    entries: List[Any],
    fetch: Callable[[str], Optional[tuple]],
    task: SaveTask,
    base: str,
    ext_for: Callable[[str, Optional[str], str], str],
    sanitize: Callable[[str], str],
) -> int:
    """把条目边抓边写进 zip（直接写在 sink 的文件对象上，不经过内存中转）。

    与 server.py 里 `_build_zip_spool` 的区别：这个版本**边抓边写**，
    所以能把「已打包 12/30」这样的真实进度报给用户；代价是失败时
    已经写进去的条目会作废（对「保存到手机」这种一次性动作是可以接受的，
    反正要么全有要么重来，用户不会看到一个半截 zip 被留在「下载」目录里 ——
    因为 sink.abort() 会把临时文件删掉）。

    返回成功写入的条目数。
    """
    total = len(entries)
    saved = 0
    with zipfile.ZipFile(sink.fp, "w", zipfile.ZIP_STORED) as archive:
        for index, item in enumerate(entries):
            if task.cancel.is_set():
                raise Canceled()
            url, hint = item[0], item[1]
            task.set_step(index, total, "正在打包 %d/%d…" % (index + 1, total))
            fetched = fetch(url)
            if fetched:
                content, content_type = fetched
                name = sanitize(hint) if hint else "%s_%03d%s" % (
                    base,
                    index + 1,
                    ext_for(url, content_type),
                )
                archive.writestr(name, content)
                saved += 1
            task.set_step(index + 1, total, "已打包 %d/%d" % (index + 1, total))
        if not saved:
            raise SaveError("所有资源均下载失败，请稍后重试")
    return saved


def cleanup_stale_temps() -> None:
    """清掉上次运行遗留的临时文件。

    `.part` 只可能在下载中被创建，而下载随进程结束一起消失，
    所以启动时看到的一律是残缺文件，直接删。
    """
    try:
        if not runtime_paths.is_android():
            return
        directory = _android_tmp_dir()
        if not os.path.isdir(directory):
            return
        removed = 0
        for name in os.listdir(directory):
            if name.endswith(".part"):
                try:
                    os.remove(os.path.join(directory, name))
                    removed += 1
                except OSError:
                    pass
        if removed:
            _log("清理了 %d 个上次遗留的临时文件" % removed)
    except Exception:
        pass
