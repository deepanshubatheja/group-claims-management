import { access, lstat, readdir } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import path from "node:path";

export const MAX_FOLDER_DEPTH = 12;
export const MAX_FOLDER_FILES = 200;
export const MAX_FOLDER_BYTES = 500 * 1024 * 1024;
export const MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024;
export const MAX_DROPPED_IMPORT_ROOTS = 20;
const formats = new Set([".xlsx", ".xls", ".csv", ".pdf"]);
export interface AuthorizedFolderFile { path: string; relativePath: string; size: number; }
export interface FolderDiscovery { files: AuthorizedFolderFile[]; skippedFiles: number; skippedLinks: number; limitReached: boolean; totalBytes: number; }
export async function discoverImportableFiles(root: string): Promise<FolderDiscovery> {
  const rootStat = await lstat(root);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) throw new Error("The selected folder is unavailable.");
  const result: FolderDiscovery = { files: [], skippedFiles: 0, skippedLinks: 0, limitReached: false, totalBytes: 0 };
  const visit = async (folder: string, depth: number): Promise<void> => {
    if (depth > MAX_FOLDER_DEPTH || result.limitReached) { result.limitReached = true; return; }
    let entries; try { entries = await readdir(folder, { withFileTypes: true }); } catch { result.skippedFiles += 1; return; }
    for (const entry of entries) { if (result.limitReached) return; const target = path.join(folder, entry.name); let info; try { info = await lstat(target); } catch { result.skippedFiles += 1; continue; } if (info.isSymbolicLink()) { result.skippedLinks += 1; continue; } if (info.isDirectory()) { await visit(target, depth + 1); continue; } if (!info.isFile() || !formats.has(path.extname(entry.name).toLowerCase()) || info.size > MAX_IMPORT_FILE_BYTES) { result.skippedFiles += 1; continue; } try { await access(target, fsConstants.R_OK); } catch { result.skippedFiles += 1; continue; } if (result.files.length >= MAX_FOLDER_FILES || result.totalBytes + info.size > MAX_FOLDER_BYTES) { result.limitReached = true; return; } result.files.push({ path: target, relativePath: path.relative(root, target), size: info.size }); result.totalBytes += info.size; }
  };
  await visit(root, 0); return result;
}

/**
 * Builds one bounded, main-process-only import plan for files and folders dropped
 * together.  The renderer receives only the aggregate preview; native paths never
 * cross back into the renderer.
 */
export async function discoverDroppedImportableFiles(roots: string[]): Promise<FolderDiscovery> {
  if (roots.length < 1 || roots.length > MAX_DROPPED_IMPORT_ROOTS || !roots.every((root) => path.isAbsolute(root))) {
    throw new Error("Invalid dropped import.");
  }

  const result: FolderDiscovery = { files: [], skippedFiles: 0, skippedLinks: 0, limitReached: false, totalBytes: 0 };
  const seenFiles = new Set<string>();
  const addFile = (file: AuthorizedFolderFile): void => {
    const identity = path.resolve(file.path);
    if (seenFiles.has(identity)) return;
    if (result.files.length >= MAX_FOLDER_FILES || result.totalBytes + file.size > MAX_FOLDER_BYTES) {
      result.limitReached = true;
      return;
    }
    seenFiles.add(identity);
    result.files.push(file);
    result.totalBytes += file.size;
  };

  for (const root of [...new Set(roots.map((value) => path.resolve(value)))]) {
    if (result.limitReached) break;
    let details;
    try { details = await lstat(root); } catch { result.skippedFiles += 1; continue; }
    if (details.isSymbolicLink()) { result.skippedLinks += 1; continue; }
    if (details.isDirectory()) {
      let discovery: FolderDiscovery;
      try { discovery = await discoverImportableFiles(root); } catch { result.skippedFiles += 1; continue; }
      result.skippedFiles += discovery.skippedFiles;
      result.skippedLinks += discovery.skippedLinks;
      for (const file of discovery.files) {
        addFile({ ...file, relativePath: path.join(path.basename(root), file.relativePath) });
        if (result.limitReached) break;
      }
      result.limitReached ||= discovery.limitReached;
      continue;
    }
    const extension = path.extname(root).toLowerCase();
    if (!details.isFile() || !formats.has(extension) || details.size > MAX_IMPORT_FILE_BYTES) { result.skippedFiles += 1; continue; }
    try { await access(root, fsConstants.R_OK); } catch { result.skippedFiles += 1; continue; }
    addFile({ path: root, relativePath: path.basename(root), size: details.size });
  }
  return result;
}
