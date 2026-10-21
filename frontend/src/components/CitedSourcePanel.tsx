import { useEffect, useRef, useState } from "react";
import { loadDocumentPreview } from "./document-viewer-client";
import { evidenceTarget } from "./document-viewer-navigation";
import { sourceLocationRequest } from "./matter-scope";

type GridCell = { row_number: number; column_number: number; value_text: string | null; source_reference: string };
type PdfBlock = { block_index: number; text: string };

interface CitedSourcePanelProps { documentId: string; sourceReference: string; onClose(): void; }

/** Shows the exact stored cell or PDF block a Matter Review answer cited. */
export default function CitedSourcePanel({ documentId, sourceReference, onClose }: CitedSourcePanelProps) {
  const [state, setState] = useState<{ status: "loading" | "ready" | "error"; message?: string; cells?: GridCell[]; blocks?: PdfBlock[]; pageText?: string }>({ status: "loading" });
  const highlighted = useRef<HTMLElement | null>(null);
  const target = evidenceTarget(sourceReference);

  useEffect(() => {
    const request = sourceLocationRequest(sourceReference);
    if (request === null) { setState({ status: "error", message: "This cited location cannot be shown in the viewer." }); return; }
    let current = true;
    setState({ status: "loading" });
    void loadDocumentPreview(window.desktopAPI, documentId, request).then((result) => {
      if (!current) return;
      if (result.state === "error") { setState({ status: "error", message: result.message }); return; }
      const { response } = result;
      if (response.contentType === "spreadsheet") {
        const grid = response.grid as { cells?: GridCell[] } | undefined;
        setState({ status: "ready", cells: grid?.cells ?? [] });
      } else {
        const page = response.references[0] as { blocks?: PdfBlock[]; value_text?: string } | undefined;
        setState({ status: "ready", blocks: page?.blocks ?? [], pageText: page?.value_text ?? "" });
      }
    });
    return () => { current = false; };
  }, [documentId, sourceReference]);

  useEffect(() => { if (state.status === "ready") highlighted.current?.scrollIntoView({ block: "nearest" }); }, [state.status]);

  const rows = [...new Set((state.cells ?? []).map((cell) => cell.row_number))].sort((first, second) => first - second);
  const columns = [...new Set((state.cells ?? []).map((cell) => cell.column_number))].sort((first, second) => first - second);
  const cellAt = (row: number, column: number) => state.cells?.find((cell) => cell.row_number === row && cell.column_number === column);
  const isTargetCell = (row: number, column: number) => target?.kind === "spreadsheet" && target.rowNumber === row && target.columnNumber === column;

  return <section className="cited-source" aria-label="Cited source">
    <div className="cited-source-heading"><div><p className="section-label">Cited source</p><strong>{sourceReference}</strong></div><button className="text-button" type="button" onClick={onClose}>Close</button></div>
    {state.status === "loading" && <p className="viewer-state" role="status">Loading the cited location…</p>}
    {state.status === "error" && <p className="viewer-warning" role="alert">{state.message}</p>}
    {state.status === "ready" && state.cells && (rows.length === 0
      ? <p className="viewer-warning">The cited cell is not present in the current extraction.</p>
      : <div className="cited-source-grid"><table><tbody>{rows.map((row) => <tr key={row}><th scope="row">{row}</th>{columns.map((column) => {
        const cell = cellAt(row, column);
        const isTarget = isTargetCell(row, column);
        return <td key={column} className={isTarget ? "cited-target" : undefined} ref={isTarget ? (element) => { highlighted.current = element; } : undefined}>{cell?.value_text ?? ""}</td>;
      })}</tr>)}</tbody></table></div>)}
    {state.status === "ready" && state.blocks && (state.blocks.length === 0
      ? <p className="cited-source-text">{state.pageText || "No extracted text is available for this page."}</p>
      : <div className="cited-source-blocks">{state.blocks.map((block) => {
        const isTarget = target?.kind === "pdf" && target.blockIndex === block.block_index;
        return <p key={block.block_index} className={isTarget ? "cited-target" : undefined} ref={isTarget ? (element) => { highlighted.current = element; } : undefined}>{block.text}</p>;
      })}</div>)}
  </section>;
}
