"""Validation of file names and of the structure of Office Open XML documents.

These checks reduce the risk of processing clearly wrong or malicious files
(corrupted archives, zip bombs, disguised formats) but do NOT make a document
safe: every file remains untrusted input.
"""

from __future__ import annotations

import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.exceptions import InvalidDocumentError, UnsupportedFileTypeError

CONTENT_TYPES_PART = "[Content_Types].xml"
ROOT_RELS_PART = "_rels/.rels"
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_MAX_CONTENT_TYPES_BYTES = 2 * 1024 * 1024
_RATIO_CHECK_MIN_BYTES = 1024 * 1024
MAX_FILENAME_LENGTH = 200
_FORBIDDEN_CHARS = set('"<>:|?*\\/')


@dataclass(frozen=True)
class FormatSpec:
    label: str
    main_content_type: str


SUPPORTED_FORMATS: dict[str, FormatSpec] = {
    ".docx": FormatSpec(
        "DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    ),
    ".xlsx": FormatSpec("XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
    ".pptx": FormatSpec(
        "PPTX", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
    ),
}


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int
    max_uncompressed_bytes: int
    max_compression_ratio: int


def sanitize_filename(raw: str | None) -> str:
    """Reduce the client-provided name to a display-only name without any path.

    The result is only used in JSON responses and in the download
    Content-Disposition header, never to build paths on disk.
    """
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN_CHARS)
    name = name.strip().lstrip(".").strip()
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = Path(name).suffix[:10]
        name = name[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name or "document"


def get_extension(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_FORMATS:
        raise UnsupportedFileTypeError()
    return extension


def pdf_filename(original_filename: str) -> str:
    stem = Path(original_filename).stem.strip() or "document"
    return f"{stem}.pdf"


def _is_unsafe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return normalized.startswith("/") or ".." in parts or (parts and ":" in parts[0])


def validate_ooxml(path: Path, extension: str, limits: ZipLimits) -> None:
    """Check that `path` is an intact OOXML package of the type given by `extension`.

    Raises `InvalidDocumentError` with a safe message when something is wrong.
    """
    spec = SUPPORTED_FORMATS.get(extension)
    if spec is None:
        raise UnsupportedFileTypeError()
    mismatch = InvalidDocumentError(f"The file content does not match a valid {spec.label} document.")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise InvalidDocumentError("The document archive is empty.")
            if len(infos) > limits.max_entries:
                raise InvalidDocumentError("The document contains too many internal parts.")

            names: set[str] = set()
            total = 0
            for info in infos:
                if _is_unsafe_member(info.filename):
                    raise InvalidDocumentError("The document contains invalid internal paths.")
                if info.flag_bits & 0x1:
                    raise InvalidDocumentError("Encrypted or password-protected documents are not supported.")
                total += info.file_size
                if total > limits.max_uncompressed_bytes:
                    raise InvalidDocumentError("The uncompressed content of the document is too large.")
                if (
                    info.file_size > _RATIO_CHECK_MIN_BYTES
                    and info.file_size > info.compress_size * limits.max_compression_ratio
                ):
                    raise InvalidDocumentError("The document has an abnormal compression ratio.")
                names.add(info.filename)

            if CONTENT_TYPES_PART not in names or ROOT_RELS_PART not in names:
                raise mismatch
            if archive.getinfo(CONTENT_TYPES_PART).file_size > _MAX_CONTENT_TYPES_BYTES:
                raise mismatch

            root = fromstring(archive.read(CONTENT_TYPES_PART))
            main_parts = [
                (element.get("PartName") or "").lstrip("/")
                for element in root.iter(f"{_CT_NS}Override")
                if element.get("ContentType") == spec.main_content_type
            ]
            if not main_parts or main_parts[0] not in names:
                raise mismatch

            # CRC check of every part: the cost is bounded by the checks above.
            if archive.testzip() is not None:
                raise InvalidDocumentError("The document is corrupted.")
    except InvalidDocumentError:
        raise
    except zipfile.BadZipFile as exc:
        if "encrypt" in str(exc).lower():
            raise InvalidDocumentError("Encrypted documents are not supported.") from exc
        raise InvalidDocumentError() from exc
    except (
        zipfile.LargeZipFile,
        ParseError,
        DefusedXmlException,
        NotImplementedError,
        RuntimeError,
        EOFError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        raise InvalidDocumentError() from exc
