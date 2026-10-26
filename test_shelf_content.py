"""Offscreen regression checks; never moves the pointer or opens a window."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import tempfile

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage, Qt
from PySide6.QtWidgets import QApplication

from shelf_content import ContentRepository, TEXT_PREFIX, merge_shelves

app = QApplication([])
with tempfile.TemporaryDirectory(prefix="flickdock-content-") as directory:
    root = Path(directory)
    repo = ContentRepository()
    raw = "  第一行\n第二行 🌈\r\n\t保留空白  "
    payload = QMimeData()
    payload.setText(raw)
    refs = repo.capture_mime(payload, root / "captured")
    assert len(refs) == 1 and refs[0].startswith(TEXT_PREFIX)
    assert repo.text(refs[0]) == raw
    assert not (root / "captured").exists(), "text must not create capture files"
    mime = repo.mime_for(refs)
    assert mime.text() == raw and mime.hasText()
    assert not mime.hasUrls() and not mime.hasFormat("text/uri-list")
    assert repo.add_text(raw) == refs[0]
    assert repo.exists(refs[0]) and repo.title(refs[0]) == "第一行"
    saved = repo.export_texts()
    assert ContentRepository(saved).text(refs[0]) == raw

    file = root / "user.txt"
    file.write_text("ordinary text file", encoding="utf8")
    filemime = QMimeData()
    filemime.setUrls([QUrl.fromLocalFile(str(file))])
    filemime.setText("file incidental text")
    assert repo.capture_mime(filemime, root / "captured") == [str(file.resolve())]
    assert repo.import_legacy(str(file)) == str(file)
    mixed = repo.mime_for([str(file), refs[0]])
    assert mixed.hasUrls() and mixed.text() == raw
    assert Path(mixed.urls()[0].toLocalFile()).resolve() == file.resolve()
    assert not repo.item_pixmap(refs[0], 32).isNull()

    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.blue)
    imagemime = QMimeData()
    imagemime.setImageData(image)
    imagerefs = repo.capture_mime(imagemime, root / "captured")
    assert Path(imagerefs[0]).suffix == ".png" and Path(imagerefs[0]).exists()

    linkmime = QMimeData()
    linkmime.setUrls([QUrl("https://example.com/a")])
    linkref = repo.capture_mime(linkmime, root / "captured")[0]
    assert repo.text(linkref) == "https://example.com/a"

    legacy = root / "captured" / "拖入的文字 (2).txt"
    legacy.write_bytes(raw.encode("utf8"))
    legacyref = repo.import_legacy(str(legacy))
    assert repo.text(legacyref) == raw and legacy.exists()
    collision = TEXT_PREFIX + "collision"
    state = {"shelves": [
        {"temporary": [str(file), str(legacy), collision], "permanent": [], "texts": {collision: "甲"}},
        {"temporary": [str(file), collision], "permanent": [str(file)], "texts": {collision: "乙"}},
        {"paths": [str(file)]},
    ]}
    merged = merge_shelves(state)
    loaded = ContentRepository(merged["texts"])
    assert len(merged["temporary"]) == 4
    assert merged["permanent"] == [str(file)]
    values = [loaded.text(ref) for ref in merged["temporary"] if loaded.is_text(ref)]
    assert values == [raw, "甲", "乙"]
    assert str(legacy) not in merged["temporary"]
    assert file.exists() and legacy.exists()
    restored = merge_shelves(merged)
    assert restored["temporary"] == merged["temporary"]
    assert restored["texts"] == merged["texts"]
    assert len(merge_shelves({"temporary": "bad", "texts": []})["temporary"]) == 0

print("SHELF_CONTENT_OK")
