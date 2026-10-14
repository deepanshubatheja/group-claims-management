export type PreviewLoadResult =
  | { state: "ready"; response: Extract<DocumentContentResponse, { ok: true }> }
  | { state: "error"; message: string };

export interface DocumentPreviewClient {
  getDocumentContent(documentId: string, options?: DocumentContentOptions): Promise<DocumentContentResponse>;
}

/**
 * Keeps renderer preview loading testable without broadening the preload API.
 * The caller still supplies only the typed, allowlisted desktop bridge.
 */
export async function loadDocumentPreview(
  client: DocumentPreviewClient,
  documentId: string,
  options: DocumentContentOptions = {},
): Promise<PreviewLoadResult> {
  try {
    const response = await client.getDocumentContent(documentId, options);
    if (response.ok) return { state: "ready", response };
    return { state: "error", message: response.error.message };
  } catch {
    return { state: "error", message: "The document preview could not be loaded." };
  }
}
