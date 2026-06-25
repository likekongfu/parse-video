"""parse-video-py 的命令行入口。"""

import typer

# Typer 会根据这些函数自动生成命令行帮助和子命令。
app = typer.Typer(
    name="parse-video-py",
    help="短视频解析工具，支持分享链接解析和启动 HTTP 服务。",
    add_completion=False,
)


@app.callback(context_settings={"help_option_names": ["-h", "--help"]})
def main():
    """命令行根入口，仅用于配置全局 help 行为。"""
    pass


@app.command()
def version():
    """显示当前包版本。"""
    typer.echo("parse-video-py 0.0.3")


@app.command()
def parse(
    urls: list[str] = typer.Argument(None, help="一个或多个视频分享链接"),
    fmt: str = typer.Option("text", "--format", help="输出格式: json, text"),
    file: str = typer.Option(
        None,
        "--file",
        "-f",
        help="从文件读取链接；每行一个链接，使用 - 表示从 stdin 读取",
    ),
):
    """解析视频分享链接，支持单条、批量和文件输入。"""
    # 延迟导入解析逻辑，让 `version` 等轻量命令不必加载全部解析依赖。
    from parse_video_py.cli._parse import run_parse

    run_parse(urls, fmt, file)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="服务监听地址"),
    port: int = typer.Option(8000, "--port", "-p", help="服务监听端口"),
):
    """启动 FastAPI HTTP 解析服务。"""
    # 延迟导入 uvicorn，避免只安装 CLI 依赖时整个命令行工具无法启动。
    try:
        import uvicorn
    except ImportError:
        typer.echo(
            "错误: uvicorn 未安装。请使用 parse-video-py[web] 安装 Web 服务依赖",
            err=True,
        )
        raise typer.Exit(code=1)

    uvicorn.run(
        "parse_video_py.web:app",
        host=host,
        port=port,
        reload=False,
    )
