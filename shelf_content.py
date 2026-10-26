"""Native shelf content, including text which never becomes a disk document.

File references remain paths. Text references are opaque IDs backed by the
state's JSON mapping. A native text-only drag exposes only text/plain, so an
input field receives characters rather than a .txt file attachment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage, QPixmap

from flickdock_icons import document_pixmap, file_pixmap

TEXT_PREFIX = "flickdock:text:"
_CAPTURED_TEXT_NAME = re.compile(r"^拖入的文字(?: \(\d+\))?\.txt$", re.IGNORECASE)


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def _unique_image_path(directory: Path) -> Path:
    candidate = directory / "拖入的图片.png"
    if not candidate.exists():
        return candidate
    # UUID fallback has no fixed collection-size limit and avoids races with
    # another capture using the same human-readable file name.
    return directory / f"拖入的图片-{uuid4().hex[:12]}.png"


class ContentRepository:
    def __init__(self, texts: dict[str, str] | None = None, metadata: dict | None = None):
        self.thumbnail_provider = None
        self.metadata = {str(ref): dict(value) for ref, value in (metadata or {}).items()
                         if isinstance(ref, str) and isinstance(value, dict)} if isinstance(metadata, dict) else {}
        self.texts: dict[str, str] = {}
        if isinstance(texts, dict):
            self.texts = {
                str(ref): value for ref, value in texts.items()
                if self.is_text(ref) and isinstance(value, str)
            }

    @staticmethod
    def is_text(ref: str) -> bool:
        return isinstance(ref, str) and ref.startswith(TEXT_PREFIX)

    def exists(self, ref: str) -> bool:
        if self.is_text(ref):
            return ref in self.texts
        return isinstance(ref, str) and bool(ref) and Path(ref).exists()

    def title(self, ref: str) -> str:
        title = self.metadata.get(ref, {}).get('title')
        if isinstance(title, str) and title.strip():
            return title.strip().splitlines()[0][:160]
        if self.is_text(ref):
            content = self.texts.get(ref, "")
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            first = lines[0] if lines else "文字片段"
            # The underlying content is never truncated. Only the list label
            # needs a sensible size before the UI applies its pixel elision.
            return first[:100] + ("…" if len(first) > 100 else "")
        return Path(ref).name or str(ref)

    def text(self, ref: str) -> str | None:
        return self.texts.get(ref) if self.is_text(ref) else None

    def add_text(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A shelf text item requires nonempty text")
        for ref, existing in self.texts.items():
            if existing == text:
                return ref
        ref = TEXT_PREFIX + uuid4().hex
        self.texts[ref] = text
        return ref

    def export_texts(self, refs: list[str] | None = None) -> dict[str, str]:
        if refs is None:
            return dict(self.texts)
        return {ref: self.texts[ref] for ref in refs if ref in self.texts}

    def prune(self, refs: list[str]) -> None:
        keep = set(refs)
        self.texts = {ref: text for ref, text in self.texts.items() if ref in keep}
        self.metadata = {ref: data for ref, data in self.metadata.items() if ref in keep}

    def category(self, ref: str) -> str:
        if self.is_text(ref):
            text = (self.text(ref) or '').strip()
            return 'links' if re.fullmatch(r'https?://\S+', text, re.IGNORECASE) else 'text'
        suffix = Path(ref).suffix.lower()
        if suffix in {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.ico','.tif','.tiff','.svg','.svgz','.avif','.heic','.heif'}:
            return 'images'
        if suffix == '.ts':
            try:
                with Path(ref).open('rb') as stream: probe=stream.read(376)
                return 'video' if len(probe)>188 and probe[0]==0x47 and probe[188]==0x47 else 'files'
            except OSError: return 'files'
        if suffix in {'.mp4','.m4v','.mov','.mkv','.webm','.avi','.wmv','.mpg','.mpeg','.3gp','.m2ts','.vob'}:
            return 'video'
        if suffix in {'.mp3','.wav','.flac','.ogg','.aac','.m4a','.wma','.opus','.aiff','.aif','.amr','.mid','.midi'}:
            return 'audio'
        if suffix in {'.txt','.md','.markdown','.log','.csv','.tsv','.rtf'}:
            return 'text'
        return 'files'

    def export_metadata(self, refs=None):
        return {ref: dict(data) for ref, data in self.metadata.items() if refs is None or ref in refs}

    def mime_for(self, refs: list[str]) -> QMimeData:
        mime = QMimeData()
        paths = []
        text = []
        for ref in refs:
            if self.is_text(ref):
                content = self.text(ref)
                if content is not None:
                    text.append(content)
            elif self.exists(ref):
                paths.append(QUrl.fromLocalFile(str(Path(ref).resolve())))
        if paths:
            mime.setUrls(paths)
        if text:
            mime.setText("\n\n".join(text))
        # In particular, native-only text has no text/uri-list and no CF_HDROP.
        return mime

    def capture_mime(self, mime: QMimeData, capture_dir: Path) -> list[str]:
        local = [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]
        if local:
            return [str(Path(path).resolve()) for path in local]
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QPixmap):
                image = image.toImage()
            if isinstance(image, QImage) and not image.isNull():
                capture_dir = Path(capture_dir)
                capture_dir.mkdir(parents=True, exist_ok=True)
                path = _unique_image_path(capture_dir)
                if image.save(str(path), "PNG"):
                    return [str(path.resolve())]
        content = mime.text() if mime.hasText() else ""
        if not content and mime.hasUrls():
            content = "\n".join(url.toString() for url in mime.urls())
        if content.strip():
            return [self.add_text(content)]
        return []

    def item_pixmap(self, ref: str, size: int = 90, stack: int = 1, dpr=None) -> QPixmap:
        from async_images import RASTER_SUFFIXES
        provider = self.thumbnail_provider
        if self.is_text(ref):
            cover = self.metadata.get(ref, {}).get('preview_image')
            if provider is not None and isinstance(cover, str) and Path(cover).suffix.lower() in RASTER_SUFFIXES:
                return provider(cover, size, dpr=dpr)
            if isinstance(cover, str) and Path(cover).is_file():
                return file_pixmap(cover, size, stack, dpr=dpr)
            return document_pixmap("link" if self.category(ref)=='links' else "TEXT", size, stack, dpr=dpr)
        if provider is not None and Path(ref).suffix.lower() in RASTER_SUFFIXES:
            return provider(ref, size, dpr=dpr)
        return file_pixmap(ref, size, stack, dpr=dpr)

    def import_legacy(self, ref: str) -> str:
        if self.is_text(ref) or not isinstance(ref, str):
            return ref
        source = Path(ref)
        if source.parent.name.casefold() == "captured" and _CAPTURED_TEXT_NAME.fullmatch(source.name):
            try:
                text = _decode_text(source.read_bytes())
                if text.strip():
                    return self.add_text(text)
            except OSError:
                pass
        return ref


def merge_shelves(data: dict) -> dict:
    """Merge old groups into one shelf while retaining both zone semantics.

    IDs from separate groups can collide. Resolve each ID against its own
    group's text mapping and create/reuse a global ID by actual text content.
    Real .txt files remain file items unless they match our known old capture
    naming convention. No source capture file is deleted during migration.
    """
    data = data if isinstance(data, dict) else {}
    groups = data.get("shelves", [data])
    if not isinstance(groups, list):
        groups = []
    repository = ContentRepository()
    merged: dict = {"temporary": [], "permanent": [], "texts": {}}
    seen = {"temporary": set(), "permanent": set()}
    global_texts = data.get("texts", {})
    global_texts = global_texts if isinstance(global_texts, dict) else {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        local_texts = group.get("texts", {})
        local_texts = local_texts if isinstance(local_texts, dict) else {}
        for mode in ("temporary", "permanent"):
            refs = group.get(mode, group.get("paths", []) if mode == "permanent" else [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, str) or not ref:
                    continue
                if repository.is_text(ref):
                    text = local_texts.get(ref, global_texts.get(ref))
                    if not isinstance(text, str) or not text.strip():
                        continue
                    identical = next((key for key, value in repository.texts.items() if value == text), None)
                    if identical is not None:
                        ref = identical
                    elif ref not in repository.texts:
                        # Keep stable IDs when loading the already-merged
                        # format. A conflicting old group gets a fresh ID.
                        repository.texts[ref] = text
                    else:
                        ref = repository.add_text(text)
                else:
                    ref = repository.import_legacy(ref)
                key = ref if repository.is_text(ref) else os.path.normcase(os.path.abspath(ref))
                if key not in seen[mode]:
                    seen[mode].add(key)
                    merged[mode].append(ref)
        if "pos" not in merged and isinstance(group.get("pos"), list) and len(group["pos"]) == 2:
            merged["pos"] = list(group["pos"])
    merged["texts"] = repository.export_texts()
    return merged
