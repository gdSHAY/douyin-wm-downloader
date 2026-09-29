# 多平台无水印下载站

粘贴一条分享链接，拿到可直接下载的直链 / 成片。支持 **抖音、B站、TikTok、小红书**。

后端 FastAPI + 单文件前端（无构建步骤），两种跑法：

- **云端**：Docker 镜像部署到 Render，得到一个可点开的网址（本文件讲的就是它）
- **本地**：`python server.py` 或打包成 exe / 安卓 APK

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/gdSHAY/douyin-wm-downloader)

> 仓库是**私有**的：点这个按钮后 Render 会先让你用 GitHub 登录、授权它读本仓库，
> 然后它读 `render.yaml` 自动建服务。不想用按钮就走下面第二节的手动流程，效果一样。

### 这个仓库已经自动验证过什么

每次推送到 `main`，CI（`.github/workflows/docker-check.yml`）会真跑一遍并给出结论：

| 验证项 | 说明 |
| --- | --- |
| Docker 镜像能构建 | `docker build` 成功，依赖全部装得上 |
| **ffmpeg 在镜像里可用** | `ffmpeg -version` 有输出 ← **B站 出成片的前提** |
| 服务能在容器里启动 | uvicorn 正常监听 `$PORT`，首页返回 200 |
| 端口注入正确 | 首页里 `__SERVICE_PORT__` 已被替换成实际端口 |
| B站 合流能力接口正常 | `/api/bili/mux/capability` 正常响应（容器里如实报「不可用」，桌面/网页用 ffmpeg） |
| **TikTok 在境外容器里可达** | 直连三个关键地址，实测 **3/3 可达**（明细见下面） |

也就是说：**「镜像能不能构建、B站 能不能合流、TikTok 通不通」这三件最容易翻车的事，在你去点部署之前就已经验过了。**

**TikTok 那一条的原始日志（CI 实测，容器内未配任何代理）：**

```
https://www.tiktok.com/                              -> HTTP 200  370981 bytes
https://www.tiktok.com/player/api/v1/items?aid=1988  -> HTTP 400  109 bytes
https://vm.tiktok.com/ZM6nQqQqQ/                     -> HTTP 200  376278 bytes

小结：3/3 个地址可达
```

第二行**回 400 而不是超时**，是本条最有价值的信息：`/player/api/v1/items` 正是解析器
实际调用的接口，400 说明 **TCP + TLS 通了、而且 TikTok 的应用层真的响应了**
（只是参数被拒）。如果出口被挡，这里会是连接超时或 5xx。

> ⚠️ 仍需你实测一次：GitHub runner 在 Azure、Render 在 AWS，**机房不同**。
> 上面的结论是「境外出口能直连」的强证据，但不是 Render 上的保证 —— 见第二节第 5 步。

---

## 一、为什么云端要用 Docker

**因为 B站 高清是「音视频分离」的（DASH）。** 平台把画面和声音给成两条流，
要得到一条能直接播放的**成片**，必须用 ffmpeg 合流 —— 这是 `/api/bili/download` 做的事。

Render 的原生 Python 运行时不提供 root，装不了系统包；Docker 里 `apt-get install ffmpeg`
一行就解决了。Dockerfile 顶部把这段原因也写了一遍，免得以后有人把它改回 `runtime: python`。

服务代码本身**不需要改**：`find_ffmpeg()` 的查找顺序里包含 `shutil.which("ffmpeg")`
和 `/usr/bin/ffmpeg`，apt 装的 ffmpeg 正好落在那里。

---

## 二、部署到 Render（5 分钟）

### 1. 注册

打开 <https://render.com> → 点 **Get Started** → 选 **GitHub** 登录（不需要信用卡）。

### 2. 授权仓库

第一次进控制台会让你 **Connect GitHub**，授权时可以只勾这一个仓库（更安全）。
若仓库是私有的，Render 也能通过这个授权读到。

### 3. 新建 Blueprint

- 控制台右上角 **New +** → **Blueprint**
- 选本仓库 → **Connect**
- Render 会读到仓库根目录的 `render.yaml`，自动填好服务名、区域、Docker 构建方式
- 点 **Apply**（或 **Create Resources**）

### 4. 等构建

首次构建要装 ffmpeg + Python 依赖，大约 **3–6 分钟**。在服务的 **Logs** 页能看到：

```
[build] ffmpeg 已就绪
...
==> Running 'uvicorn server:app --host 0.0.0.0 --port 10000'
INFO:     Uvicorn running on http://0.0.0.0:10000
```

看到这两行就成功了。页面上方会给出网址：`https://<服务名>.onrender.com`。

### 5. 验证（别只看首页）

首页能打开不代表能用 —— 按这条顺序试，才能证明「解析 → 下载」整条链路通：

| 步骤 | 操作 | 期望 |
| --- | --- | --- |
| 1 | 打开网址 | 页面正常显示，无脚本报错 |
| 2 | 粘一条**抖音**分享链接 → 解析 | 出标题、封面、档位列表 |
| 3 | 点下载 | 拿到 mp4，能播放 |
| 4 | 切到 **B站**，粘一条视频链接 → 解析 | 出标题与清晰度列表（未登录通常 480P/360P） |
| 5 | 点「下载成片」 | 得到**已合流的 mp4**（有声音）—— 这条能过，说明 ffmpeg 生效了 |
| 6 | 切到 **TikTok** 粘链接 → 解析 | 境外机房通常可直连；若失败见下面「排查」 |
| 7 | 切到 **小红书** 粘分享链接 | 出图文列表 |

---

## 三、免费实例的三个限制（先知道，少走弯路）

| 限制 | 具体表现 | 怎么办 |
| --- | --- | --- |
| **15 分钟无流量会休眠** | 下次请求要等约 1 分钟冷启动（页面会显示 Render 的加载页） | 自己用时点一下等一会；不想等就升到 $7/月（不休眠） |
| **带宽 5 GB / 月** | B站 一条 1080P 成片可能几百 MB，下一个就吃掉几 % | 够个人用；分享给很多人用会超。超了当月服务会被暂停 |
| **0.1 CPU / 512 MB** | 长视频 / 4K 合流会比较慢（合流是 IO 密集，一般不会 OOM） | 短视频无感；4K 长片耐心等或升配 |

另外：**免费实例没有持久磁盘** —— 重启后容器里写的东西全没。
所以网页「设置」里填的 B站 cookie / TikTok 代理**留不住**，要长期生效得走环境变量（见下）。

---

## 四、环境变量（都在 Render 控制台 → Environment 配）

| 变量 | 作用 | 要不要配 |
| --- | --- | --- |
| `BILI_SESSDATA` | B站 登录态。配上才能下 1080P/4K | 可选 |
| `TIKTOK_PROXY` | TikTok 代理。`http://host:port` | 境外机房一般不用配 |

### 关于 `BILI_SESSDATA`

不配也能用 —— **480P/360P，但依然是合流好的成片**（音视频完整、能直接播放）。

⚠️ 配之前请想清楚：**服务网址是公开可访问的**。填了你的 SESSDATA，
等于任何拿到链接的人都能用你的账号下载高清。要开高清，建议先把访问控制做好。

### 关于 `TIKTOK_PROXY`

TikTok 官方接口在**中国大陆的网络**里无法直连（DNS 能解析、TCP 443 超时）。
Render 的机房在境外，**直连就应该能用，这一项留空即可**。

> **证据**：CI 在境外容器里（未配任何代理）直连实测 **3/3 地址可达**，
> 其中解析器真正调用的 `/player/api/v1/items` 返回 400（应用层已响应，非网络不通）。
> 明细见本文开头的日志块。
>
> ⚠️ 保留一条限定：GitHub runner 在 Azure、Render 在 AWS，**机房不同**，
> 结论是强证据而非保证。请按第二节第 5 步实测一次 —— 若你在 Render 的 Logs 里
> 看到类似 `直连（没有探测到可用的 TikTok 代理）` 的说明，就说明它走了预期路径。

如果 TikTok 解析仍失败，说明该机房出口被挡了：

1. 换个区域试试（改 `render.yaml` 的 `region`，或控制台 Settings → Region）
2. 或填一个**公网可达**的代理地址 —— 注意不能写 `127.0.0.1`，
   那在服务器上指的是它自己，必然连不通

---

## 五、改代码后怎么重新上线

`render.yaml` 里 `autoDeploy: true` 已经开了自动部署：

```bash
# 改完代码，推送到 main 分支
git push origin main
```

Render 检测到推送就会自动重建、重新部署，**网址不变**。

如果你在本地维护的是另一个目录结构，注意**服务代码是「扁平」放在仓库根的**
（`server.py` 与 `static/` 同级），不是放在子目录里 —— 这跟 `uvicorn server:app` 的
导入方式有关。

---

## 六、排查

| 现象 | 多半是 | 怎么确认 |
| --- | --- | --- |
| 部署失败，日志里 `apt-get` 报错 | 镜像源临时抽风 | 在 Render 点 **Manual Deploy → Clear build cache & deploy** |
| 部署成功但访问 502 | 端口没起来 | 看日志有没有 `Uvicorn running on http://0.0.0.0:`；`PORT` 必须是 `$PORT` 展开的（见 Dockerfile 注释） |
| 首页能开，但点解析没反应 | 前端把请求打到 `127.0.0.1` 了 | 打开浏览器 F12 → Network，看请求域名。正常应是当前网址（相对路径） |
| B站 解析成功、下载报「未找到 ffmpeg」 | ffmpeg 没进最终镜像 | 看构建日志有没有 `[build] ffmpeg 已就绪`；有的话进服务 Shell 跑 `ffmpeg -version` |
| B站 下载 400「不支持代理该域名」 | 直链主机不在白名单 | 换一条视频试；仍失败请提 issue，附上报错里的完整域名 |
| TikTok 报 SSL / 连接失败 | 机房出口被挡 | 见上面「关于 TIKTOK_PROXY」 |
| 下载到一半断了 | 免费实例的请求时长或带宽限制 | 换小一点的档位；免费实例不适合下超大文件 |

---

## 七、本地跑（不部署也能用）

```bash
pip install -r requirements.txt
python server.py            # 默认 http://127.0.0.1:8787，会自动开浏览器
python server.py --no-browser
```

本地跑 B站 成片同样需要 ffmpeg：Windows 装完把 `ffmpeg.exe` 放进 PATH，
或设环境变量 `FFMPEG_PATH` 指向它。

本地跑 TikTok 需要代理（大陆网络无法直连），在页面右上角 **⚙ 设置 → TikTok 代理** 里填，
或开着 Clash / FlClash（默认 7890）让程序自动探测。

---

## 许可

仅供个人学习与备份自己有权保存的内容使用。请遵守各平台的服务条款与著作权法，
不要用于传播他人作品。
