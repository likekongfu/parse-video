---
knowledge_version: 1
last_scanned_at: 2026-06-08
source_commit: dd55df3
---

# 构建运行部署

## 本地开发环境

| 依赖 | 版本/要求 | 证据来源 |
|---|---|---|
| Python | >=3.10 | `pyproject.toml:requires-python` |
| uv | 最新版（包管理+虚拟环境） | `README.md:69-78` |
| hatchling | 构建后端 | `pyproject.toml:build-backend` |

## 环境变量

| 变量 | 用途 | 是否敏感 | 默认值/示例 | 使用位置 | 证据来源 |
|---|---|---|---|---|---|
| `PARSE_VIDEO_USERNAME` | Basic Auth 用户名 | 是 | 不设置=不开启 | `web.py:34` | `web.py:34` |
| `PARSE_VIDEO_PASSWORD` | Basic Auth 密码 | 是 | 不设置=不开启 | `web.py:35` | `web.py:35` |
| `PARSE_VIDEO_PROXY` | HTTP/HTTPS 代理地址 | 是 | 不设置=直连 | `utils.py:create_async_client()` | `utils.py` |
| `DOCUMENT_CONVERTER_MAX_UPLOAD_BYTES` | 文档转换服务最大上传体积 | 否 | `20971520` | `document_convert_web.py` | `document_convert_web.py` |
| `DOCUMENT_CONVERTER_PDF_PASSWORD_MIN_LENGTH` | PDF 加密密码最小长度 | 否 | `6` | `document_convert_web.py` | `document_convert_web.py` |
| `DOCUMENT_CONVERTER_PDF_PASSWORD_MAX_LENGTH` | PDF 加密密码最大长度 | 否 | `32` | `document_convert_web.py` | `document_convert_web.py` |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 是 | 必填，无默认值 | `document_summary.py` | `document_summary.py` |
| `DEEPSEEK_MODEL` | 总结模型 | 否 | `deepseek-v4-flash` | `document_summary.py` | `document_summary.py` |
| `DEEPSEEK_API_URL` | Chat Completions 地址 | 否 | `https://api.deepseek.com/chat/completions` | `document_summary.py` | `document_summary.py` |
| `DEEPSEEK_TIMEOUT_SECONDS` | AI 请求超时 | 否 | `120` | `document_summary.py` | `document_summary.py` |
| `DEEPSEEK_MAX_INPUT_CHARS` | 单次总结最大输入字符数；超出时取开头和结尾并返回 source_truncated | 否 | `100000` | `document_summary.py` | `document_summary.py` |
| `DOCUMENT_SUMMARY_UPLOAD_DIR` | 总结文档持久化目录 | 否 | `data/document-summary` | `document_summary_web.py` | `document_summary_web.py` |

## 安装依赖

```bash
# 创建虚拟环境并安装全部依赖（推荐）
uv venv && uv pip install -e ".[all]"

# 仅安装核心+Web
uv pip install -e ".[web]"

# 仅安装核心+CLI
uv pip install -e ".[cli]"

# 安装开发依赖
uv pip install -e ".[all,dev]"
```

## 本地启动

```bash
# 开发模式（自动重载）
uvicorn main:app --reload

# 生产模式
uvicorn parse_video_py.web:app --host 0.0.0.0 --port 8000

# CLI 启动 Web 服务
parse-video-py serve --port 8000
```

## 测试命令

```bash
# 运行全部测试
pytest tests/ -v --tb=short

# 运行单个测试文件
pytest tests/test_utils.py -v

# 带超时限制（防止卡死）
pytest tests/ -v --tb=short --timeout=60

# 带覆盖率
pytest --cov=parse_video_py
```

## 代码质量检查

```bash
# 格式化
black .
isort .

# Lint
flake8 .

# 全部 pre-commit 检查
pre-commit run --all-files
```

## 构建命令

```bash
# 构建 wheel 包
uv build

# Docker 构建
docker build -t parse-video-py .
```

## 部署方式

**Docker 部署**（主要方式）：

- Dockerfile：`python:3.10-slim` + uv 安装依赖
- 暴露端口：8000
- 启动命令：`uvicorn parse_video_py.web:app --host 0.0.0.0 --port 8000`
- CI/CD：GitHub Actions 自动构建推送到 Docker Hub（`docker.yml`）
- 镜像：`wujunwei928/parse-video-py:latest`
- 证据来源：`Dockerfile`、`.github/workflows/docker.yml`
- 代理配置：`docker run -e PARSE_VIDEO_PROXY=http://proxy:端口`

**CI/CD 流程**：

- `python-app.yml`：push/PR 到 main → 安装依赖 → 运行 pytest
- `docker.yml`：push 到 main → 构建 Docker 镜像 → 推送 Docker Hub

## 数据库初始化/迁移

统一用户和文档总结使用 MySQL；部署文档总结前执行：

```bash
python scripts/migrate_unified_users.py
mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" -p "$MYSQL_DATABASE" \
  < migrations/20260713_document_summary.sql
```

媒体容器需要继续挂载 `/app/data`，以持久化 `DOCUMENT_SUMMARY_UPLOAD_DIR` 下的上传文件。

## 常见问题

| 问题 | 可能原因 | 排查方式 | 相关文件 |
|---|---|---|---|
| 解析失败返回 500 | 平台接口变更、分享链接过期 | 检查对应解析器的 HTTP 请求和响应 | `parser/<平台>.py` |
| "未检测到有效的分享链接" | URL 格式不匹配正则 | 检查 `utils.py:URL_REG` 正则 | `utils.py:4` |
| "does not have source config" | URL 域名未在映射表中 | 检查 `video_source_info_mapping` 的 `domain_list` | `parser/__init__.py:29-145` |
| Docker 构建失败 | 依赖安装问题 | 检查 `pyproject.toml` 和网络 | `Dockerfile` |
| pre-commit pytest 卡死 | 测试超时 | 添加 `--timeout=60` | `.pre-commit-config.yaml` |

## 代理测试

### 免费代理获取

免费 HTTP 代理不稳定，仅供测试。推荐来源：

- **ProxyScrape API**（实时，质量较好）：`https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all`
- **proxifly/free-proxy-list**（GitHub，每 5 分钟更新）：`https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.json`
- **free-proxy-list.net**：网页列表，需手动提取

### 快速验证代理是否生效

```bash
# 1. 设置代理
export PARSE_VIDEO_PROXY=http://代理地址:端口

# 2. 验证出口 IP 是否切换
python3 -c "
import asyncio, os
from parse_video_py.utils import create_async_client

async def check():
    client = create_async_client(follow_redirects=True)
    resp = await client.get('https://httpbin.org/ip', timeout=10)
    print(f'出口 IP: {resp.json()[\"origin\"]}')
    await client.aclose()

asyncio.run(check())
"

# 3. 测试解析
parse-video-py parse "https://v.douyin.com/xxx"

# 4. 清除代理
unset PARSE_VIDEO_PROXY
```

### 批量筛选可用代理

从 ProxyScrape 获取列表后并发测试，典型命中率约 20%（50 个中约 10 个可用）：

```python
import asyncio, os, urllib.request
from parse_video_py.utils import create_async_client

async def test_proxy(proxy_addr):
    os.environ["PARSE_VIDEO_PROXY"] = f"http://{proxy_addr}"
    client = create_async_client(follow_redirects=True)
    try:
        resp = await client.get("https://httpbin.org/ip", timeout=10)
        origin = resp.json()["origin"]
        await client.aclose()
        return proxy_addr, origin
    except Exception:
        await client.aclose()
        return None, None

async def main():
    url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"
    proxies = urllib.request.urlopen(url, timeout=10).read().decode().strip().split("\n")

    tasks = [test_proxy(p.strip()) for p in proxies[:30]]
    results = await asyncio.gather(*tasks)

    for addr, ip in results:
        if addr:
            print(f"✅ {addr} → {ip}")

asyncio.run(main())
```
