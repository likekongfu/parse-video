"""CLI 输出格式化工具。"""

import dataclasses
import json
import sys

from parse_video_py.parser.base import VideoInfo


def format_text_output(info: VideoInfo) -> str:
    """把解析结果格式化成人类容易阅读的文本。"""
    lines = []
    lines.append(f"标题: {info.title}")
    lines.append(f"作者: {info.author.name} (UID: {info.author.uid})")

    if info.video_url:
        lines.append(f"视频地址: {info.video_url}")
    if info.cover_url:
        lines.append(f"封面地址: {info.cover_url}")
    if info.music_url:
        lines.append(f"音乐地址: {info.music_url}")

    if info.images:
        lines.append("图片列表:")
        for i, img in enumerate(info.images, 1):
            if img.live_photo_url:
                lines.append(f"  [{i}] {img.url} (LivePhoto: {img.live_photo_url})")
            else:
                lines.append(f"  [{i}] {img.url}")
    else:
        lines.append("图片数量: 0")

    return "\n".join(lines)


def format_json_output(info: VideoInfo) -> str:
    """把 dataclass 解析结果转换成 JSON 字符串。"""
    data = dataclasses.asdict(info)
    return json.dumps(data, ensure_ascii=False, indent=2)


def output_result(info: VideoInfo, fmt: str = "text") -> None:
    """把单条解析结果输出到 stdout。"""
    if fmt == "json":
        print(format_json_output(info))
    else:
        print(format_text_output(info))


def output_batch_error(input_url: str, error_msg: str) -> None:
    """批量解析时，把单条失败信息输出到 stderr。"""
    print(f"[失败] {input_url}", file=sys.stderr)
    print(f"错误: {error_msg}", file=sys.stderr)
