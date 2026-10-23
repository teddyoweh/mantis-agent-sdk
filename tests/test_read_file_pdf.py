"""``read_file`` on a PDF returns its text page by page (pypdf, or poppler's
pdftotext), with ``pages`` choosing which — as Claude Code's Read does. It used
to answer "mantis can't render PDF pages yet"."""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest

from mantis_agent.builtin_tools.fs import _pdf_page_numbers, read_file


def _tiny_pdf(path: Path, pages: list[str]) -> None:
    """A valid PDF with one line of Helvetica text per page, xref and all."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    for i, text in enumerate(pages):
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page_id} 0 R")
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objs.insert(2, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def _can_read_pdfs() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return shutil.which("pdftotext") is not None


@pytest.mark.skipif(not _can_read_pdfs(), reason="needs pypdf or poppler's pdftotext")
def test_reads_pdf_text_page_by_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _tiny_pdf(tmp_path / "doc.pdf", ["Revenue grew in July", "Methodology note", "Appendix tables"])
    out = anyio.run(lambda: read_file.fn(path="doc.pdf"))
    assert "3 pages" in out
    assert "--- page 1 ---" in out and "Revenue grew in July" in out and "Appendix tables" in out
    two = anyio.run(lambda: read_file.fn(path="doc.pdf", pages="2"))
    assert "Methodology note" in two and "Revenue grew" not in two


def test_page_specs() -> None:
    assert _pdf_page_numbers("3", 10) == [3]
    assert _pdf_page_numbers("1-3", 10) == [1, 2, 3]
    assert _pdf_page_numbers("2,7-9", 10) == [2, 7, 8, 9]
    assert _pdf_page_numbers("8-40", 10) == [8, 9, 10]        # clipped to the document
    assert _pdf_page_numbers(None, 50) == list(range(1, 21))   # at most 20 per call
    assert _pdf_page_numbers("nonsense", 4) == [1, 2, 3, 4]
