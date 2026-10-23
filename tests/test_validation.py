from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.exceptions import InvalidDocumentError, UnsupportedFileTypeError
from app.services.validation import (
    ZipLimits,
    get_extension,
    pdf_filename,
    sanitize_filename,
    validate_ooxml,
)
from tests.ooxml_factory import SAMPLES, build_zip, docx_parts, make_docx, make_xlsx

LIMITS = ZipLimits(max_entries=100, max_uncompressed_bytes=50 * 1024 * 1024, max_compression_ratio=100)


def _write(tmp_path: Path, data: bytes, name: str = "input.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.docx", "report.docx"),
        ("../../etc/passwd.docx", "passwd.docx"),
        ("C:\\Users\\john\\Desktop\\budget.xlsx", "budget.xlsx"),
        ("/var/tmp/../slides.pptx", "slides.pptx"),
        ('rep"o\x00rt\r\n.docx', "report.docx"),
        ("...hidden.docx", "hidden.docx"),
        ("", "document"),
        (None, "document"),
        ("../..", "document"),
        ("résumé final.docx", "résumé final.docx"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_and_keeps_extension() -> None:
    result = sanitize_filename("a" * 500 + ".docx")
    assert len(result) <= 200
    assert result.endswith(".docx")


@pytest.mark.parametrize("name", ["a.docx", "B.DOCX", "c.XlSx", "d.pptx"])
def test_supported_extensions(name: str) -> None:
    assert get_extension(name) in {".docx", ".xlsx", ".pptx"}


@pytest.mark.parametrize("name", ["a.doc", "b.pdf", "c.docm", "d.txt", "noext", "e.docx.exe", "f.odt"])
def test_unsupported_extensions(name: str) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        get_extension(name)


def test_pdf_filename() -> None:
    assert pdf_filename("report.docx") == "report.pdf"
    assert pdf_filename("budget 2025.v2.xlsx") == "budget 2025.v2.pdf"


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_valid_documents(tmp_path: Path, extension: str) -> None:
    validate_ooxml(_write(tmp_path, SAMPLES[extension]()), extension, LIMITS)


def test_extension_mismatch_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidDocumentError, match="XLSX"):
        validate_ooxml(_write(tmp_path, make_docx()), ".xlsx", LIMITS)
    with pytest.raises(InvalidDocumentError, match="DOCX"):
        validate_ooxml(_write(tmp_path, make_xlsx(), "b.bin"), ".docx", LIMITS)


@pytest.mark.parametrize(
    "data",
    [
        b"this is not a zip file",
        b"PK\x03\x04" + b"\x00" * 50,
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512,  # OLE: .doc or encrypted OOXML
    ],
)
def test_not_a_zip(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, data), ".docx", LIMITS)


def test_zip_without_content_types(tmp_path: Path) -> None:
    parts = docx_parts()
    del parts["[Content_Types].xml"]
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


def test_main_part_missing(tmp_path: Path) -> None:
    parts = docx_parts()
    del parts["word/document.xml"]
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


def test_macro_enabled_document_renamed_is_rejected(tmp_path: Path) -> None:
    parts = docx_parts()
    parts["[Content_Types].xml"] = str(parts["[Content_Types].xml"]).replace(
        "wordprocessingml.document.main+xml", "ms-word.document.macroEnabled.main+xml"
    ).replace("application/vnd.openxmlformats-officedocument.ms-word", "application/vnd.ms-word")
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


def test_malicious_xml_entities_are_rejected(tmp_path: Path) -> None:
    parts = docx_parts()
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">&lol2;</Types>'
    )
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


def test_corrupted_crc_is_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in docx_parts("content to be corrupted").items():
            archive.writestr(name, data)
    raw = bytearray(buffer.getvalue())
    index = raw.find(b"content to be corrupted")
    raw[index] ^= 0xFF
    with pytest.raises(InvalidDocumentError):
        validate_ooxml(_write(tmp_path, bytes(raw)), ".docx", LIMITS)


def test_compression_bomb_is_rejected(tmp_path: Path) -> None:
    parts = docx_parts()
    parts["word/media/bomb.bin"] = b"\x00" * (5 * 1024 * 1024)
    with pytest.raises(InvalidDocumentError, match="compression ratio"):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


def test_uncompressed_size_limit(tmp_path: Path) -> None:
    parts = docx_parts()
    parts["word/media/big.bin"] = bytes(range(256)) * 8192  # 2 MiB, hard to compress
    limits = ZipLimits(max_entries=100, max_uncompressed_bytes=1024 * 1024, max_compression_ratio=1000)
    with pytest.raises(InvalidDocumentError, match="too large"):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", limits)


def test_too_many_entries(tmp_path: Path) -> None:
    parts = docx_parts()
    parts.update({f"word/extra/{i}.xml": "<x/>" for i in range(200)})
    with pytest.raises(InvalidDocumentError, match="too many"):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)


@pytest.mark.parametrize("member", ["../evil.xml", "/abs/evil.xml", "word/../../evil.xml", "C:/evil.xml"])
def test_path_traversal_members_are_rejected(tmp_path: Path, member: str) -> None:
    parts = docx_parts()
    parts[member] = "<x/>"
    with pytest.raises(InvalidDocumentError, match="internal paths"):
        validate_ooxml(_write(tmp_path, build_zip(parts)), ".docx", LIMITS)
