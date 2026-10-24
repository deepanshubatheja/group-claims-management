import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [workbookPath, outputDir] = process.argv.slice(2);
if (!workbookPath || !outputDir) throw new Error("Usage: render_workbook.mjs <workbook.xlsx> <output-dir>");
await fs.mkdir(outputDir, { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(workbookPath));
const sheets = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
console.log(sheets.ndjson);
for (const sheet of workbook.worksheets.items) {
  const preview = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(outputDir, `${sheet.name.replaceAll(/[^A-Za-z0-9]+/g, "_")}.png`), new Uint8Array(await preview.arrayBuffer()));
}
