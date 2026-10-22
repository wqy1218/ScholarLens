# ScholarLens

领域感知的论文阅读工作台，Windows 单机应用。导入论文 → 自动归属研究领域 → 精读（翻译 / 解释 / 三类高亮 / 批注 / 多轮问答）→ 领域知识沉淀与图谱。

论文、笔记、问答记录存在本机一个 SQLite 文件里；领域归属用本地 `BAAI/bge-small-en-v1.5` 计算。
联网只用于 arXiv 下载与你自己配置的大模型 API。

## 下载

- 安装版：Releases → 1.1.0 的 `ScholarLens-Setup-1.1.0.exe`，向导式安装，可选安装目录，按用户安装；
- 源码：`git clone` 本仓库，按下面「上手」执行。

---

## 架构

```
┌─ Electron 主进程 app/main.js ─────────────────────────────┐
│ 单实例锁 → 释放残留后端 → 选空闲端口(127.0.0.1:21800–21849) │
│ spawn python-runtime/python.exe -m uvicorn server.api:app  │
│ 轮询 /health 就绪 → BrowserWindow.loadURL(/ui/index.html)  │
│ 托盘常驻 + backend.lock 记录 PID，退出时 taskkill /T /F     │
└───────────────────────────┬───────────────────────────────┘
                            │ HTTP + SSE(/events)
┌───────────────────────────▼───────────────────────────────┐
│ FastAPI 后端 server/  （纯本地 127.0.0.1）                  │
│  pdfio(PyMuPDF) · ocr(扫描页) · assign(嵌入+质心) · highlight │
│  translate · summarize · qa(llm.py, OpenAI 兼容) · store    │
└───────────────────────────┬───────────────────────────────┘
                            │ SQLite (WAL) + FTS5
       <data>/scholarlens.db · <data>/papers/ · <data>/llm_cache.db
```

前端不是 SPA：`app/renderer/` 是手写 HTML/CSS/JS，由后端以静态资源挂载在 `/ui/*`，
PDF 渲染用随包 vendored 的 `pdf.js`（`app/renderer/vendor/`），不经过任何构建工具。

## 技术栈

| 层 | 选型 |
|---|---|
| 桌面壳 | Electron 33（主进程 `app/main.js`，preload 暴露最小 IPC） |
| 后端 | Python 3.12 + FastAPI + uvicorn |
| PDF | PyMuPDF（解析/取文）+ pdf.js 3.11（渲染，vendored）；无文字层页面由视觉模型转写 |
| 存储 | SQLite 单文件，WAL，FTS5 全文索引；PDF 副本存 `<data>/papers/` |
| 向量 | sentence-transformers `BAAI/bge-small-en-v1.5`（384 维，CPU，`HF_HUB_OFFLINE=1`） |
| LLM | OpenAI 兼容接口，多厂商档案（默认 DeepSeek），带响应缓存与统计 |
| 打包 | electron-builder 26 → NSIS 按用户安装包 |

打包版主进程以 `--no-sandbox` 启动 Chromium；界面只加载本机 `127.0.0.1` 的资源，
外链在独立窗口打开。

## 目录结构

```
app/            Electron 主进程 + preload + renderer（HTML/CSS/JS/vendor）
server/         FastAPI 后端（api.py 为路由总入口）
docs/           使用说明、原理与流程
assets/         应用图标（icon.ico / icon.png）与静态 logo（mark.svg）
tools/          出图辅助页（icon.html：图标几何的 SVG 母版）
scripts/        数据迁移 migrate_*.py、自检 smoke_*/test_*/check_integrity.py、审计 audit.py
data/           源码运行时的数据目录（gitignore）
python-runtime/ 随包可重定位 Python（gitignore）
models/hf/      嵌入模型缓存（gitignore）
```

## 上手

前置：Windows 10/11 x64、Node.js 18+、Python 3.12。

```bash
git clone <your-fork> && cd ScholarLens
npm install                      # electron + electron-builder + pdfjs-dist
pip install -r requirements.txt  # 后端依赖（fastapi/uvicorn/pymupdf/sentence-transformers…）
npm start                        # 或双击 start.cmd
```

以下两项不在仓库内，需自行准备：

1. `python-runtime/` — 主进程按 `python-runtime/python.exe` 找解释器，可用
   `pip install -r requirements.txt --target python-runtime/Lib/site-packages` 填充。
2. `models/hf/` — 需要 `BAAI/bge-small-en-v1.5`；默认 `HF_HUB_OFFLINE=1` 只读本地缓存，
   首次可临时去掉该变量联网拉取。

## 构建交付包

```cmd
md %USERPROFILE%\_slbuild
set TMP=%USERPROFILE%\_slbuild&& set TEMP=%USERPROFILE%\_slbuild
npx electron-builder --win nsis --publish never
# 产物：dist/ScholarLens-Setup-<version>.exe —— 向导式安装包，可选安装目录
```

`TMP` 须指向新建的空目录：`TMP` 指向 `%LOCALAPPDATA%\Temp` 时，NSIS 生成卸载器一步返回退出码 2，构建中断。

构建前清空 `python-runtime` 与 `server` 下的 `__pycache__`。

其他目标：

```bash
npx electron-builder --win portable --publish never   # dist/ScholarLens-<version>.exe，单文件免安装
npx electron-builder --win --dir --publish never      # dist/win-unpacked/，免安装目录形态
```

网络不通时加 `--config.electronDist=node_modules/electron/dist` 复用本机 Electron。

`electron-builder.yml` 用 `extraResources` 把 `server/`、`app/renderer/`、`python-runtime/`、
`assets/`、`models/hf/` 原样落到 `resources/`（不压缩，保持 Python 运行时布局）；
只有 Electron 主进程代码进 asar。NSIS 要求 `assets/icon.ico` 含 256px 帧。

图标几何母版：`assets/mark.svg` 与 `tools/icon.html`；界面动效 logo 用 `viewBox="0 0 86 86"`。
`scripts/make_icons.py` 重新生成 16–256 px 图标并覆盖 `assets/` 下的文件。

## 后端接口

共 120 条路由，全部在 `server/api.py`：

- 运行状态：`GET /health`、`GET /health/integrity`、`GET /events`（SSE 进度）
- 领域：`/domains` 增删改、合并、拆分（`/split`）、重命名、权重（`/weight`）、层级（`/parent`）、阈值（`auto-threshold`、`reset-threshold`）、`reassign`；
  综述草稿 `POST /domains/{id}/survey` 与 `/surveys`（读取/编辑/删除/列表）；
  `/domains/{id}/overview|graph|knowledge|search|papers|notes|config|citations|concepts|questions|reviews|report|survey|snapshots|subtopics|path`
- 领域发现：`GET /domains/discover`、`POST /domains/discover/apply`（逐篇接受/调整）
- 论文：`/papers`、`/papers/{id}`、`/pdf`、`/summary`（生成/取消）、`/highlights`（含 `auto`）、
  `/notes`（含标签）、`/card`（读/存/删、`/card/export`）、`/outline`（章节图表算法索引）、`/skim`、`/export`、
  `/questions`、`/citations`、`/subtopics`、`/domains`（归属）、`/move`、`/cancel`、`/retry`、`/touch`
- 精读：`POST /papers/{id}/translate`、`POST /papers/{id}/explain`
- 问答：`POST /papers/{id}/qa`、`/qa/sessions`（改名、删除）、`/qa/history`、`/qa/history-all`、
  `DELETE /papers/{id}/qa/turns/{id}`（修正范围时撤回上一轮）、
  `POST /papers/{id}/qa/turns/{id}/important`（标为重要并同步笔记）、
  `POST /papers/{id}/qa/scope/infer`（范围推断）、`GET /papers/{id}/qa/suggestions`（推荐问题）
- 导入：`POST /papers/import`、`POST /arxiv/search`、`POST /arxiv/batch-import`
- 概念与引用：`/concepts/mappings`、`POST /citations/{id}/resolve`、`/suggestions`
- 配置与审计：`/config`、`/llm/profiles`、`/llm/stats`、`/audit/report`、`/operations`、`/overview`、`/notes/tags`

## 关键配置

`设置` 面板可在线修改，落 `config` 表：

| 键 | 默认 | 作用 |
|---|---|---|
| `assign_threshold` | 0.5 | 质心余弦 ≥ 此值才归属该领域 |
| `link_threshold` | 0.35 | 弱关联建图阈值 |
| `domain_dispersion_weight` | 0.3 | 宽领域（跨类别）扣罚 λ |
| `max_tokens_default` | 2048 | 单次 LLM 输出上限 |

模型档案（`/llm/profiles`）字段：`profile_id`、`name`、`api_base`、`api_key`、`model_fast`、`model_pro`、`thinking`、`vision_model`。
`api_key` 由用户自填；`thinking` 默认 `disabled`；`vision_model` 留空时用 `model_fast`，设置面板未提供该字段。

系统提示词的「输出格式」段与解析代码是契约关系，改动需同步。

## 自检

```bash
python scripts/check_integrity.py <db_path>   # 完整性门禁：0 干净 / 1 有问题 / 2 路径错误
python scripts/test_schema.py                 # 迁移与生命周期
python scripts/test_api.py                    # 接口冒烟（临时 DATA_DIR）
python scripts/smoke_settings.py              # 设置、模型档案与接口守卫
python scripts/smoke_domains.py               # 领域、合并、检索与问答
python scripts/smoke_reading.py               # 阅读位置、快照、拆分与综述
python scripts/smoke_regression.py            # schema、领域上下文、综述持久化
node   scripts/test_figure_geom.js            # 图表坐标系单测
python scripts/e2e.py                         # 端到端：导入、位置、结构、综述、快照恢复
python scripts/smoke_import.py                # 导入/解析主链路（需联网访问 arXiv）
python scripts/audit.py                       # 问答引用编号抽样
```

`check_integrity.py` 要求数据库路径已存在。`smoke_import.py` 需要网络，可使用
`SCHOLARLENS_SMOKE_IMPORT_PDF=<本地 PDF>` 离线运行。

## 数据与隐私

数据目录 `<data>` 随运行形态而定：

- NSIS 安装版：`%APPDATA%\ScholarLens\data\`；
- portable 版：exe 同级的 `data\`；
- 源码运行：仓库的 `data\`。

数据库、PDF、缓存全在该目录下，备份与换机复制它即可；`data/` 与 `APIKEY.txt` 都在 `.gitignore` 内。

## 文档

- [docs/使用说明.md](docs/使用说明.md) — 安装、配置、日常操作、备份与 FAQ
- [docs/原理与流程.md](docs/原理与流程.md) — 架构、数据模型、导入/检索/阅读流程

## 许可

AGPL-3.0-only，全文见 [LICENSE](LICENSE)。Copyright © 2026 `w_a_n_g_q_i_y_i`。

第三方组件（Electron、FastAPI、PyMuPDF、pdf.js、sentence-transformers 等）各自遵循其原许可证。
