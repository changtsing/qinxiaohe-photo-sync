# 亲小禾照片同步（Mac 微信）

从 Mac 版微信的亲小禾小程序缓存中，自动提取并保存班级相册、考勤抓拍等照片，支持**一次性全量 + 增量同步**。

## 推荐流程（全自动）

```bash
# 1. 自动滚动加载全部动态（需辅助功能权限）
python3 scripts/qinxiaohe-photo-sync/browse.py

# 2. 从 API 缓存提取 URL 并下载原图
python3 scripts/qinxiaohe-photo-sync/sync.py
```

`browse.py` 会自动定位「亲小禾」微信窗口、点击列表区域，并用 Page Down + 滚轮 + 拖拽组合滚动，直到缓存里出现全部 API 分页。  
`sync.py` 会从这些 API 响应里提取图片 URL，并**直接下载高清原图**（无需逐张点开）。

## 使用前准备

1. 使用 **Mac 版微信**
2. 打开「亲小禾」→「成长空间 / 班级动态」列表页
3. 保持亲小禾窗口在屏幕上可见（不要被其他窗口完全挡住）

### 辅助功能权限

`browse.py` 需要 **系统设置 → 隐私与安全性 → 辅助功能** 中为 Terminal 或 Cursor 开启权限，否则无法控制滚动。

## 命令说明

### 自动浏览（加载 API 缓存）

```bash
python3 scripts/qinxiaohe-photo-sync/browse.py
```

常用参数：

```bash
# 查看当前已缓存多少页 API
python3 scripts/qinxiaohe-photo-sync/browse.py --status

# 加载到第 17 页后自动停止（166 条动态约 17 页）
python3 scripts/qinxiaohe-photo-sync/browse.py --until-pages 17

# 如果滚动没生效，加大滚动强度并延长等待
python3 scripts/qinxiaohe-photo-sync/browse.py --scrolls 200 --pixels 700 --pause 2 --stagnant-limit 40

# AI 视觉模式：截图判断是否滑到底（需 OpenAI API Key）
export OPENAI_API_KEY="sk-..."
python3 scripts/qinxiaohe-photo-sync/browse.py --ai --until-pages 17
```

### AI 视觉模式（推荐）

如果纯机械滚动不稳定，可开启 `--ai`：

1. 每轮滚动后截取「亲小禾」窗口
2. 调用视觉大模型判断是否到底、滚动是否生效
3. 若滚动无效，自动加大滚动幅度

需要 **OpenAI API Key**（支持视觉的模型，默认 `gpt-4o-mini`）：

```bash
export OPENAI_API_KEY="sk-..."
python3 browse.py --ai --until-pages 17
```

也可复制 `.env.example` 为 `.env` 后填入密钥。

> **关于 Cursor API Key**：Cursor API 用于 [Cursor Agent SDK](https://cursor.com/docs/sdk)（跑代码 Agent、改仓库、开 PR），**不适合**每几秒分析一次微信截图。本项目的实时「看屏幕」能力用的是 OpenAI 视觉 API。Cursor Key 更适合让 Agent 帮你维护/改进这套脚本本身。

### 同步下载

```bash
python3 scripts/qinxiaohe-photo-sync/sync.py
```

默认保存到 `~/Pictures/亲小禾/`（视频在 `~/Pictures/亲小禾/videos/`）。再次运行只增量保存新图。

```bash
# 边浏览边自动下载
python3 scripts/qinxiaohe-photo-sync/sync.py --watch

# 只从图片缓存提取（不下载 API 原图）
python3 scripts/qinxiaohe-photo-sync/sync.py --cache-only
```

## 原理

| 项目 | 说明 |
|------|------|
| 小程序 | 亲小禾 `wx54ef0cc36d1ddf68` |
| 相册 API | `applet.xiaohebook.com/growSpace/page`（分页列表） |
| 原图地址 | `album-img.xiaohebook.com/tmp_*.jpg`（公开可下载） |
| 相册原图 | `album-img.xiaohebook.com` |
| 动态视频 | `album-video.xiaohebook.com`（保存到 `videos/` 子目录） |
| 去重 | 照片按内容 SHA256；视频按 URL + 文件哈希记录 |

## 说明与限制

- **browse.py** 负责加载 API 分页；加载越多，能同步的照片越多
- 亲小禾 API 需要微信登录态，脚本**不能**直接调 API，只能从微信已浏览产生的缓存里读
- 相册原图 URL 本身可公开下载，因此 sync 能拿到高清版（如 3072×4096），比缓存缩略图更清晰
