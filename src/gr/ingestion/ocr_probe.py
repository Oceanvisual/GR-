"""Startup probe: is tesseract installed and does it have the required languages (R-04)?"""

import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OcrAvailability:
    available: bool
    langs: list[str] = field(default_factory=list)
    version: str | None = None
    reason: str | None = None


def probe_tesseract(required_langs: str, *, enabled: bool = True) -> OcrAvailability:
    """`required_langs` is OCR_LANG, e.g. `rus+eng`; every listed language must be installed."""
    if not enabled:
        return OcrAvailability(available=False, reason="OCR отключён (OCR_ENABLED=false).")
    binary = shutil.which("tesseract")
    if binary is None:
        return OcrAvailability(available=False, reason="tesseract не установлен.")
    try:
        langs_out = subprocess.run(
            [binary, "--list-langs"], capture_output=True, text=True, timeout=10, check=True
        )
        version_out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return OcrAvailability(available=False, reason=f"tesseract не запускается: {exc}")
    installed = [line.strip() for line in langs_out.stdout.splitlines()[1:] if line.strip()]
    version_lines = (version_out.stdout or version_out.stderr).splitlines()
    version = version_lines[0].strip() if version_lines else None
    missing = [lang for lang in required_langs.split("+") if lang not in installed]
    if missing:
        return OcrAvailability(
            available=False,
            langs=installed,
            version=version,
            reason=f"нет языковых пакетов tesseract: {', '.join(missing)}",
        )
    return OcrAvailability(available=True, langs=installed, version=version)
