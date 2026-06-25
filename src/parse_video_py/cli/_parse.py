"""`parse-video-py parse` 命令的核心逻辑。"""

import asyncio
import sys
from pathlib import Path

import typer

from parse_video_py import parse_video_share_url
from parse_video_py.cli.output import output_batch_error, output_result
from parse_video_py.parser.base import VideoInfo
from parse_video_py.utils import extract_url

# 批量解析时限制并发，避免一次性对目标平台发起过多请求。
_CONCURRENCY_LIMIT = 10
_sem = asyncio.Semaphore(_CONCURRENCY_LIMIT)


def _read_inputs_from_file(file_path: str) -> list[str]:
    """从文件或 stdin 读取分享链接列表。"""
    if file_path == "-":
        lines = sys.stdin.read().splitlines()
    else:
        try:
            lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            typer.echo(f"无法读取文件: {file_path}", err=True)
            raise typer.Exit(code=1)

    # 去掉空行和首尾空白，避免把空字符串传给解析器。
    return [line.strip() for line in lines if line.strip()]


async def _parse_single(
    url: str,
) -> tuple[VideoInfo | None, str | None]:
    """解析单条输入，成功返回视频信息，失败返回错误文本。"""
    try:
        # 用户粘贴的内容可能是一整段分享文案，这里先提取里面的第一个 URL。
        extracted = extract_url(url)
        if not extracted:
            return None, f"未检测到有效的分享链接: {url}"

        info = await parse_video_share_url(extracted)
        return info, None
    except Exception as e:
        # CLI 不直接抛出内部异常，而是转换成用户可读的失败原因。
        return None, str(e)


async def _limited_parse_single(url: str) -> tuple[VideoInfo | None, str | None]:
    """带并发限制地解析单条链接。"""
    async with _sem:
        return await _parse_single(url)


async def _parse_batch(
    urls: list[str],
) -> list[tuple[str, VideoInfo | None, str | None]]:
    """批量解析链接，并保留每条输入和结果的对应关系。"""
    tasks = [_limited_parse_single(url) for url in urls]
    results = await asyncio.gather(*tasks)
    return [(url, info, err) for url, (info, err) in zip(urls, results)]


def run_parse(urls: list[str] | None, fmt: str, file: str | None) -> None:
    """解析命令入口，由 `cli/__init__.py` 的 Typer 命令调用。"""
    if fmt not in ("json", "text"):
        typer.echo(
            f"不支持的输出格式: {fmt}，可选值: json, text",
            err=True,
        )
        raise typer.Exit(code=1)

    if urls and file:
        typer.echo("不能同时指定链接参数和 --file 文件输入", err=True)
        raise typer.Exit(code=1)

    inputs: list[str] = []
    if file:
        inputs = _read_inputs_from_file(file)
    elif urls:
        inputs = list(urls)
    else:
        typer.echo("请提供要解析的链接，或使用 --file 指定输入文件", err=True)
        raise typer.Exit(code=1)

    if not inputs:
        return

    # 单条输入直接解析，失败时用非零退出码告诉调用方命令失败。
    if len(inputs) == 1:
        info, err = asyncio.run(_parse_single(inputs[0]))
        if err:
            typer.echo(f"解析失败: {err}", err=True)
            raise typer.Exit(code=1)
        output_result(info, fmt)
        return

    # 多条输入批量解析：允许部分失败，只在全部失败时返回非零退出码。
    results = asyncio.run(_parse_batch(inputs))
    fail_count = 0
    for i, (url, info, err) in enumerate(results):
        if i > 0 and fmt == "text":
            print()
        if err:
            output_batch_error(url, err)
            fail_count += 1
        else:
            output_result(info, fmt)

    if fail_count == len(inputs):
        typer.echo(f"全部 {len(inputs)} 条链接均解析失败", err=True)
        raise typer.Exit(code=1)
