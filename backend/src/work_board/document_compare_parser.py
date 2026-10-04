"""Fixed, literal invoice/CSV comparison inside the supervised parser child.

No model, filesystem selection, network, generated code or canonical writes.
PDF structure checks restrict the supported grammar; they are not malware scans.
"""
from __future__ import annotations

import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
import logging
import re
import zlib

CAPABILITY = "work.document-compare.v1"
JOB_KIND = "document_invoice_compare_v1"
OPERATION = "compare-line-totals-by-sku"
MIB = 1024 * 1024
PDF_LIMIT, CSV_LIMIT = 2 * MIB, MIB
SKU = re.compile(r"[A-Z0-9][A-Z0-9_.-]{0,31}\Z", re.ASCII)
QUANTITY = re.compile(r"[1-9][0-9]{0,6}\Z", re.ASCII)
PRICE = re.compile(r"(?:0|[1-9][0-9]{0,6})\.[0-9]{2}\Z", re.ASCII)
FONTS = frozenset("/" + name for name in (
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"))
FORBIDDEN = frozenset(("/A", "/AA", "/OpenAction", "/JavaScript", "/JS",
    "/AcroForm", "/Annots", "/EmbeddedFiles", "/EF", "/XObject", "/ToUnicode",
    "/FontDescriptor", "/FontFile", "/FontFile2", "/FontFile3", "/Differences",
    "/Encrypt", "/RichMedia", "/Launch", "/URI", "/SubmitForm", "/Metadata", "/Prev"))


class DocumentParseError(ValueError):
    """Stable literal reason; never echo malformed document contents to logs."""


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _row(fields, citation, digest):
    if len(fields) != 3 or any(len(v.encode("utf-8")) > 128 for v in fields):
        raise DocumentParseError("document_row_shape_unsupported")
    sku, quantity, price = fields
    if (SKU.fullmatch(sku) is None or QUANTITY.fullmatch(quantity) is None
        or int(quantity) > 1_000_000 or PRICE.fullmatch(price) is None):
        raise DocumentParseError("document_numeric_or_sku_grammar_unsupported")
    with localcontext() as context:
        context.prec = 32
        total = Decimal(quantity) * Decimal(price)
    return {"sku": sku, "quantity": quantity, "unit_price": price,
        "total": format(total, ".2f"), "citation": citation, "source_sha256": digest,
        "formula": f"{quantity} * {price}"}


def _insert(rows, row):
    if row["sku"] in rows:
        raise DocumentParseError("document_duplicate_sku")
    rows[row["sku"]] = row


def csv_rows(raw: bytes):
    if not 1 <= len(raw) <= CSV_LIMIT:
        raise DocumentParseError("document_csv_size_exceeded")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        raise DocumentParseError("document_csv_utf8_required") from None
    if any(ord(c) < 32 and c not in "\r\n" for c in text) or "\x7f" in text:
        raise DocumentParseError("document_csv_control_character")
    if "\r" in text.replace("\r\n", ""):
        raise DocumentParseError("document_csv_line_ending_unsupported")
    lines = text.splitlines()
    if not lines or lines[0] != "SKU,QTY,UNIT_PRICE" or len(lines) > 2001:
        raise DocumentParseError("document_csv_header_or_rows_unsupported")
    rows = {}
    for ordinal, line in enumerate(lines[1:], 2):
        if not line or len(line.encode()) > 512:
            raise DocumentParseError("document_csv_blank_or_long_row")
        try:
            fields = next(csv.reader([line], strict=True))
        except (csv.Error, StopIteration):
            raise DocumentParseError("document_csv_row_unsupported") from None
        _insert(rows, _row(fields, {"csv_row": ordinal}, sha256(raw)))
    if not rows:
        raise DocumentParseError("document_csv_empty")
    return rows


def strict_flate(encoded: bytes, remaining: int) -> bytes:
    ceiling = min(PDF_LIMIT, remaining)
    if ceiling < 1:
        raise DocumentParseError("document_decoded_aggregate_exceeded")
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(encoded, ceiling + 1)
    except zlib.error:
        raise DocumentParseError("document_flate_malformed") from None
    if len(decoded) > ceiling or decoder.unconsumed_tail:
        raise DocumentParseError("document_decoded_stream_exceeded")
    if not decoder.eof or decoder.unused_data:
        raise DocumentParseError("document_flate_malformed")
    return decoded


def pdf_rows(raw: bytes):
    if not 1 <= len(raw) <= PDF_LIMIT or not raw.startswith(b"%PDF-"):
        raise DocumentParseError("document_pdf_signature_or_size_unsupported")
    ending = re.search(rb"startxref\s+([0-9]{1,8})\s+%%EOF\s*\Z", raw)
    if ending is None or raw[int(ending[1]):int(ending[1])+4] != b"xref":
        raise DocumentParseError("document_pdf_classic_xref_required")
    import pypdf
    from pypdf import PdfReader, apply_configuration
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject, DecodedStreamObject, ContentStream
    from pypdf._page import _get_page_resources
    if pypdf.__version__ != "6.19.0":
        raise DocumentParseError("document_parser_version_blocked")
    warnings = []
    class RejectWarnings(logging.Handler):
        def emit(self, record):
            if len(warnings) < 1:
                warnings.append(True)
    logger = logging.getLogger("pypdf")
    handler = RejectWarnings(); logger.addHandler(handler)
    try:
        with apply_configuration(maximum_declared_stream_length=PDF_LIMIT,
            array_based_stream_maximum_output_length=PDF_LIMIT,
            zlib_maximum_output_length=PDF_LIMIT, zlib_maximum_recovery_input_length=65536,
            lzw_maximum_output_length=PDF_LIMIT, run_length_maximum_output_length=PDF_LIMIT,
            jbig2_maximum_output_length=PDF_LIMIT, image_maximum_buffer_size=PDF_LIMIT,
            page_tree_maximum_entries=32, page_tree_maximum_depth=4,
            xform_maximum_invocations_per_extraction=0, jbig2dec_binary=None):
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted or reader.xref_objStm or not 1 <= len(reader.pages) <= 10:
                raise DocumentParseError("document_pdf_structure_unsupported")
            stack = [reader.trailer]; visited = set(); edges = 0
            while stack:
                obj = stack.pop(); edges += 1
                if edges > 4096:
                    raise DocumentParseError("document_pdf_object_bound_exceeded")
                if isinstance(obj, IndirectObject):
                    identity = (obj.idnum, obj.generation)
                    if identity in visited: continue
                    visited.add(identity); obj = obj.get_object()
                if isinstance(obj, DictionaryObject):
                    if FORBIDDEN.intersection(obj) or obj.get("/Type") in {"/ObjStm", "/XRef", "/Action", "/Filespec", "/EmbeddedFile"}:
                        raise DocumentParseError("document_pdf_structure_unsupported")
                    stack.extend(dict.values(obj))
                elif isinstance(obj, ArrayObject):
                    if len(obj) > 2048:
                        raise DocumentParseError("document_pdf_object_bound_exceeded")
                    stack.extend(obj)
            rows = {}; text_bytes = 0; remaining = 4 * MIB
            for physical_page, page in enumerate(reader.pages, 1):
                resources = _get_page_resources(page)
                fonts = resources.get("/Font", {}).get_object() if isinstance(resources.get("/Font"), IndirectObject) else resources.get("/Font", {})
                if not fonts or len(fonts) > 16:
                    raise DocumentParseError("document_pdf_font_unsupported")
                for font_ref in dict.values(fonts):
                    font = font_ref.get_object()
                    if (font.get("/Subtype") != "/Type1" or font.get("/BaseFont") not in FONTS
                        or font.get("/Encoding") not in (None, "/WinAnsiEncoding", "/StandardEncoding", "/MacRomanEncoding")
                        or "/Widths" in font):
                        raise DocumentParseError("document_pdf_font_unsupported")
                stream = page.get("/Contents")
                stream = stream.get_object() if stream is not None else None
                if not isinstance(stream, StreamObject):
                    raise DocumentParseError("document_pdf_single_contents_required")
                filter_name = stream.get("/Filter")
                if filter_name not in (None, "/FlateDecode"):
                    raise DocumentParseError("document_pdf_filter_unsupported")
                params = stream.get("/DecodeParms")
                if params is not None and (not isinstance(params, DictionaryObject) or set(params) - {"/Predictor"} or params.get("/Predictor", 1) != 1):
                    raise DocumentParseError("document_pdf_predictor_unsupported")
                data = strict_flate(stream._data, remaining) if filter_name else stream._data
                if len(data) > min(PDF_LIMIT, remaining):
                    raise DocumentParseError("document_decoded_aggregate_exceeded")
                remaining -= len(data)
                decoded = DecodedStreamObject(); decoded.set_data(data)
                contents = ContentStream(decoded, reader)
                if len(contents.operations) > 50000 or any(op == b"INLINE IMAGE" for _, op in contents.operations):
                    raise DocumentParseError("document_pdf_content_unsupported")
                from pypdf.generic import NameObject
                page[NameObject("/Contents")] = decoded
                text = page.extract_text(extraction_mode="plain")
                text_bytes += len(text.encode("utf-8"))
                if text_bytes > 128 * 1024:
                    raise DocumentParseError("document_extracted_text_exceeded")
                lines = text.splitlines()
                if physical_page == 1 and (len(lines) < 2 or lines[:2] != ["INVOICE USD", "SKU QTY UNIT_PRICE"]):
                    raise DocumentParseError("document_invoice_header_unsupported")
                for ordinal, line in enumerate(lines, 1):
                    if not line: continue
                    if len(line.encode()) > 128:
                        raise DocumentParseError("document_invoice_line_exceeded")
                    if (physical_page == 1 and ordinal == 1 and line == "INVOICE USD") or line == "SKU QTY UNIT_PRICE": continue
                    _insert(rows, _row(line.split(" "), {"physical_page": physical_page, "extraction_line": ordinal}, sha256(raw)))
            if warnings or not rows:
                raise DocumentParseError("document_pdf_partial_or_empty_text")
            return rows
    except DocumentParseError:
        raise
    except Exception:
        raise DocumentParseError("document_pdf_malformed_or_unsupported") from None
    finally:
        logger.removeHandler(handler)


def compare(pdf: bytes, csv_source: bytes):
    left, right = pdf_rows(pdf), csv_rows(csv_source)
    skus = sorted(set(left) | set(right))
    if len(skus) > 1000:
        raise DocumentParseError("document_joined_skus_exceeded")
    entries = []
    output = io.StringIO(newline=""); writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["SKU", "STATUS", "PDF_QTY", "PDF_UNIT_PRICE", "PDF_TOTAL", "CSV_QTY", "CSV_UNIT_PRICE", "CSV_TOTAL", "DIFFERENCE_CSV_MINUS_PDF", "PDF_CITATION", "CSV_ROW", "PDF_SHA256", "CSV_SHA256", "PDF_FORMULA", "CSV_FORMULA", "DIFFERENCE_FORMULA"])
    report = ["Invoice comparison: USD", "Operation: " + OPERATION,
        "PDF SHA-256: " + sha256(pdf), "CSV SHA-256: " + sha256(csv_source),
        "Extraction ordinals cite parser text, not geometric line positions.", "Memory: no_learning", ""]
    for sku in skus:
        a, b = left.get(sku), right.get(sku)
        with localcontext() as context:
            context.prec = 32
            difference = format(Decimal(b["total"]) - Decimal(a["total"]), ".2f") if a and b else None
        status = "pdf_only" if b is None else "csv_only" if a is None else "equal" if difference == "0.00" else "different"
        formula = f'{b["total"]} - {a["total"]}' if a and b else None
        entries.append({"sku": sku, "status": status, "pdf": a, "csv": b,
            "difference_csv_minus_pdf": difference, "difference_formula": formula})
        pdf_cite = f'page {a["citation"]["physical_page"]}, extraction line {a["citation"]["extraction_line"]}' if a else ""
        csv_cite = str(b["citation"]["csv_row"]) if b else ""
        writer.writerow([sku, status, *(a.get(k, "") if a else "" for k in ("quantity", "unit_price", "total")),
            *(b.get(k, "") if b else "" for k in ("quantity", "unit_price", "total")), difference or "", pdf_cite, csv_cite,
            sha256(pdf), sha256(csv_source), a["formula"] if a else "", b["formula"] if b else "", formula or ""])
        report.append(f"{sku}: {status}; PDF {a['total'] if a else 'missing'} ({pdf_cite}); CSV {b['total'] if b else 'missing'} (row {csv_cite}); difference {difference if difference is not None else 'missing'}")
    report_bytes = ("\n".join(report) + "\n").encode(); csv_bytes = output.getvalue().encode()
    manifest = canonical({"schema": "document_invoice_compare.v1", "operation": OPERATION,
        "pdf_sha256": sha256(pdf), "csv_sha256": sha256(csv_source), "rows": entries, "no_learning": True})
    if len(report_bytes) > 65536 or len(csv_bytes) > 262144 or len(manifest) > 32768:
        raise DocumentParseError("document_output_bound_exceeded")
    return {"report": report_bytes.decode(), "csv": csv_bytes.decode(), "manifest": json.loads(manifest), "no_learning": True}
