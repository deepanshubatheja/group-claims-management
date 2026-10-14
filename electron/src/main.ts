import { app, BrowserWindow, ipcMain, session } from "electron";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  BackendProcessError,
  BackendProcessManager,
  resolveDevelopmentBackendLaunch,
} from "./backend-process";
import {
  GET_APP_INFO_CHANNEL,
  GET_BACKEND_HEALTH_CHANNEL,
  GET_STORAGE_STATUS_CHANNEL,
  type AppInfo,
  type BackendHealthResponse,
  type StorageStatusResponse,
} from "./ipc-contract";

const developmentServerUrl = process.env.ELECTRON_RENDERER_URL;
const rendererHtmlPath = path.join(__dirname, "../../frontend/dist/index.html");
const rendererHtmlUrl = pathToFileURL(rendererHtmlPath).toString();
const projectRoot = path.join(__dirname, "../..");
let backendProcess: BackendProcessManager | undefined;

function isAllowedRendererUrl(url: string): boolean {
  try {
    if (developmentServerUrl !== undefined) {
      return new URL(url).origin === new URL(developmentServerUrl).origin;
    }

    return url === rendererHtmlUrl;
  } catch {
    return false;
  }
}

function createAppInfo(): AppInfo {
  const appInfo: AppInfo = {
    name: app.getName(),
    version: app.getVersion(),
    platform: process.platform,
  };

  if (
    appInfo.name.length === 0 ||
    appInfo.version.length === 0 ||
    !["darwin", "win32", "linux"].includes(appInfo.platform)
  ) {
    throw new Error("Unable to provide validated application information.");
  }

  return Object.freeze(appInfo);
}

function createContentSecurityPolicy(): string {
  // Vite's React Fast Refresh preamble is an inline module in development.
  // Production bundles do not need this exception.
  const scriptSource =
    developmentServerUrl !== undefined
      ? "script-src 'self' 'unsafe-inline'"
      : "script-src 'self'";
  const styleSource =
    developmentServerUrl !== undefined
      ? "style-src 'self' 'unsafe-inline'"
      : "style-src 'self'";
  const directives = [
    "default-src 'self'",
    scriptSource,
    styleSource,
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-src 'none'",
  ];

  if (developmentServerUrl !== undefined) {
    const server = new URL(developmentServerUrl);
    const websocketProtocol = server.protocol === "https:" ? "wss:" : "ws:";
    const websocketUrl = `${websocketProtocol}//${server.host}`;
    directives[5] = `connect-src 'self' ${server.origin} ${websocketUrl}`;
  }

  return directives.join("; ");
}

function configureSessionSecurity(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });

  session.defaultSession.setPermissionCheckHandler(() => false);

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.resourceType !== "mainFrame") {
      callback({ cancel: false });
      return;
    }

    callback({
      cancel: false,
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [createContentSecurityPolicy()],
      },
    });
  });
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    show: false,
    backgroundColor: "#f6f8fb",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
    },
  });

  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedRendererUrl(url)) {
      event.preventDefault();
    }
  });

  window.once("ready-to-show", () => window.show());

  if (developmentServerUrl !== undefined) {
    void window.loadURL(developmentServerUrl);
  } else {
    void window.loadFile(rendererHtmlPath);
  }

  return window;
}

function createBackendHealthFailure(
  error: unknown,
): { ok: false; error: { code: string; message: string } } {
  if (error instanceof BackendProcessError) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
      },
    };
  }

  return {
    ok: false,
    error: {
      code: "BACKEND_UNAVAILABLE",
      message: "The backend is unavailable.",
    },
  };
}

function startDevelopmentBackend(storageRoot: string): void {
  if (app.isPackaged) {
    // Packaging a standalone backend executable is a later release milestone.
    return;
  }

  try {
    backendProcess = new BackendProcessManager({
      launch: resolveDevelopmentBackendLaunch(projectRoot, storageRoot),
      onDiagnostic: (message) => console.error(`Backend process: ${message}`),
    });
    void backendProcess.start().catch(() => undefined);
  } catch (error: unknown) {
    console.error("Backend process could not be configured.");
  }
}

// Called on explicit renderer health requests (initial load or user retry): restarts a
// failed backend once per request, and retries configuration if the venv was missing.
async function ensureDevelopmentBackend(): Promise<BackendProcessManager | undefined> {
  if (backendProcess === undefined) {
    startDevelopmentBackend(app.getPath("userData"));
  }

  if (backendProcess?.state === "failed") {
    await backendProcess.start();
  }

  return backendProcess;
}

app.setName("Group Claims Management");

app.whenReady().then(() => {
  configureSessionSecurity();
  startDevelopmentBackend(app.getPath("userData"));

  ipcMain.handle(GET_APP_INFO_CHANNEL, (event, ...arguments_: unknown[]) => {
    const senderFrame = event.senderFrame;

    if (
      arguments_.length !== 0 ||
      senderFrame === null ||
      !isAllowedRendererUrl(senderFrame.url)
    ) {
      throw new Error("Invalid application information request.");
    }

    return createAppInfo();
  });

  ipcMain.handle(
    GET_BACKEND_HEALTH_CHANNEL,
    async (event, ...arguments_: unknown[]): Promise<BackendHealthResponse> => {
      const senderFrame = event.senderFrame;
      if (
        arguments_.length !== 0 ||
        senderFrame === null ||
        !isAllowedRendererUrl(senderFrame.url)
      ) {
        throw new Error("Invalid backend health request.");
      }

      try {
        const backend = await ensureDevelopmentBackend();
        if (backend === undefined) {
          return createBackendHealthFailure(
            new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."),
          );
        }

        return { ok: true, health: await backend.getHealth() };
      } catch (error: unknown) {
        return createBackendHealthFailure(error);
      }
    },
  );

  ipcMain.handle(
    GET_STORAGE_STATUS_CHANNEL,
    async (event, ...arguments_: unknown[]): Promise<StorageStatusResponse> => {
      const senderFrame = event.senderFrame;
      if (
        arguments_.length !== 0 ||
        senderFrame === null ||
        !isAllowedRendererUrl(senderFrame.url)
      ) {
        throw new Error("Invalid storage status request.");
      }

      if (backendProcess === undefined) {
        return createBackendHealthFailure(
          new BackendProcessError("BACKEND_UNAVAILABLE", "The backend is unavailable."),
        );
      }

      try {
        return { ok: true, storage: await backendProcess.getStorageStatus() };
      } catch (error: unknown) {
        return createBackendHealthFailure(error);
      }
    },
  );

  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  backendProcess?.stop();
});
