import { useEffect, useMemo, useRef, useState } from "react";
import { nextPdfPage, previousPdfPage, spreadsheetRequest, type PdfPagination } from "./document-viewer-navigation";
import { loadDocumentPreview } from "./document-viewer-client";

const GRID_ROWS = 10;
const GRID_COLUMNS = 10;
type UnknownRecord = Record<string, unknown>;
type ViewerState = "idle" | "loading" | "ready" | "error";

function text(value: unknown, fallback = ""): string { return typeof value === "string" ? value : fallback; }
function number(value: unknown, fallback = 0): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function columnName(column: number): string { let name = ""; while (column > 0) { const remainder = (column - 1) % 26; name = String.fromCharCode(65 + remainder) + name; column = Math.floor((column - 1) / 26); } return name; }
function formatBytes(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`; }

function ExtractionSummary({ extraction }: { extraction?: UnknownRecord }) {
  if (!extraction) return null;
  return <dl className="extraction-summary"><div><dt>Status</dt><dd>{text(extraction.status, "unknown")}</dd></div><div><dt>Non-empty items</dt><dd>{number(extraction.nonEmptyItems).toLocaleString()}</dd></div><div><dt>Pages</dt><dd>{number(extraction.pageCount).toLocaleString()}</dd></div><div><dt>Text blocks</dt><dd>{number(extraction.textBlockCount).toLocaleString()}</dd></div><div><dt>Warnings</dt><dd>{number(extraction.warningCount).toLocaleString()}</dd></div></dl>;
}

function SpreadsheetGrid({ response, onNavigate }: { response: Extract<DocumentContentResponse, { ok: true }>; onNavigate: (sheetName: string, rowStart: number, columnStart: number) => void }) {
  const worksheets = (response.worksheets ?? []) as UnknownRecord[];
  const grid = (response.grid ?? {}) as UnknownRecord;
  const sheetName = text(grid.sheetName);
  const rowStart = number(grid.rowStart, 1);
  const rowLimit = number(grid.rowLimit, GRID_ROWS);
  const columnStart = number(grid.columnStart, 1);
  const columnLimit = number(grid.columnLimit, GRID_COLUMNS);
  const maxRow = number(grid.maxRow);
  const maxColumn = number(grid.maxColumn);
  const cells = (grid.cells ?? []) as UnknownRecord[];
  const [selectedCell, setSelectedCell] = useState<UnknownRecord | null>(null);
  useEffect(() => setSelectedCell(null), [sheetName, rowStart, columnStart]);
  const cellMap = useMemo(() => new Map(cells.map((cell) => [`${number(cell.row_number)}:${number(cell.column_number)}`, cell])), [cells]);
  const endRow = Math.min(maxRow, rowStart + rowLimit - 1);
  const endColumn = Math.min(maxColumn, columnStart + columnLimit - 1);
  const rows = Array.from({ length: Math.max(0, endRow - rowStart + 1) }, (_, index) => rowStart + index);
  const columns = Array.from({ length: Math.max(0, endColumn - columnStart + 1) }, (_, index) => columnStart + index);
  if (!sheetName || maxRow === 0 || maxColumn === 0) return <p className="viewer-state">This parsed workbook has no inspectable worksheet cells.</p>;
  return <><div className="worksheet-toolbar"><label>Worksheet <select value={sheetName} onChange={(event) => onNavigate(event.target.value, 1, 1)}>{worksheets.map((sheet) => <option key={text(sheet.name)} value={text(sheet.name)}>{text(sheet.name)} ({number(sheet.nonEmptyCellCount)} non-empty)</option>)}</select></label><span>Rows {rowStart}–{endRow} of {maxRow}; columns {columnName(columnStart)}–{columnName(endColumn)}</span></div>
    <div className="spreadsheet-scroll"><table className="spreadsheet-grid"><thead><tr><th aria-label="Row number" />{columns.map((column) => <th key={column}>{columnName(column)}</th>)}</tr></thead><tbody>{rows.map((row) => <tr key={row}><th>{row}</th>{columns.map((column) => { const cell = cellMap.get(`${row}:${column}`); return <td key={column}><button type="button" className={cell ? "grid-cell populated" : "grid-cell"} onClick={() => setSelectedCell(cell ?? { row_number: row, column_number: column, source_reference: `${sheetName}!${columnName(column)}${row}`, is_empty: true })}>{cell ? text(cell.value_text) : ""}</button></td>; })}</tr>)}</tbody></table></div>
    <div className="grid-pagination"><button type="button" disabled={rowStart === 1} onClick={() => onNavigate(sheetName, Math.max(1, rowStart - rowLimit), columnStart)}>Previous rows</button><button type="button" disabled={endRow >= maxRow} onClick={() => onNavigate(sheetName, rowStart + rowLimit, columnStart)}>Next rows</button><button type="button" disabled={columnStart === 1} onClick={() => onNavigate(sheetName, rowStart, Math.max(1, columnStart - columnLimit))}>Previous columns</button><button type="button" disabled={endColumn >= maxColumn} onClick={() => onNavigate(sheetName, rowStart, columnStart + columnLimit)}>Next columns</button></div>
    {grid.previewTruncated === true && <p className="viewer-warning">Some unusually large cell values are abbreviated in this preview. The immutable source file and full extraction remain unchanged.</p>}
    {selectedCell && <aside className="cell-inspector"><strong>{text(selectedCell.source_reference)}</strong><span>Type: {text(selectedCell.value_type, "empty")}</span>{selectedCell.formula_text ? <><span>Formula</span><code>{text(selectedCell.formula_text)}</code>{selectedCell.formula_truncated === true && <small>Formula abbreviated for preview safety.</small>}</> : null}<span>Value</span><pre>{text(selectedCell.value_text, "(empty)")}</pre>{selectedCell.value_truncated === true && <small>Value abbreviated for preview safety.</small>}</aside>}
  </>;
}

function PdfPreview({ response, onNavigate }: { response: Extract<DocumentContentResponse, { ok: true }>; onNavigate: (pageNumber: number) => void }) {
  const [showRaw, setShowRaw] = useState(false);
  const page = (response.references as UnknownRecord[])[0];
  const pagination = (response.pagination ?? {}) as UnknownRecord;
  const pageNumber = number(pagination.pageNumber);
  const pageCount = number(pagination.pageCount);
  const navigation: PdfPagination = { pageNumber, pageCount, hasPrevious: pagination.hasPrevious === true, hasNext: pagination.hasNext === true, previousPageNumber: number(pagination.previousPageNumber) || undefined, nextPageNumber: number(pagination.nextPageNumber) || undefined };
  if (!page) return <p className="viewer-state">No extracted page is available.</p>;
  const blocks = (page.blocks ?? []) as UnknownRecord[];
  return <><div className="worksheet-toolbar"><span>Page {pageNumber} of {pageCount}</span><button type="button" onClick={() => setShowRaw(!showRaw)}>{showRaw ? "Show cleaned text" : "Show raw text"}</button></div><article className="preview-reference"><div className="preview-reference-heading"><strong>{text(page.extraction_method) === "ocr" ? "Local OCR" : "Native text"}</strong><span>Source {text(page.source_reference)}</span></div>{text(page.quality_warning) && <p className="preview-warning">{text(page.quality_warning)}</p>}<pre>{showRaw ? text(page.raw_text, text(page.value_text)) : text(page.value_text) || "(no extracted text)"}</pre></article>{blocks.length > 0 && <details className="pdf-blocks"><summary>Source text blocks and coordinates</summary>{blocks.map((block) => <p key={number(block.block_index)}><strong>Block {number(block.block_index)}</strong> ({number(block.x0).toFixed(1)}, {number(block.y0).toFixed(1)})–({number(block.x1).toFixed(1)}, {number(block.y1).toFixed(1)})<br />{text(block.text)}</p>)}</details>}<div className="grid-pagination"><button type="button" disabled={previousPdfPage(navigation) === null} onClick={() => { const previous = previousPdfPage(navigation); if (previous !== null) onNavigate(previous); }}>Previous page</button><button type="button" disabled={nextPdfPage(navigation) === null} onClick={() => { const next = nextPdfPage(navigation); if (next !== null) onNavigate(next); }}>Next page</button></div></>;
}

export default function DocumentsScreen() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [message, setMessage] = useState("Loading imported documents…");
  const [selected, setSelected] = useState<DocumentContentResponse | null>(null);
  const [activeDocument, setActiveDocument] = useState<DocumentSummary | null>(null);
  const [viewerState, setViewerState] = useState<ViewerState>("idle");
  const [viewerError, setViewerError] = useState("");
  const previewRequest = useRef(0);
  const refresh = async () => { const response = await window.desktopAPI.listDocuments(); if (response.ok) { setDocuments(response.documents); setMessage(response.documents.length ? "" : "No documents imported yet."); } else setMessage(response.error.message); };
  useEffect(() => { void refresh(); }, []);
  const showDocument = async (document: DocumentSummary, options: DocumentContentOptions = {}) => { const request = ++previewRequest.current; setActiveDocument(document); setSelected(null); setViewerState("loading"); setViewerError(""); const result = await loadDocumentPreview(window.desktopAPI, document.id, options); if (request !== previewRequest.current) return; if (result.state === "ready") { setSelected(result.response); setViewerState("ready"); } else { setViewerState("error"); setViewerError(result.message); } };
  const importDocuments = async () => { setMessage("Importing selected documents…"); const response = await window.desktopAPI.selectAndImportDocuments(); if (!response.ok) setMessage(response.error.message); else if (response.cancelled) setMessage("No documents selected."); else { setMessage(response.documents.some((item) => !item.ok) ? "Some documents could not be imported." : "Import complete."); await refresh(); } };
  const retryParsing = async (documentId: string) => { setMessage("Reprocessing the immutable local copy…"); const response = await window.desktopAPI.retryDocumentParsing(documentId); if (response.ok) { await refresh(); await showDocument(response.document); } else setMessage(response.error.message); };
  const success = selected?.ok ? selected : null;
  const document = success?.document ?? activeDocument;
  return <><header className="page-header"><div><p className="eyebrow">Documents</p><h1>Imported documents</h1></div></header><section className="documents-card"><p>Import local XLSX, XLS, CSV, or PDF files. Originals are copied unchanged into the local vault.</p><button className="primary-button" type="button" onClick={() => void importDocuments()}>Import Documents</button></section><section className="documents-card"><h2>Document library</h2><p className="storage-message" role="status">{message}</p>{documents.map((item) => <button className="document-row" type="button" key={item.id} onClick={() => void showDocument(item)}><span><strong>{item.original_filename}</strong><small>{item.file_format.toUpperCase()} · {formatBytes(item.file_size_bytes)} · {new Date(item.imported_at).toLocaleString()}</small></span><span className={`document-status ${item.processing_status}`}>{item.processing_status}</span></button>)}</section>{document && <section className="documents-card"><h2>{document.original_filename}</h2>{viewerState === "loading" && <p className="viewer-state" role="status">Loading extracted content…</p>}{viewerState === "error" && <p className="viewer-error" role="alert">{viewerError}</p>}{viewerState === "ready" && success && <><p className="support-note">{document.file_format.toUpperCase()} · Parser {success.parserVersion ?? "not parsed"}</p><ExtractionSummary extraction={success.extraction as UnknownRecord | undefined} />{(document.processing_status === "failed" || document.processing_status === "unsupported") && <button className="primary-button" type="button" onClick={() => void retryParsing(document.id)}>Reprocess document</button>}{success.contentType === "spreadsheet" && <SpreadsheetGrid response={success} onNavigate={(sheetName, rowStart, columnStart) => void showDocument(document, spreadsheetRequest(sheetName, rowStart, columnStart))} />}{success.contentType === "pdf" && <PdfPreview response={success} onNavigate={(pageNumber) => void showDocument(document, { pageNumber })} />}{!success.contentType && <p className="viewer-state">{document.processing_error ?? "No extracted content is available."}</p>}</>}</section>}</>;
}
