"""Local file naming, archive and short-description helpers."""
from __future__ import annotations

from pathlib import Path
import zipfile


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法找到可用文件名")


def zip_items(paths: list[str], destination: Path) -> Path:
    destination = unique_path(destination)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for raw in paths:
            source = Path(raw)
            if source.is_file():
                archive.write(source, source.name)
            elif source.is_dir():
                for child in source.rglob("*"):
                    if child.is_file():
                        archive.write(child, str(Path(source.name) / child.relative_to(source)))
    return destination


def kind(path: str) -> str:
    return Path(path).suffix.lstrip(".").upper()[:4] or "FILE"


def kind_colors(text: str) -> tuple[str, str]:
    return {
        "PNG": ("#4779fa", "#7855ed"), "JPG": ("#4779fa", "#7855ed"),
        "JPEG": ("#4779fa", "#7855ed"), "ZIP": ("#f7ba32", "#acd61b"),
        "PDF": ("#fa8248", "#eb446d"), "MP4": ("#19c7d5", "#4378ed"),
        "DOCX": ("#49aaed", "#3659b4"), "XLSX": ("#31be7e", "#0b8057"),
    }.get(text, ("#53b4e9", "#6071d9"))


TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".html",
                 ".py", ".js", ".ts", ".css", ".ini", ".yaml", ".yml"}


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


def text_snippet(path: str, limit: int = 240) -> str | None:
    source = Path(path)
    if source.suffix.lower() not in TEXT_SUFFIXES or not source.is_file():
        return None
    try:
        with source.open("rb") as stream:
            data = stream.read(4096)
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("gb18030", errors="replace")
        content = content.strip()
        return content[:limit] + ("…" if len(content) > limit else "")
    except OSError:
        return None
