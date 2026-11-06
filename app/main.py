import base64
import io
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

import img2pdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import pymupdf
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from starlette.background import BackgroundTask

app = FastAPI(title="pdfTool")

A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
LAYOUT = img2pdf.get_layout_fun(A4, fit=img2pdf.FitMode.into, auto_orient=True)

# (scala, qualità JPEG): tentativi progressivi se il PDF supera il limite di peso
ATTEMPTS = [(1.0, 90), (0.85, 80), (0.7, 70), (0.55, 60), (0.4, 50)]


def prepare(data: bytes, rotation: int, enhance: bool, scale: float, quality: int) -> bytes:
    """Restituisce i byte originali se non serve toccare nulla (nessuna perdita di qualità)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise HTTPException(400, "Uno dei file non è un'immagine valida.")

    needs_work = (
        rotation % 360 != 0
        or enhance
        or scale < 1.0
        or img.format not in ("JPEG", "PNG")
        or img.mode in ("RGBA", "LA", "P")
    )
    if not needs_work:
        return data

    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.split()[-1])
        img = background

    if enhance:
        img = ImageOps.grayscale(img)
        img = ImageOps.autocontrast(img, cutoff=1)
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    if rotation % 360:
        img = img.rotate(-rotation, expand=True)  # gradi in senso orario

    if scale < 1.0:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def build_pdf(sources: List[bytes], items: List[dict], enhance: bool, scale: float, quality: int) -> bytes:
    """Assembla il PDF finale seguendo la sequenza di pagine scelta dall'utente.

    Ogni item e' {"file": indice, "page": numero pagina PDF oppure None se e' un'immagine, "rot": gradi}.
    """
    writer = PdfWriter()
    readers = {}
    for it in items:
        idx, rot = it["file"], it["rot"] % 360
        data = sources[idx]
        if it["page"] is None:
            one = img2pdf.convert([prepare(data, rot, enhance, scale, quality)], layout_fun=LAYOUT)
            writer.add_page(PdfReader(io.BytesIO(one)).pages[0])
        else:
            if idx not in readers:
                try:
                    readers[idx] = PdfReader(io.BytesIO(data))
                except Exception:
                    raise HTTPException(400, "Uno dei PDF non è valido.")
                if readers[idx].is_encrypted:
                    raise HTTPException(400, "Un PDF è protetto da password: non è supportato.")
            try:
                page = readers[idx].pages[it["page"]]
            except IndexError:
                raise HTTPException(400, "Pagina PDF inesistente.")
            if rot:
                page.rotate(rot)  # gradi in senso orario
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def run_ocr(src: Path, dst: Path, lang: str, deskew: bool) -> None:
    cmd = ["ocrmypdf", "-l", lang, "--skip-text", "--optimize", "1", "--output-type", "pdf"]
    if deskew:
        cmd.append("--deskew")
    cmd += [str(src), str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "L'OCR ha impiegato troppo tempo. Prova con meno pagine.")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise HTTPException(500, f"OCR non riuscito: {tail}")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/thumbs")
def thumbs(file: UploadFile = File(...)):
    """Miniature (JPEG, data URL) di tutte le pagine di un PDF."""
    data = file.file.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(400, "Il file non è un PDF valido.")
    if doc.needs_pass:
        raise HTTPException(400, "Il PDF è protetto da password: non è supportato.")
    out = []
    for page in doc:
        zoom = 260 / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.append("data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpeg", jpg_quality=70)).decode())
    doc.close()
    return {"thumbs": out}


@app.post("/api/preview")
def preview(file: UploadFile = File(...), page: int = Form(0)):
    """Una singola pagina di un PDF a risoluzione alta (JPEG), per l'anteprima ingrandita."""
    data = file.file.read()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(400, "Il file non è un PDF valido.")
    if doc.needs_pass:
        raise HTTPException(400, "Il PDF è protetto da password: non è supportato.")
    if not 0 <= page < len(doc):
        raise HTTPException(400, "Pagina inesistente.")
    pg = doc[page]
    zoom = min(1400 / max(pg.rect.width, 1), 3)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    doc.close()
    return Response(pix.tobytes("jpeg", jpg_quality=85), media_type="image/jpeg")


@app.post("/api/convert")
def convert(
    files: List[UploadFile] = File(...),
    items: str = Form("[]"),
    enhance: bool = Form(False),
    ocr: bool = Form(True),
    lang: str = Form("ita"),
    max_mb: float = Form(0),
    filename: str = Form("documento"),
):
    if not files:
        raise HTTPException(400, "Nessun file ricevuto.")
    if not re.fullmatch(r"[a-z]{3}(\+[a-z]{3})*", lang):
        raise HTTPException(400, "Lingua OCR non valida.")

    try:
        seq = [
            {
                "file": int(x["file"]),
                "page": None if x.get("page") is None else int(x["page"]),
                "rot": int(x.get("rot", 0)),
            }
            for x in json.loads(items)
        ]
    except Exception:
        raise HTTPException(400, "Sequenza di pagine non valida.")
    if not seq or any(not 0 <= it["file"] < len(files) for it in seq):
        raise HTTPException(400, "Sequenza di pagine non valida.")

    raw = [f.file.read() for f in files]

    limit = max_mb * 1024 * 1024 if max_mb > 0 else 0
    attempts = ATTEMPTS if limit else ATTEMPTS[:1]
    pdf = b""
    for scale, quality in attempts:
        pdf = build_pdf(raw, seq, enhance, scale, quality)
        # margine del 10% per il livello di testo aggiunto dall'OCR
        if not limit or len(pdf) <= limit * 0.9:
            break

    tmp = Path(tempfile.mkdtemp(prefix="pdftool_"))
    src, dst = tmp / "in.pdf", tmp / "out.pdf"
    src.write_bytes(pdf)

    try:
        if ocr:
            run_ocr(src, dst, lang, deskew=enhance)
        else:
            shutil.copy(src, dst)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    safe = re.sub(r"[^\w\- ]", "", filename).strip() or "documento"
    return FileResponse(
        dst,
        media_type="application/pdf",
        filename=f"{safe}.pdf",
        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
    )


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
