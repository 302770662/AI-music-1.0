const BROWSER_API = "http://127.0.0.1:8765";
const DOWNLOAD_API = "http://127.0.0.1:8766";
const SUNO_ORIGIN_RE = /^https:\/\/(?:www\.)?suno\.com\//i;

const ports = new Map();
// Track which Suno tabs have a live Create form. Multiple /create tabs can be
// open at once; URL alone is not enough to select the tab that can execute.
const runnerReadiness = new Map();
let generationStatus = null;
let downloadStatus = null;
let generationDispatch = null;
let downloadDispatch = null;
let downloadContext = null;
let pollInFlight = false;
// Keep generation and download in separate visible tabs.  Suno navigates
// from /create to /song/<id> after Create; sharing one tab makes the two
// workers interrupt each other.
let generationTabId = null;
let downloadTabId = null;
let downloadTabOwned = false;

function isSunoUrl(url) {
  return SUNO_ORIGIN_RE.test(String(url || ""));
}

function now() {
  return Date.now();
}

function safeText(value) {
  return String(value == null ? "" : value);
}

async function requestJson(api, path, options = {}) {
  const response = await fetch(`${api}${path}`, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `worker HTTP ${response.status}`);
  return payload;
}

function workerApiForPath(path) {
  return String(path || "").startsWith("/v1/download/") ? DOWNLOAD_API : BROWSER_API;
}

async function postWorker(path, payload) {
  return requestJson(workerApiForPath(path), path, {
    method: "POST",
    body: JSON.stringify(payload || {}),
  });
}

function resetDispatch(mode, taskId = null) {
  if (mode === "download") {
    if (!taskId || !downloadDispatch || downloadDispatch.taskId === taskId) downloadDispatch = null;
  } else if (!taskId || !generationDispatch || generationDispatch.taskId === taskId) {
    generationDispatch = null;
  }
}

function portForTab(tabId) {
  const port = ports.get(Number(tabId));
  return port || null;
}

function readyPortTabIds() {
  return new Set(Array.from(runnerReadiness.entries())
    .filter(([, value]) => value && value.ready && now() - Number(value.at || 0) < 30000)
    .map(([tabId]) => Number(tabId)));
}

function postToTab(tabId, message) {
  const port = portForTab(tabId);
  if (!port) return false;
  try {
    port.postMessage(message);
    return true;
  } catch (_error) {
    ports.delete(Number(tabId));
    return false;
  }
}

async function visibleSunoTab(mode = "generation", command = null) {
  const tabs = await chrome.tabs.query({});
  const songUrl = mode === "download" && command && command.job && command.job.song_url
    ? String(command.job.song_url).replace(/\/$/, "") : "";
  const isGeneration = mode !== "download";
  let tabId = isGeneration ? generationTabId : downloadTabId;
  let tab = tabs.find(item => Number(item.id) === Number(tabId)) || null;
  // A remembered generation tab can become stale or remain an empty /create
  // page after Suno remounts the form. Never keep routing work to it merely
  // because the tab id is still valid; it must still be a connected, ready
  // Create form. Otherwise select a different ready tab below.
  if (isGeneration && tab) {
    const tabReady = readyPortTabIds().has(Number(tab.id));
    const isCreate = /\/create(?:[/?#]|$)/i.test(String(tab.url || ""));
    if (!isCreate || !portForTab(tab.id) || !tabReady || Number(tab.id) === Number(downloadTabId)) {
      tab = null;
      generationTabId = null;
    }
  }
  if (!tab) {
    const candidates = tabs.filter(item => isSunoUrl(item.url));
    if (isGeneration) {
      const createCandidates = candidates.filter(item => Number(item.id) !== Number(downloadTabId) &&
        /\/create(?:[/?#]|$)/i.test(String(item.url || "")));
      const ready = readyPortTabIds();
      // Reuse the visible/connected Create page first. The dashboard no
      // longer opens a second page, so one generation tab remains stable even
      // when an older Create tab is still open.
      tab = createCandidates.find(item => item.active && portForTab(item.id) && ready.has(Number(item.id))) ||
        createCandidates.find(item => portForTab(item.id) && ready.has(Number(item.id))) ||
        createCandidates.find(item => item.active && portForTab(item.id)) ||
        createCandidates.find(item => portForTab(item.id)) ||
        createCandidates.sort((left, right) => Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0))[0] || null;
    } else {
      tab = candidates.find(item => Number(item.id) !== Number(generationTabId) &&
        /\/song\//i.test(String(item.url || ""))) || null;
    }
  }
  const target = isGeneration ? "https://suno.com/create" : songUrl;
  if (!tab) {
    if (!target) return null;
    try { tab = await chrome.tabs.create({ url: target, active: false }); }
    catch (_error) { return null; }
    if (!isGeneration) downloadTabOwned = true;
  } else if (target && String(tab.url || "").replace(/\/$/, "") !== target && !isGeneration) {
    // Do not navigate an existing download page to another song.  The old
    // page may still contain a stale menu/download context, which caused the
    // next queue item to download from the previous song.  Each new song gets
    // a fresh visible Suno page; MP3 -> Video for the same song still reuses
    // the page because the target URL is identical.
    const previousTabId = Number(tab.id);
    const previousOwned = downloadTabOwned;
    try {
      tab = await chrome.tabs.create({ url: target, active: false });
      downloadTabOwned = true;
      if (previousOwned && previousTabId && previousTabId !== Number(tab.id)) {
        chrome.tabs.remove(previousTabId).catch(() => {});
      }
    } catch (_error) { return tab; }
  } else if (target && String(tab.url || "").replace(/\/$/, "") !== target && isGeneration &&
             !/\/create(?:[/?#]|$)/i.test(String(tab.url || ""))) {
    try { await chrome.tabs.update(tab.id, { url: target, active: false }); return null; }
    catch (_error) { return tab; }
  }
  if (isGeneration && Number(tab.id) === Number(downloadTabId)) return null;
  if (!isGeneration && Number(tab.id) === Number(generationTabId)) return null;
  if (isGeneration) generationTabId = Number(tab.id);
  else downloadTabId = Number(tab.id);
  return tab;
}

function commandSignature(command) {
  const job = command && command.job ? command.job : {};
  return [
    command && command.action,
    command && command.task_id,
    command && command.queue_path,
    job.task_id,
    job.download_type,
    command && command.auto_create,
    command && command.auto_download,
  ].map(safeText).join("|");
}

async function dispatch(mode, command) {
  if (!command || command.action === "idle") return;
  const tab = await visibleSunoTab(mode, command);
  if (!tab || !tab.id) return;
  const port = portForTab(tab.id);
  if (!port) return;
  const signature = commandSignature(command);
  const previous = mode === "download" ? downloadDispatch : generationDispatch;
  // A content-script message can be lost during Suno navigation or an
  // extension reconnect. Retry the same command after a short lease instead
  // of leaving the local queue permanently stuck in claimed/running.
  const leaseMs = mode === "download" ? 30 * 60 * 1000 : 5000;
  if (previous && previous.tabId === tab.id && previous.signature === signature && now() - previous.at < leaseMs) return;
  const messageType = mode === "download" ? "suno-download-command" : "suno-command";
  if (!postToTab(tab.id, { type: messageType, command })) return;
  const record = { tabId: tab.id, taskId: command.task_id, signature, at: now() };
  if (mode === "download") downloadDispatch = record;
  else generationDispatch = record;
}

function generationBusy() {
  return Boolean(generationStatus && generationStatus.active);
}

async function pollWorkers() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const values = await Promise.allSettled([
      requestJson(BROWSER_API, "/v1/browser/status"),
      requestJson(DOWNLOAD_API, "/v1/download/status"),
    ]);
    generationStatus = values[0].status === "fulfilled" ? values[0].value : null;
    downloadStatus = values[1].status === "fulfilled" ? values[1].value : null;

    await reconcileDownloadContext();

    // Generation and downloading use separate visible tabs. Dispatch both
    // independently: a long generation wait must not prevent the download
    // listener from claiming already-finished songs, and a download menu
    // wait must not stall the next generation command.
    const dispatches = [];
    if (generationBusy()) {
      const command = await requestJson(BROWSER_API, "/v1/browser/command").catch(() => null);
      dispatches.push(dispatch("generation", command));
    }
    if (downloadStatus && (downloadStatus.active || downloadStatus.current)) {
      const command = await requestJson(DOWNLOAD_API, "/v1/download/command").catch(() => null);
      dispatches.push(dispatch("download", command));
    }
    await Promise.allSettled(dispatches);
  } finally {
    pollInFlight = false;
  }
}

function installPolling() {
  if (!installPolling.timer) {
    installPolling.timer = setInterval(() => pollWorkers().catch(() => {}), 1500);
  }
  chrome.alarms.create("hotspot-suno-poll", { periodInMinutes: 0.5 });
  pollWorkers().catch(() => {});
}

installPolling();

chrome.runtime.onInstalled.addListener(installPolling);
chrome.runtime.onStartup.addListener(installPolling);
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "hotspot-suno-poll") pollWorkers().catch(() => {});
});

chrome.runtime.onConnect.addListener(port => {
  const tabId = port.sender && port.sender.tab ? port.sender.tab.id : null;
  if (tabId == null) return;
  ports.set(Number(tabId), port);
  runnerReadiness.set(Number(tabId), { ready: false, at: now(), url: "" });
  resetDispatch("generation");
  // Keep any in-flight download lease across a content-script reconnect.
  port.onMessage.addListener(message => {
    if (!message || typeof message !== "object") return;
    if (message.type === "worker-post" && message.path) {
      postWorker(message.path, message.payload || {}).catch(() => {});
      if (String(message.path).startsWith("/v1/browser/result")) resetDispatch("generation", message.payload && message.payload.task_id);
      if (String(message.path).startsWith("/v1/download/result") ||
          String(message.path).startsWith("/v1/download/error")) {
        resetDispatch("download", message.payload && message.payload.task_id);
      }
      return;
    }
    if (message.type === "runner-ready") {
      runnerReadiness.set(Number(tabId), {
        ready: Boolean(message.ready),
        at: now(),
        url: safeText(message.url),
        runner_version: safeText(message.runner_version),
      });
      return;
    }
    if (message.type === "download-intent" && message.payload) {
      const payload = message.payload;
      if (!payload.task_id || !payload.queue_path || !["mp3", "video"].includes(payload.download_type)) return;
      downloadContext = {
        task_id: String(payload.task_id),
        queue_path: String(payload.queue_path),
        download_type: String(payload.download_type),
        tabId: Number(tabId),
        song_id: String(payload.song_id || ""),
        intentAt: now(),
        downloadId: null,
        missingSince: null,
      };
    }
  });
  port.onDisconnect.addListener(() => {
    if (ports.get(Number(tabId)) === port) ports.delete(Number(tabId));
    runnerReadiness.delete(Number(tabId));
    if (generationDispatch && generationDispatch.tabId === Number(tabId)) generationDispatch = null;
    // A page reconnect must not re-dispatch the same visible download.
  });
  installPolling();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" || changeInfo.url) {
    if (generationDispatch && generationDispatch.tabId === tabId) generationDispatch = null;
    // A download command navigates the dedicated tab to the current song.
    // The content script returns immediately after starting that navigation;
    // retaining the old lease would suppress the same command after the new
    // page reconnects, leaving the queue stuck on the previous song.
    if (downloadDispatch && downloadDispatch.tabId === tabId) downloadDispatch = null;
    if (changeInfo.url || changeInfo.status === "loading") runnerReadiness.delete(Number(tabId));
  }
});

chrome.tabs.onRemoved.addListener(tabId => {
  ports.delete(Number(tabId));
  runnerReadiness.delete(Number(tabId));
  if (generationDispatch && generationDispatch.tabId === tabId) generationDispatch = null;
  if (downloadDispatch && downloadDispatch.tabId === tabId) downloadDispatch = null;
  if (downloadContext && downloadContext.tabId === tabId) downloadContext = null;
  if (generationTabId === Number(tabId)) generationTabId = null;
  if (downloadTabId === Number(tabId)) {
    downloadTabId = null;
    downloadTabOwned = false;
  }
});

function downloadSourceMatches(item, context) {
  if (!item || !context) return false;
  if (context.downloadId != null && Number(context.downloadId) !== Number(item.id)) return false;
  if (now() - Number(context.intentAt || 0) > 30 * 60 * 1000) return false;
  const url = safeText(item.url);
  const referrer = safeText(item.referrer);
  const knownSunoSource = isSunoUrl(url) || isSunoUrl(referrer) || url.startsWith("blob:");
  if (knownSunoSource) return true;
  // Some Chrome builds expose only the CDN URL and omit the referrer.  The
  // intent is emitted immediately before the visible menu click, so an
  // expected audio/video file created in that short window is still safely
  // bindable to this single current queue item.
  const filename = safeText(item.filename).toLowerCase();
  const mime = safeText(item.mime).toLowerCase();
  const expectedType = context.download_type === "mp3"
    ? filename.endsWith(".mp3") || filename.endsWith(".mp3.crdownload") || mime.includes("audio")
    : filename.endsWith(".mp4") || filename.endsWith(".mp4.crdownload") || mime.includes("video");
  return expectedType && now() - Number(context.intentAt || 0) <= 5000;
}

function downloadExtensionMatches(item, context) {
  if (!item || !context) return false;
  // A persisted download_id is an exact Chrome binding created immediately
  // before the visible menu click. It remains valid across a service-worker
  // restart, even when the original intent timestamp is old.
  if (context.downloadId != null) {
    if (Number(context.downloadId) !== Number(item.id)) return false;
  } else if (!downloadSourceMatches(item, context)) {
    return false;
  }
  const filename = safeText(item.filename).toLowerCase();
  const mime = safeText(item.mime).toLowerCase();
  // Suno/Chrome may expose a completed file with a generated `.tmp` suffix.
  // The worker performs the authoritative magic-byte check, so accept this
  // temporary suffix only for the already-bound download.
  const temporary = filename.endsWith(".tmp") || filename.endsWith(".crdownload");
  const isMp3 = context.download_type === "mp3" && (filename.endsWith(".mp3") || mime.includes("audio") || temporary);
  const isVideo = context.download_type === "video" && (filename.endsWith(".mp4") || mime.includes("video") || temporary);
  if (!isMp3 && !isVideo) return false;
  return true;
}

async function findDownload(downloadId) {
  const values = await chrome.downloads.search({ id: Number(downloadId) });
  return values && values.length ? values[0] : null;
}

async function waitForDownloadRecord(downloadId, timeoutMs = 15000) {
  const deadline = now() + timeoutMs;
  let item = null;
  while (now() < deadline) {
    item = await findDownload(downloadId).catch(() => null);
    if (item && safeText(item.filename).trim()) return item;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return item;
}

async function reconcileDownloadContext() {
  const current = downloadStatus && downloadStatus.current;
  const job = current && current.job;
  if (!job || !job.task_id) return;
  if (!downloadContext || downloadContext.task_id !== String(job.task_id)) {
    if (!job.download_id) return;
    downloadContext = {
      task_id: String(job.task_id),
      queue_path: String(downloadStatus.current.queue_path || ""),
      download_type: String(job.download_type || ""),
      tabId: downloadDispatch ? downloadDispatch.tabId : null,
      song_id: String(job.song_id || ""),
      intentAt: now(),
      downloadId: Number(job.download_id),
      missingSince: null,
    };
  } else if (job.download_id && downloadContext.downloadId == null) {
    downloadContext.downloadId = Number(job.download_id);
  }
  if (downloadContext && downloadContext.downloadId != null) {
    const item = await findDownload(downloadContext.downloadId).catch(() => null);
    if (!item) {
      // A final Suno Download click should create a Chrome download record
      // quickly. If that record never appears, leaving the item in
      // download_started permanently blocks every later queue item. Allow a
      // short propagation window, then requeue this item once and release
      // the lease. The worker still requires a real MP3/MP4 file before
      // marking anything complete.
      downloadContext.missingSince = downloadContext.missingSince || now();
      if (now() - downloadContext.missingSince >= 45000) {
        await reportDownloadError("Chrome 未找到对应的下载记录，已释放当前任务", "requeue");
      }
      return;
    }
    downloadContext.missingSince = null;
    if (item.state === "complete") await finishDownload(item);
    // Suno occasionally leaves the Chrome entry in `in_progress` while the
    // completed media has a generated `.tmp` filename.  When Chrome reports
    // that every byte has arrived, hand the local file to the worker.  The
    // worker still performs the mandatory MP3/MP4 magic-byte validation
    // before copying it into either archive, so this never accepts a cover
    // image or an incomplete payload merely because its filename is `.tmp`.
    else if (item.state === "in_progress" && Number(item.totalBytes) > 0 &&
             Number(item.bytesReceived) === Number(item.totalBytes)) {
      await finishDownload(item);
    }
    else if (item.state === "interrupted") await reportDownloadError(`Chrome 下载中断: ${item.error || "UNKNOWN"}`, item.error === "USER_CANCELED" ? "requeue" : "block");
  }
}

function basename(value) {
  return safeText(value).split(/[\\/]/).pop() || "download";
}

function notifyDownloadTab(type, payload) {
  if (!downloadContext) return;
  postToTab(downloadContext.tabId, { type, payload });
}

async function reportDownloadError(reason, action = "block") {
  const context = downloadContext;
  if (!context) return;
  try {
    await postWorker("/v1/download/error", {
      task_id: context.task_id,
      queue_path: context.queue_path,
      action,
      reason,
    });
  } catch (_error) {
    // The worker status remains the source of truth; a later poll will expose
    // the still-running item if the local report could not be delivered.
  }
  notifyDownloadTab("suno-download-error", { task_id: context.task_id, reason, action });
  resetDispatch("download", context.task_id);
  downloadContext = null;
}

async function finishDownload(item) {
  const context = downloadContext;
  if (!context || !downloadExtensionMatches(item, context)) return;
  // Chrome can emit the terminal state before its local filename is exposed
  // to the extension. Wait briefly instead of sending an empty path to the
  // worker and permanently blocking an otherwise valid download.
  const resolved = await waitForDownloadRecord(item.id);
  if (resolved) item = resolved;
  const payload = {
    task_id: context.task_id,
    queue_path: context.queue_path,
    download_id: String(item.id),
    filename: basename(item.filename),
    file_path: item.filename,
    bytes_received: Number(item.bytesReceived || 0),
  };
  try {
    const response = await postWorker("/v1/download/result", payload);
    notifyDownloadTab("suno-download-complete", {
      task_id: context.task_id,
      download_id: String(item.id),
      filename: payload.filename,
      file_path: payload.file_path,
      bytes_received: payload.bytes_received,
      response,
    });
    resetDispatch("download", context.task_id);
    downloadContext = null;
    // Do not wait for the next periodic poll after a terminal browser
    // receipt. The worker has already released this item; immediately ask
    // for the next queue entry so the dedicated tab can navigate to the next
    // song without a visible idle gap.
    setTimeout(() => pollWorkers().catch(() => {}), 0);
  } catch (error) {
    const message = `下载文件已完成，但本地保存校验失败: ${error.message || error}`;
    const retryable = /浏览器下载文件不存在|无法读取浏览器下载文件|浏览器下载文件为空/.test(message);
    await reportDownloadError(message, retryable ? "requeue" : "block");
  }
}

chrome.downloads.onCreated.addListener(item => {
  // Chrome may expose a temporary .crdownload filename at creation time, so
  // bind by the just-issued Suno source first and validate MP3/MP4 on finish.
  if (!downloadSourceMatches(item, downloadContext)) return;
  downloadContext.downloadId = Number(item.id);
  postWorker("/v1/download/phase", {
    task_id: downloadContext.task_id,
    queue_path: downloadContext.queue_path,
    phase: "download_started",
    download_id: String(item.id),
  }).catch(() => {});
});

chrome.downloads.onChanged.addListener(async delta => {
  if (!downloadContext || downloadContext.downloadId == null) return;
  if (Number(delta.id) !== Number(downloadContext.downloadId)) return;
  if (delta.state && delta.state.current === "complete") {
    const item = await findDownload(delta.id);
    if (item) await finishDownload(item);
  } else if (delta.state && delta.state.current === "in_progress") {
    const item = await findDownload(delta.id);
    if (item && Number(item.totalBytes) > 0 &&
        Number(item.bytesReceived) === Number(item.totalBytes)) {
      await finishDownload(item);
    }
  } else if (delta.state && delta.state.current === "interrupted") {
    const item = await findDownload(delta.id);
    const reason = item && item.error ? `Chrome 下载中断: ${item.error}` : "Chrome 下载中断";
    await reportDownloadError(reason, item && item.error === "USER_CANCELED" ? "requeue" : "block");
  }
});
