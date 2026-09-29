# 多平台无水印下载站 —— 云端容器镜像（Render 部署用）
#
# 为什么必须是 Docker，而不是 Render 的原生 Python 环境
# ----------------------------------------------------
# B站 高清是 DASH「音视频分离」（画面和声音是两条流），要出**合成好的成片**
# 必须调用 ffmpeg 合流 —— 见 server.py 的 /api/bili/download。
# Render 的原生运行时不提供 root，装不了系统包；Docker 里 apt-get 一行就带上了 ffmpeg。
#
# 不用额外配 FFMPEG_PATH
# ---------------------
# server.py 的 find_ffmpeg() 查找顺序是：
#   环境变量 FFMPEG_PATH → 打包内置的那份 → shutil.which("ffmpeg") → /usr/bin/ffmpeg
# apt 装的 ffmpeg 落在 /usr/bin/ffmpeg，`shutil.which` 与候选列表都能命中，无需粘环境变量。
#
# 镜像大小的取舍
# --------------
# 用 slim 而不是完整版：这里所有依赖（fastapi/uvicorn/requests/gmssl/yt-dlp/curl_cffi）
# 在 PyPI 上都有 cp312 的 manylinux wheel，不需要 gcc 现场编译，所以不装 build-essential。

FROM python:3.12-slim

# ffmpeg          —— B站 DASH 合流（-c copy）与音频转 MP3（libmp3lame）都靠它
# ca-certificates —— pip 走 https 拉包要根证书（slim 镜像里默认不全）
# 装完立刻清 apt 列表，别把索引留在镜像层里
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 构建期就确认 ffmpeg 真的可用：失败停在构建阶段，比部署后才发现「合流报 502」好查得多
RUN ffmpeg -version > /dev/null 2>&1 && echo "[build] ffmpeg 已就绪"

WORKDIR /app

# 先只拷依赖清单再装包：之后改业务代码时这一层能命中缓存，重建快很多
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# 再拷源码。.dockerignore 已排除构建产物、安卓工程与本地凭据
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=10000

EXPOSE 10000

# ★ 必须用 shell 形式（不是 JSON exec 数组形式）—— 只有这样 $PORT 才会在容器里被展开。
#   Render 会注入实际端口；${PORT:-10000} 让本地 `docker run` 不带 -e PORT 时也有默认值。
#   （踩过的坑：写成 ["uvicorn", ..., "--port", "$PORT"] 时 $PORT 不会展开，
#     uvicorn 收到空参数直接退出，表现为「部署成功但访问 502」。）
# ★ --timeout-keep-alive 放宽到 120s：B站 成片合流后的回传可能持续好几分钟。
# ★ 不要加 --workers：tiktok_parser 的备胎直链表（_ALTS）是**进程内**的，多 worker 会
#   各存一份，表现为「解析得到的备胎在下载时找不到，换链时好时坏」。
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-10000} --timeout-keep-alive 120
