---
knowledge_version: 1
last_scanned_at: 2026-06-08
source_commit: dd55df3
confidence: high
---

# 项目地图

## 技术栈

| 类型 | 技术/框架 | 版本 | 证据来源 |
|---|---|---|---|
| 语言 | Python | >=3.10 | `pyproject.toml:requires-python` |
| Web 框架 | FastAPI | >=0.110 | `pyproject.toml:web deps` |
| ASGI 服务器 | uvicorn | >=0.29 | `pyproject.toml:web deps` |
| 模板引擎 | Jinja2 | >=3.1 | `pyproject.toml:web deps` |
| MCP 集成 | fastapi-mcp | >=0.4 | `pyproject.toml:web deps` |
| CLI 框架 | Typer | >=0.12 | `pyproject.toml:cli deps` |
| CLI 输出 | Rich | >=13.0 | `pyproject.toml:cli deps` |
| HTTP 客户端 | httpx / aiohttp | >=0.27 / >=3.9 | `pyproject.toml:dependencies` |
| HTML 解析 | parsel + lxml | >=1.9 / >=5.0 | `pyproject.toml:dependencies` |
| JSON 查询 | jmespath | >=1.0 | `pyproject.toml:dependencies` |
| UA 伪装 | fake-useragent | >=1.5 | `pyproject.toml:dependencies` |
| 构建工具 | hatchling | - | `pyproject.toml:build-system` |
| 包管理 | uv | - | `README.md:69-78` |
| 测试 | pytest + pytest-asyncio | >=8.0 / >=0.23 | `pyproject.toml:dev deps` |
| 代码格式 | black + isort + flake8 | line-length: 88 | `pyproject.toml:tool.black/isort` |

## 目录结构

| 路径 | 作用 | 是否核心 | 证据来源 |
|---|---|---|---|
| `main.py` | Web 服务启动入口 | 是 | `main.py:1-6` |
| `src/parse_video_py/` | 主包源码 | 是 | `pyproject.toml:packages` |
| `src/parse_video_py/parser/` | 26 个平台解析器 + BaseParser + 路由映射 | 是 | `src/parse_video_py/parser/__init__.py` |
| `src/parse_video_py/web.py` | FastAPI 应用、路由、Basic Auth | 是 | `src/parse_video_py/web.py` |
| `src/parse_video_py/cli/` | CLI 命令行工具 | 是 | `src/parse_video_py/cli/__init__.py` |
| `src/parse_video_py/utils.py` | URL 提取、query 参数解析工具 | 是 | `src/parse_video_py/utils.py` |
| `src/parse_video_py/templates/` | Jinja2 HTML 模板 | 否 | `src/parse_video_py/templates/` |
| `tests/` | 测试用例 | 否 | `tests/` |
| `docs/` | 文档（agents 配置等） | 否 | `docs/` |
| `.github/workflows/` | CI/CD 配置 | 否 | `.github/workflows/` |

## 启动入口

| 类型 | 入口位置 | 启动方式 | 证据来源 |
|---|---|---|---|
| Web 服务 | `main.py` | `uvicorn main:app --reload` | `main.py:4-6` |
| Web 服务（生产） | `parse_video_py.web:app` | `uvicorn parse_video_py.web:app --host 0.0.0.0 --port 8000` | `Dockerfile:20` |
| CLI | `parse_video_py.cli:app` | `parse-video-py parse/serve/version` | `pyproject.toml:project.scripts` |
| Python SDK | `parse_video_py.parse_video_share_url` | `asyncio.run(parse_video_share_url(url))` | `src/parse_video_py/__init__.py:1` |

## 核心模块

| 模块 | 路径 | 职责 | 被谁调用 | 修改风险 | 证据来源 |
|---|---|---|---|---|---|
| BaseParser | `parser/base.py` | 定义解析器抽象基类、数据结构、VideoSource 枚举 | 所有解析器 | **高** | `parser/base.py` |
| 解析器路由 | `parser/__init__.py` | VideoSource→域名→解析器映射、`parse_video_share_url()`、`parse_video_id()` | web.py、cli | **高** | `parser/__init__.py:29-193` |
| Web 应用 | `web.py` | FastAPI 路由、Basic Auth、MCP 挂载 | uvicorn | **高** | `web.py` |
| CLI 入口 | `cli/__init__.py` | Typer 命令注册（parse/serve/version） | pyproject.toml scripts | 中 | `cli/__init__.py` |
| CLI 解析逻辑 | `cli/_parse.py` | 解析命令核心逻辑、批量解析、并发控制 | cli/__init__.py | 中 | `cli/_parse.py` |
| CLI 输出 | `cli/output.py` | 结果格式化（text/json） | cli/_parse.py | 低 | `cli/output.py` |
| 工具函数 | `utils.py` | URL 提取、query 参数解析、HTTP 客户端工厂 | web.py、cli/_parse.py、所有解析器 | **高** | `utils.py` |
| 26 个解析器 | `parser/*.py` | 各平台视频/图集解析 | parser/__init__.py 路由 | 中 | `parser/` |

## 外部依赖

| 依赖 | 用途 | 配置位置 | 调用位置 | 证据来源 |
|---|---|---|---|---|
| httpx | HTTP 请求（异步） | `pyproject.toml` | 各平台解析器（通过 `utils.py:create_async_client()` 工厂创建） | `parser/douyin.py`、`parser/weibo.py` 等 |
| aiohttp | HTTP 请求（异步，部分解析器使用） | `pyproject.toml` | 部分解析器 | `pyproject.toml:dependencies` |
| fake-useragent | 随机 User-Agent 生成 | `pyproject.toml` | `BaseParser.get_default_headers()` | `parser/base.py:99` |
| parsel + lxml | HTML/XPath 解析 | `pyproject.toml` | 部分解析器 | `pyproject.toml:dependencies` |
| jmespath | JSON 数据查询 | `pyproject.toml` | 部分解析器 | `pyproject.toml:dependencies` |
| pyyaml | YAML 解析（小红书解析器用） | `pyproject.toml` | `parser/redbook.py` | `parser/redbook.py:6` |
| fastapi-mcp | MCP 协议集成 | `pyproject.toml` | `web.py` | `web.py:10,26-27` |

## 数据存储

项目使用 SQLAlchemy Core，并复用同一组 MySQL 环境变量；未配置 MySQL 时回退至 `MATERIAL_DB_PATH` 指定的 SQLite。

- `web.py`：素材记录缓存。
- `user_db.py`：`users`、`user_identities`、`qr_login_sessions`。
- `qr_auth.py`：小程序码生成、状态、一次性 ticket 和网页 Cookie 会话。
- `document_summary.py`：PDF/DOCX 文本、20 类文档识别、动态总结卡片和结构化总结缓存。
- `document_summary_web.py`：登录用户文档上传、解析、总结和历史路由。
- `document_translation.py`：按标题/段落分块调用 DeepSeek，并缓存翻译和导出结果。
- `document_translation_web.py`：登录用户翻译与 DOCX/TXT 导出路由。
- `processing_history.py`：统一保存 PDF、文档、图片、视频处理记录与限量查询。
- `processing_history_web.py`：基于网页 Cookie 会话的统一处理历史写入和查询接口。
- `ocr_service.py`：PaddleOCR 3.x 懒加载、串行推理与结果归一化。
- `ocr_web.py`：登录用户 JPG/PNG/PDF 上传、PDF 逐页渲染和 OCR 接口。
- `migrations/20260713_unified_users.sql`：MySQL 建表迁移。
- `migrations/20260713_document_summary.sql`：`documents` 与 `document_tasks` 建表迁移。
- `migrations/20260713_document_summary_types.sql`：已建库环境增加文档类型与类型来源字段。
- `migrations/20260713_document_translation.sql`：`document_translations` 建表迁移。

## API 路由

| 方法 | 路径 | 功能 | Handler | 证据来源 |
|---|---|---|---|---|
| GET | `/` | Web 界面 | `web.py:read_item` | `web.py:64` |
| GET | `/video/share/url/parse` | 分享链接解析 | `web.py:share_url_parse` | `web.py:75` |
| GET | `/video/id/parse` | 视频 ID 解析 | `web.py:video_id_parse` | `web.py:98` |
| MCP | `/mcp` | AI 工具集成 | FastApiMCP 自动注册 | `web.py:26-27,114` |
| POST | `/auth/documents/upload` | 上传 PDF/DOCX 并返回 document_id | `document_summary_web.py` | `document_summary_web.py` |
| POST | `/auth/documents/{id}/parse` | 提取并缓存文档文本 | `document_summary_web.py` | `document_summary_web.py` |
| POST | `/auth/documents/{id}/summarize` | DeepSeek 自动分类与动态结构化总结；可选 document_type/regenerate 手动重生成 | `document_summary_web.py` | `document_summary_web.py` |
| POST | `/auth/documents/{id}/save` | 保存到用户历史 | `document_summary_web.py` | `document_summary_web.py` |
| GET | `/auth/documents/history` | 查询用户保存记录 | `document_summary_web.py` | `document_summary_web.py` |
| POST | `/auth/processing-history` | 写入当前登录用户的文件处理记录 | `processing_history_web.py` | `processing_history_web.py` |
| GET | `/auth/processing-history` | 查询当前登录用户的统一处理历史 | `processing_history_web.py` | `processing_history_web.py` |
| POST | `/auth/ocr` | 使用 PaddleOCR 识别 JPG、PNG 或扫描版 PDF | `ocr_web.py` | `ocr_web.py` |
| POST | `/auth/documents/{id}/translate` | 按语言、模式、风格和术语表翻译 | `document_translation_web.py` | `document_translation_web.py` |
| GET | `/auth/documents/{id}/translations/{translation_id}/export` | 导出 DOCX/TXT | `document_translation_web.py` | `document_translation_web.py` |

## 环境变量

| 变量 | 用途 | 是否敏感 | 默认值 | 使用位置 | 证据来源 |
|---|---|---|---|---|---|
| `PARSE_VIDEO_USERNAME` | Basic Auth 用户名 | 是 | 未设置=不开启 | `web.py:34` | `web.py:34` |
| `PARSE_VIDEO_PASSWORD` | Basic Auth 密码 | 是 | 未设置=不开启 | `web.py:35` | `web.py:35` |
| `PARSE_VIDEO_PROXY` | HTTP 代理地址 | 是 | 未设置=不使用代理，格式：`http://[user:pass@]host:port` | `utils.py:create_async_client()` | `utils.py` |

## 未确认事项

- 无
