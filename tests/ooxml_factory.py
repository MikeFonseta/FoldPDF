"""Generators of minimal but valid Office Open XML documents, without external dependencies.

Used both by the unit tests (validation) and by the integration tests with
real LibreOffice.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DOC_REL = f"{_R}/officeDocument"

DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
XLSX_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def content_types(overrides: dict[str, str]) -> str:
    items = "".join(f'<Override PartName="{part}" ContentType="{ctype}"/>' for part, ctype in overrides.items())
    return (
        f'{_XML}<Types xmlns="{_CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{items}</Types>"
    )


def relationships(*rels: tuple[str, str, str]) -> str:
    items = "".join(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>' for rid, rtype, target in rels)
    return f'{_XML}<Relationships xmlns="{_REL_NS}">{items}</Relationships>'


def build_zip(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def docx_parts(text: str = "Sample document") -> dict[str, str | bytes]:
    return {
        "[Content_Types].xml": content_types({"/word/document.xml": DOCX_MAIN}),
        "_rels/.rels": relationships(("rId1", _DOC_REL, "word/document.xml")),
        "word/document.xml": (
            f'{_XML}<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:body></w:document>"
        ),
    }


def make_docx(text: str = "Sample document") -> bytes:
    return build_zip(docx_parts(text))


def make_xlsx(text: str = "Sample sheet") -> bytes:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return build_zip(
        {
            "[Content_Types].xml": content_types(
                {
                    "/xl/workbook.xml": XLSX_MAIN,
                    "/xl/worksheets/sheet1.xml":
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                }
            ),
            "_rels/.rels": relationships(("rId1", _DOC_REL, "xl/workbook.xml")),
            "xl/workbook.xml": (
                f'{_XML}<workbook xmlns="{ns}" xmlns:r="{_R}">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": relationships(("rId1", f"{_R}/worksheet", "worksheets/sheet1.xml")),
            "xl/worksheets/sheet1.xml": (
                f'{_XML}<worksheet xmlns="{ns}"><sheetData><row r="1">'
                f'<c r="A1" t="inlineStr"><is><t>{escape(text)}</t></is></c><c r="B1"><v>42</v></c>'
                "</row></sheetData></worksheet>"
            ),
        }
    )


_EMPTY_TREE = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
)


def _theme() -> str:
    def color(tag: str, value: str) -> str:
        return f'<a:{tag}><a:srgbClr val="{value}"/></a:{tag}>'

    colors = "".join(
        color(tag, value)
        for tag, value in [
            ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "1F497D"), ("lt2", "EEECE1"),
            ("accent1", "4F81BD"), ("accent2", "C0504D"), ("accent3", "9BBB59"), ("accent4", "8064A2"),
            ("accent5", "4BACC6"), ("accent6", "F79646"), ("hlink", "0000FF"), ("folHlink", "800080"),
        ]
    )
    font = '<a:latin typeface="Liberation Sans"/><a:ea typeface=""/><a:cs typeface=""/>'
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line = f'<a:ln w="9525">{fill}</a:ln>'
    effect = "<a:effectStyle><a:effectLst/></a:effectStyle>"
    return (
        f'{_XML}<a:theme xmlns:a="{_A}" name="Sample"><a:themeElements>'
        f'<a:clrScheme name="Sample">{colors}</a:clrScheme>'
        f'<a:fontScheme name="Sample"><a:majorFont>{font}</a:majorFont><a:minorFont>{font}</a:minorFont></a:fontScheme>'
        f'<a:fmtScheme name="Sample"><a:fillStyleLst>{fill * 3}</a:fillStyleLst>'
        f"<a:lnStyleLst>{line * 3}</a:lnStyleLst><a:effectStyleLst>{effect * 3}</a:effectStyleLst>"
        f"<a:bgFillStyleLst>{fill * 3}</a:bgFillStyleLst></a:fmtScheme>"
        "</a:themeElements></a:theme>"
    )


def make_pptx(text: str = "Sample presentation") -> bytes:
    ns = f'xmlns:a="{_A}" xmlns:r="{_R}" xmlns:p="{_P}"'
    pml = "application/vnd.openxmlformats-officedocument.presentationml"
    clr_map = (
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    )
    text_box = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Text 1"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="7315200" cy="1371600"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="3200"/>'
        f"<a:t>{escape(text)}</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    return build_zip(
        {
            "[Content_Types].xml": content_types(
                {
                    "/ppt/presentation.xml": PPTX_MAIN,
                    "/ppt/slideMasters/slideMaster1.xml": f"{pml}.slideMaster+xml",
                    "/ppt/slideLayouts/slideLayout1.xml": f"{pml}.slideLayout+xml",
                    "/ppt/slides/slide1.xml": f"{pml}.slide+xml",
                    "/ppt/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml",
                }
            ),
            "_rels/.rels": relationships(("rId1", _DOC_REL, "ppt/presentation.xml")),
            "ppt/presentation.xml": (
                f"{_XML}<p:presentation {ns}>"
                '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
                "</p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": relationships(
                ("rId1", f"{_R}/slideMaster", "slideMasters/slideMaster1.xml"),
                ("rId2", f"{_R}/slide", "slides/slide1.xml"),
                ("rId3", f"{_R}/theme", "theme/theme1.xml"),
            ),
            "ppt/slideMasters/slideMaster1.xml": (
                f"{_XML}<p:sldMaster {ns}><p:cSld><p:spTree>{_EMPTY_TREE}</p:spTree></p:cSld>{clr_map}"
                '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>'
            ),
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": relationships(
                ("rId1", f"{_R}/slideLayout", "../slideLayouts/slideLayout1.xml"),
                ("rId2", f"{_R}/theme", "../theme/theme1.xml"),
            ),
            "ppt/slideLayouts/slideLayout1.xml": (
                f'{_XML}<p:sldLayout {ns} type="blank"><p:cSld name="Blank"><p:spTree>{_EMPTY_TREE}</p:spTree>'
                "</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
            ),
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": relationships(
                ("rId1", f"{_R}/slideMaster", "../slideMasters/slideMaster1.xml"),
            ),
            "ppt/slides/slide1.xml": (
                f"{_XML}<p:sld {ns}><p:cSld><p:spTree>{_EMPTY_TREE}{text_box}</p:spTree></p:cSld>"
                "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
            ),
            "ppt/slides/_rels/slide1.xml.rels": relationships(
                ("rId1", f"{_R}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ),
            "ppt/theme/theme1.xml": _theme(),
        }
    )


SAMPLES = {".docx": make_docx, ".xlsx": make_xlsx, ".pptx": make_pptx}
