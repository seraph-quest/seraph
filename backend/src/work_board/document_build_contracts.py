"""Closed, content-only local document generation contracts (no authority)."""
from __future__ import annotations

import json
import math
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SPEC_BYTES = 65536
MAX_FILE_BYTES = 4 * 1024 * 1024
DOCX_MEDIA = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
XLSX_MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PDF_MEDIA = 'application/pdf'
CellValue = str | int | float | bool | None


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)


def coordinate(value: str) -> tuple[int, int]:
    match = re.fullmatch(r'\$?([A-Z]{1,2})\$?([1-9][0-9]{0,2})', value)
    if not match:
        raise ValueError('document_cell_coordinate_invalid')
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    row = int(match[2])
    if row > 256 or column > 64:
        raise ValueError('document_cell_extent_exceeded')
    return row - 1, column - 1


def literal(value):
    if type(value) not in (str, int, float, bool, type(None)):
        raise ValueError('document_cell_type_invalid')
    if isinstance(value, str) and len(value.encode('utf-8')) > 4096:
        raise ValueError('document_cell_text_bound')
    if type(value) in (int, float) and (abs(value) > 1e100 or not math.isfinite(value)):
        raise ValueError('document_number_bound')
    return value


def text_content(value):
    if any(ord(char) < 32 and char not in '\t\n\r' or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF) for char in value):
        raise ValueError('document_text_character_invalid')


class Section(Closed):
    heading: str = Field(max_length=200)
    paragraphs: list[str] = Field(max_length=128)
    citation_refs: list[str] = Field(max_length=16)


class Citation(Closed):
    source_ref: str = Field(min_length=1, max_length=512)
    label: str = Field(min_length=1, max_length=200)


class ReportTable(Closed):
    title: str = Field(max_length=200)
    columns: list[str] = Field(min_length=1, max_length=64)
    rows: list[list[CellValue]] = Field(max_length=256)
    citation_refs: list[str] = Field(max_length=16)

    @model_validator(mode='after')
    def rectangular(self):
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError('document_table_not_rectangular')
            for value in row:
                literal(value)
        for column in self.columns:
            if len(column.encode()) > 4096:
                raise ValueError('document_column_bound')
        return self


class Cell(Closed):
    sheet: str
    cell: str
    value: CellValue


class Formula(Closed):
    sheet: str
    cell: str
    expression: str = Field(min_length=1, max_length=512)


class CellFormat(Closed):
    sheet: str
    cell: str
    preset: Literal['plain', 'integer', 'decimal', 'percent', 'currency', 'header']


class SpreadsheetSpec(Closed):
    sheet_names: list[str] = Field(min_length=1, max_length=8)
    cells: list[Cell] = Field(max_length=16384)
    formulas: list[Formula] = Field(max_length=2048)
    formats: list[CellFormat] = Field(max_length=16384)

    @model_validator(mode='after')
    def bounded(self):
        if len(set(name.lower() for name in self.sheet_names)) != len(self.sheet_names):
            raise ValueError('document_duplicate_sheet')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,30}', name) or name.lower() == 'overview' for name in self.sheet_names):
            raise ValueError('document_sheet_name_invalid')
        if len(self.cells) + len(self.formulas) > 16384:
            raise ValueError('document_cell_count_bound')
        for entries in (self.cells + self.formulas, self.formats):
            seen = set()
            for entry in entries:
                key = (entry.sheet, coordinate(entry.cell))
                if entry.sheet not in self.sheet_names or key in seen:
                    raise ValueError('document_duplicate_or_foreign_cell')
                seen.add(key)
        for entry in self.cells:
            literal(entry.value)
            if type(entry.value) is str:
                text_content(entry.value)
        for entry in self.formulas:
            if len(entry.expression.encode()) > 512:
                raise ValueError('document_formula_byte_bound')
        return self


class DocumentBuildSpec(Closed):
    kind: Literal['report', 'brief', 'table_workbook']
    title: str = Field(min_length=1, max_length=200)
    sections: list[Section] = Field(max_length=32)
    tables: list[ReportTable | SpreadsheetSpec] = Field(max_length=16)
    citations: list[Citation] = Field(max_length=16)
    style_preset: Literal['plain', 'compact']

    @model_validator(mode='after')
    def bounded(self):
        if self.kind == 'table_workbook':
            if len(self.tables) != 1 or not isinstance(self.tables[0], SpreadsheetSpec):
                raise ValueError('document_workbook_requires_one_spreadsheet')
        elif any(not isinstance(table, ReportTable) for table in self.tables):
            raise ValueError('document_report_table_required')
        if sum(len(section.paragraphs) for section in self.sections) > 128:
            raise ValueError('document_paragraph_count_bound')
        refs = [citation.source_ref for citation in self.citations]
        if len(set(refs)) != len(refs) or any(len(ref.encode()) > 512 for ref in refs):
            raise ValueError('document_citation_invalid')
        for item in [*self.sections, *[t for t in self.tables if isinstance(t, ReportTable)]]:
            if any(ref not in refs for ref in item.citation_refs):
                raise ValueError('document_citation_unknown')
        if any(len(text.encode()) > 4096 for section in self.sections for text in section.paragraphs):
            raise ValueError('document_paragraph_byte_bound')
        strings = [self.title, *[citation.label for citation in self.citations], *refs]
        for section in self.sections:
            strings.extend([section.heading, *section.paragraphs])
        for table in self.tables:
            if isinstance(table, ReportTable):
                strings.extend([table.title, *table.columns, *[cell for row in table.rows for cell in row if type(cell) is str]])
        for text in strings:
            text_content(text)
        if len(canonical_spec(self)) > MAX_SPEC_BYTES:
            raise ValueError('document_spec_byte_bound')
        if self.kind == 'table_workbook':
            from .document_build_formula import calculate, FormulaError
            from pydantic_core import PydanticCustomError
            try:
                calculate(self.tables[0])
            except FormulaError as exc:
                raise PydanticCustomError('document_formula_invalid', '{sheet}!{cell}: {formula_code}',
                    {'sheet': exc.sheet, 'cell': exc.cell, 'formula_code': exc.code}) from None
        return self


def canonical_spec(spec: DocumentBuildSpec) -> bytes:
    return json.dumps(spec.model_dump(mode='json'), ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True).encode()


class DocumentBuildArtifact(Closed):
    artifact_ref: str = Field(pattern=r'^document-build:[0-9a-f-]{36}:(editable|pdf)$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=1, le=MAX_FILE_BYTES)
    media_type: Literal[DOCX_MEDIA, XLSX_MEDIA, PDF_MEDIA]


class DocumentOutput(Closed):
    editable_artifact: DocumentBuildArtifact
    pdf_artifact: DocumentBuildArtifact | None
    source_refs: list[str] = Field(max_length=16)
    warnings: list[str] = Field(max_length=16)

    @model_validator(mode='after')
    def bounded(self):
        if self.editable_artifact.media_type not in (DOCX_MEDIA, XLSX_MEDIA) or not self.editable_artifact.artifact_ref.endswith(':editable'):
            raise ValueError('document_editable_artifact_invalid')
        if self.pdf_artifact and (self.pdf_artifact.media_type != PDF_MEDIA or not self.pdf_artifact.artifact_ref.endswith(':pdf') or self.pdf_artifact.artifact_ref.rsplit(':', 1)[0] != self.editable_artifact.artifact_ref.rsplit(':', 1)[0]):
            raise ValueError('document_pdf_artifact_invalid')
        if self.pdf_artifact is None and not self.warnings:
            raise ValueError('document_missing_pdf_warning_required')
        if any(len(warning) > 200 for warning in self.warnings) or len(set(self.source_refs)) != len(self.source_refs) or any(not ref or len(ref.encode()) > 512 for ref in self.source_refs):
            raise ValueError('document_output_bound')
        return self
