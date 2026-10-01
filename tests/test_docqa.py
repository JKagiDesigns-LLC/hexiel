"""Document Q&A: extraction (PDF pages, Word, Markdown), FTS index, citations, incremental re-index."""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexiel import docqa
from hexiel.tooling import Toolchain
from hexiel.tools import ToolContext


def make_pdf(path: Path, pages: list[str]) -> None:
    """A minimal valid PDF with one line of Helvetica text per page (no extra libraries)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')


def ctx(tmp_path):
    return ToolContext(cwd=str(tmp_path), chain=Toolchain())


def setup_docs(tmp_path):
    d = tmp_path / "papers"
    d.mkdir()
    make_pdf(d / "study.pdf", ["Introduction to soil moisture sensors",
                               "Results: irrigation reduced water use by 31 percent in the trial"])
    make_docx(d / "notes.docx", ["Meeting notes", "The grant deadline is March 14 for the drought project"])
    (d / "readme.md").write_text("# Lab handbook\nSamples are stored at minus 80 degrees in freezer B.\n")
    return d


def test_index_and_cited_search(tmp_path):
    setup_docs(tmp_path)
    msg = docqa._index({"path": "papers", "semantic": False}, ctx(tmp_path)).text
    assert "indexed 3 new/changed document(s)" in msg
    hit = docqa._search({"query": "how much did irrigation reduce water use?"}, ctx(tmp_path)).text
    assert "[papers/study.pdf p.2]" in hit and "31 percent" in hit
    assert "p.1]" not in hit.split("\n\n")[0]             # the right page ranks first
    assert "[papers/notes.docx]" in docqa._search({"query": "grant deadline"}, ctx(tmp_path)).text
    assert "freezer B" in docqa._search({"query": "where are samples stored"}, ctx(tmp_path)).text


def test_reindex_only_changed_files(tmp_path):
    d = setup_docs(tmp_path)
    docqa._index({"path": "papers", "semantic": False}, ctx(tmp_path))
    again = docqa._index({"path": "papers", "semantic": False}, ctx(tmp_path)).text
    assert "indexed 0 new/changed" in again and "3 unchanged" in again
    (d / "readme.md").write_text("# Lab handbook\nSamples moved to freezer C.\n")
    third = docqa._index({"path": "papers", "semantic": False}, ctx(tmp_path)).text
    assert "indexed 1 new/changed" in third
    hit = docqa._search({"query": "samples freezer"}, ctx(tmp_path)).text
    assert "freezer C" in hit and "freezer B" not in hit      # stale passages replaced


def test_search_before_index(tmp_path):
    r = docqa._search({"query": "anything"}, ctx(tmp_path))
    assert r.is_error and "docs_index" in r.text
