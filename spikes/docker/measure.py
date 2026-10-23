"""Spike T073 measurements, run inside the spike image with fixtures mounted at /fixtures."""

import json
import os
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pypdfium2 as pdfium
from rapidfuzz.distance import Levenshtein

FIXTURES = Path("/fixtures")
WORK = Path("/tmp/spike")


def first_line(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return (out.stdout or out.stderr).splitlines()[0]


def normalize(text: str) -> str:
    return " ".join(text.split())


def ocr_page(dpi: int, psm: int) -> dict[str, object]:
    document = pdfium.PdfDocument(str(FIXTURES / "scan.pdf"))
    start = time.perf_counter()
    image = document[0].render(scale=dpi / 72).to_pil().convert("L")
    png = WORK / f"page_{dpi}.png"
    image.save(png)
    render_s = time.perf_counter() - start
    start = time.perf_counter()
    out = subprocess.run(
        ["tesseract", str(png), "stdout", "-l", "rus+eng", "--psm", str(psm), "tsv"],
        capture_output=True,
        text=True,
        check=True,
    )
    ocr_s = time.perf_counter() - start
    rows = [line.split("\t") for line in out.stdout.splitlines()[1:]]
    words = [(r[11], float(r[10])) for r in rows if len(r) == 12 and r[11].strip()]
    text = " ".join(word for word, _ in words)
    expected = normalize((FIXTURES / "scan.expected.txt").read_text(encoding="utf-8"))
    cer = Levenshtein.distance(normalize(text), expected) / len(expected)
    confs = [conf for _, conf in words if conf >= 0]
    return {
        "dpi": dpi,
        "psm": psm,
        "render_s": round(render_s, 2),
        "ocr_s": round(ocr_s, 2),
        "cer_pct": round(cer * 100, 2),
        "mean_conf": round(sum(confs) / len(confs), 1) if confs else None,
        "words": len(words),
    }


def convert(src: Path, fmt: str) -> dict[str, object]:
    profile = WORK / "lo_profile"
    start = time.perf_counter()
    out = subprocess.run(
        [
            "soffice",
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--convert-to",
            fmt,
            "--outdir",
            str(WORK),
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    elapsed = time.perf_counter() - start
    target = WORK / f"{src.stem}.{fmt}"
    return {
        "from": src.suffix,
        "to": fmt,
        "seconds": round(elapsed, 2),
        "ok": target.exists(),
        "rc": out.returncode,
    }


def volume_write() -> dict[str, object]:
    probe = Path("/data/probe.txt")
    try:
        probe.write_text("ok", encoding="utf-8")
        return {"uid": os.getuid(), "writable": True}
    except OSError as exc:
        return {"uid": os.getuid(), "writable": False, "error": str(exc)}


def healthcheck() -> dict[str, object]:
    www = WORK / "www"
    www.mkdir(parents=True, exist_ok=True)
    (www / "healthz").write_text('{"status":"ok"}', encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(www))
    server = ThreadingHTTPServer(("127.0.0.1", 8000), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    probe = (
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
    )
    rc = subprocess.run(["python", "-c", probe], check=False).returncode
    server.shutdown()
    return {"python_probe_rc": rc, "curl_present": shutil.which("curl") is not None}


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    docx = WORK / "sample.docx"
    shutil.copy(FIXTURES / "regulation_manual.docx", docx)
    to_doc = convert(docx, "doc")
    results = {
        "tesseract": first_line(["tesseract", "--version"]),
        "langs": subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, check=False
        ).stdout.split()[-3:],
        "soffice": first_line(["soffice", "--version"]),
        "ocr": [ocr_page(300, 3), ocr_page(300, 6), ocr_page(200, 3)],
        "convert": [
            to_doc,
            convert(WORK / "sample.doc", "docx") if to_doc["ok"] else None,
        ],
        "volume": volume_write(),
        "healthcheck": healthcheck(),
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
