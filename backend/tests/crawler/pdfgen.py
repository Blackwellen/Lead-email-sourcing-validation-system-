"""Build a tiny valid PDF by hand (no reportlab) for extraction tests."""


def make_pdf(lines: list[str], *, mailto: str | None = None, pages: int = 1) -> bytes:
    objs: list[bytes] = []

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n_fixed = 3  # catalog, pages, font
    page_ids = [n_fixed + 1 + i * 2 for i in range(pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(pages):
        content_id = page_ids[i] + 1
        annots = ""
        if mailto and i == 0:
            annots = f" /Annots [<< /Type /Annot /Subtype /Link /Rect [10 10 100 30] /A << /S /URI /URI (mailto:{mailto}) >> >>]"
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_id} 0 R{annots} >>".encode()
        )
        y = 750
        ops = ["BT", "/F1 12 Tf"]
        for ln in lines:
            ops.append(f"1 0 0 1 50 {y} Tm ({esc(ln)}) Tj")
            y -= 18
        ops.append("ET")
        stream = "\n".join(ops).encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
