# -*- coding: utf-8 -*-
"""运行期路径工具：同时兼容「源码运行」与「PyInstaller 打包运行」。

为什么需要这个模块
------------------
打包成 exe 后有两类路径会变，直接写 `os.path.dirname(__file__)` 会踩坑：

1. **只读资源**（`static/`、内置的 `ffmpeg.exe`）
   PyInstaller 会把它们解到 `sys._MEIPASS`（onedir 下就是 exe 隔壁的 `_internal/`）。
   此时 `__file__` 指向的是那个临时/内部目录，不是 exe 所在目录。

2. **可写配置**（`bili_config.json`、`tiktok_config.json`）
   **绝不能写进 `_MEIPASS`** —— onefile 模式下那是每次启动都重建的临时目录，
   写进去等于「用户填完 B站 cookie，重启就没了」。

因此统一从这里取路径：
    resource_dir()  只读资源在哪   -> _MEIPASS / 源码目录
    app_dir()       程序本体在哪   -> exe 所在目录 / 源码目录
    data_dir()      可写数据放哪   -> exe 同目录的 data/，不可写则退 %APPDATA%
"""

from __future__ import annotations

import os
import sys

# 用于 %APPDATA% 兜底目录名，同时出现在启动横幅里便于排查
APP_NAME = "多平台无水印下载站"

# 允许用户显式指定数据目录（便携部署到 U 盘、或想固定配置位置时用）
ENV_DATA_DIR = "WM_DL_DATA_DIR"


def is_frozen() -> bool:
    """是否由 PyInstaller 打包后运行。"""
    return bool(getattr(sys, "frozen", False))


def is_android() -> bool:
    """是否运行在 python-for-android 打出的 APK 里。

    不能靠 `sys.platform` 判断 —— p4a 里它仍然是 `linux`。
    p4a 启动时会注入 ANDROID_ARGUMENT / ANDROID_PRIVATE 等环境变量，
    这两个是可靠的「我在安卓上」信号。
    """
    return "ANDROID_ARGUMENT" in os.environ or "ANDROID_PRIVATE" in os.environ


def resource_dir() -> str:
    """只读资源目录。

    打包后：`sys._MEIPASS`（onedir 模式下即 exe 相邻的 `_internal`）；
    源码运行：本文件所在目录（即项目根）。
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if not base:
            base = os.path.dirname(os.path.abspath(sys.executable))
        return base
    return os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    """程序本体所在目录：打包后是 exe 所在目录，源码运行是项目根。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _probe_writable(path: str) -> bool:
    """真的写一个文件试试 —— 只判断权限位在 Windows 上并不可靠。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


_DATA_DIR_CACHE: str = ""


def data_dir() -> str:
    """可写数据目录，结果在进程内缓存。

    优先级：
      1. 环境变量 `WM_DL_DATA_DIR`
      2. exe（或源码）同目录下的 `data/` —— 便携，拷走就能带走配置
      3. `%APPDATA%\\多平台无水印下载站` —— 装在 Program Files 等只读位置时的兜底
    """
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE:
        return _DATA_DIR_CACHE

    env = (os.environ.get(ENV_DATA_DIR) or "").strip().strip('"')
    if env:
        os.makedirs(env, exist_ok=True)
        _DATA_DIR_CACHE = env
        return _DATA_DIR_CACHE

    if is_android():
        # APK 里选 `ANDROID_PRIVATE`（即 /data/data/<包名>/files）；
        # 这里一定可写，且随应用数据一起被系统管理，卸载才会清掉。
        base = (os.environ.get("ANDROID_PRIVATE") or app_dir()).strip()
        target = os.path.join(base, "wm_data")
        try:
            os.makedirs(target, exist_ok=True)
            _DATA_DIR_CACHE = target
            return _DATA_DIR_CACHE
        except OSError:
            pass

    portable = os.path.join(app_dir(), "data")
    if _probe_writable(portable):
        _DATA_DIR_CACHE = portable
        return _DATA_DIR_CACHE

    roaming = os.environ.get("APPDATA") or os.path.expanduser("~")
    fallback = os.path.join(roaming, APP_NAME)
    os.makedirs(fallback, exist_ok=True)
    _DATA_DIR_CACHE = fallback
    return _DATA_DIR_CACHE


def config_path(filename: str) -> str:
    """配置文件完整路径。

    源码运行**沿用旧位置**（项目根），这样已有的 `bili_config.json` /
    `tiktok_config.json` 不会因为本次改造而「搬家」——用户不用重填。
    打包后（exe 或 APK）才切到 `data_dir()`。
    """
    if is_frozen() or is_android():
        return os.path.join(data_dir(), filename)
    return os.path.join(app_dir(), filename)
