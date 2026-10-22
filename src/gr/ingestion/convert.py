"""Old and neighbouring word-processor formats — `.doc`, `.rtf`, `.odt` — through LibreOffice
(FR-001, R-04, T075).

The file is converted to DOCX by `soffice --headless` and then read as any DOCX. Each conversion
runs with its own temporary profile (two at once do not share one, and nothing is left behind)
in its own process group, so a hung conversion is killed with every process LibreOffice started.
This runs inside a parsing worker (T076), whose timeout is longer than the conversion's. Without
LibreOffice the user is asked to save the document as DOCX.
"""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from gr.domain.document import RawDocument
from gr.domain.enums import DocFormat
from gr.ingestion.base import IngestionError
from gr.ingestion.docx import DocxExtractor

CONVERTED = (DocFormat.DOC, DocFormat.RTF, DocFormat.ODT)
DEFAULT_TIMEOUT_S = 60.0


def soffice_binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def to_docx(data: bytes, fmt: DocFormat, *, binary: str, timeout_s: float) -> bytes:
    """The DOCX LibreOffice makes of `data`; `corrupted_file` when it cannot."""
    kind = fmt.value.upper()
    with tempfile.TemporaryDirectory(prefix="gr-convert-") as folder:
        work = Path(folder)
        source = work / f"source.{fmt.value}"
        source.write_bytes(data)
        command = [
            binary,
            f"-env:UserInstallation={(work / 'profile').as_uri()}",
            "--headless", "--norestore", "--nolockcheck", "--nodefault",
            "--convert-to", "docx:MS Word 2007 XML",
            "--outdir", str(work / "out"),
            str(source),
        ]  # fmt: skip
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # its own group: a timeout kills soffice.bin too
            )
        except OSError:
            raise _unavailable(fmt) from None
        try:
            process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise IngestionError(
                "corrupted_file",
                f"Файл {kind} не удалось сконвертировать за {timeout_s:g} с.",
                {"reason": "conversion_timeout"},
            ) from None
        result = work / "out" / "source.docx"
        if process.returncode != 0 or not result.is_file() or result.stat().st_size == 0:
            raise IngestionError(
                "corrupted_file",
                f"Файл {kind} не удалось сконвертировать: возможно, он повреждён.",
                {"reason": "conversion_failed"},
            )
        return result.read_bytes()


def _unavailable(fmt: DocFormat) -> IngestionError:
    return IngestionError(
        "unsupported_format",
        f"Файлы {fmt.value.upper()} читаются через LibreOffice, а его на сервере нет. "
        "Сохраните документ в формате DOCX и загрузите снова.",
        {"detected": fmt.value},
    )


class ConvertingExtractor:
    """A `.doc`, `.rtf` or `.odt` file: converted to DOCX, then read as DOCX."""

    def __init__(
        self,
        fmt: DocFormat,
        docx: DocxExtractor,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        binary: str | None = None,
    ) -> None:
        if fmt not in CONVERTED:
            raise ValueError(f"{fmt} is not converted")
        self.format = fmt
        self.docx = docx
        self.timeout_s = timeout_s
        self.binary = binary

    def extract(self, data: bytes) -> RawDocument:
        binary = self.binary or soffice_binary()
        if binary is None:
            raise _unavailable(self.format)
        raw = self.docx.extract(to_docx(data, self.format, binary=binary, timeout_s=self.timeout_s))
        return raw.model_copy(update={"format": self.format})
