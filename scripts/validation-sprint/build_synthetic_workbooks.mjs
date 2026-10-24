import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const outputDir = process.argv[2];
if (!outputDir) {
  throw new Error("Usage: node build_synthetic_workbooks.mjs <output-directory>");
}

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDirectory, "../..");
const virtualEnvironmentPython = process.platform === "win32"
  ? path.join(projectRoot, "backend", ".venv", "Scripts", "python.exe")
  : path.join(projectRoot, "backend", ".venv", "bin", "python");
const python = process.env.PYTHON || (fs.existsSync(virtualEnvironmentPython)
  ? virtualEnvironmentPython
  : process.platform === "win32" ? "python" : "python3");
const result = spawnSync(
  python,
  [path.join(scriptDirectory, "build_synthetic_workbooks.py"), outputDir],
  { stdio: "inherit" },
);

if (result.error) {
  throw result.error;
}
process.exitCode = result.status ?? 1;
