"""Writers for small, valid office files and PDFs, used by the demo and the tests."""

from __future__ import annotations

import zipfile
from pathlib import Path


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF (Helvetica, WinAnsi) that pypdfium2 can read."""

    def esc(line: str) -> str:
        out = []
        for byte in line.encode("cp1252", errors="replace"):
            ch = chr(byte)
            if ch in "\\()":
                out.append("\\" + ch)
            elif 32 <= byte < 127:
                out.append(ch)
            else:
                out.append(f"\\{byte:03o}")
        return "".join(out)

    ops = ["BT", "/F1 11 Tf", "14 TL", "72 760 Td"]
    for line in lines:
        ops.append(f"({esc(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return _zip(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
            ),
            "word/document.xml": document,
        }
    )


def make_pptx(slides: list[list[str]]) -> bytes:
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    files = {"[Content_Types].xml": "<Types/>"}
    for n, lines in enumerate(slides, 1):
        paras = "".join(f"<a:p><a:r><a:t>{_xml_escape(t)}</a:t></a:r></a:p>" for t in lines)
        files[f"ppt/slides/slide{n}.xml"] = (
            f"<p:sld {ns} xmlns:p='p'><a:txBody>{paras}</a:txBody></p:sld>"
        )
    return _zip(files)


def _zip(files: dict[str, str]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def make_xlsx(path: Path, rows: list[list[object]], title: str = "Budget") -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(row)
    wb.save(path)
