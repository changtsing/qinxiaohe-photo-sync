# 亲小禾照片同步（Mac 微信）

从 Mac 版微信的亲小禾小程序缓存中，自动提取并保存班级相册、考勤抓拍等照片，支持**一次性全量 + 增量同步**。

## 推荐流程（全自动）

```bash
# 1. 自动滚动加载全部动态（需辅助功能权限）
python3 scripts/qinxiaohe-photo-sync/browse.py

# 2. 从 API 缓存提取 URL 并下载原图
python3 scripts/qinxiaohe-photo-sync/sync.py
```

`browse.py` 会控制鼠标滚轮自动向下滑动，让微信把相册 API 分页数据写进缓存。  
`sync.py` 会从这些 API 响应里提取图片 URL，并**直接下载高清原图**（无需逐张点开）。

## 使用前准备

1. 使用 **Mac 版微信**
2. 打开「亲小禾」→「成长空间 / 班级动态」列表页
3. 运行 `browse.py` 前，把鼠标移到动态列表区域

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
```

### 同步下载

```bash
python3 scripts/qinxiaohe-photo-sync/sync.py
```

默认保存到 `~/Pictures/亲小禾/`。再次运行只增量保存新图。

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
| 去重 | 按图片内容 SHA256，状态文件 `.sync-state.json` |

## 说明与限制

- **browse.py** 负责加载 API 分页；加载越多，能同步的照片越多
- 亲小禾 API 需要微信登录态，脚本**不能**直接调 API，只能从微信已浏览产生的缓存里读
- 相册原图 URL 本身可公开下载，因此 sync 能拿到高清版（如 3072×4096），比缓存缩略图更清晰
