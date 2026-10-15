const { execFileSync } = require("node:child_process");
const path = require("node:path");

/**
 * Electron's distributed macOS app bundle is modified when extra resources
 * are added. With no Developer ID certificate available for this beta, apply
 * an ad-hoc signature so macOS can execute the bundled backend and renderer.
 * This is intentionally not Developer-ID signing or notarization.
 */
exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }

  const appPath = path.join(
    context.appOutDir,
    `${context.packager.appInfo.productFilename}.app`,
  );
  execFileSync(
    "codesign",
    ["--force", "--deep", "--sign", "-", "--timestamp=none", appPath],
    { stdio: "inherit" },
  );
};
