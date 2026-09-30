const DB_NAME = 'qqMusicUploader';
const STORE = 'tasks';

function bufferToBase64(value) {
  if (typeof value === 'string') return value;
  if (value instanceof ArrayBuffer) value = new Uint8Array(value);
  if (ArrayBuffer.isView(value)) value = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (!(value instanceof Uint8Array)) return '';
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < value.length; i += chunk) {
    binary += String.fromCharCode(...value.subarray(i, i + chunk));
  }
  return btoa(binary);
}
function taskForMessage(task) {
  return {
    ...task,
    audioBuffer: null,
    lyricBuffer: null,
    payloadChunked: true
  };
}

function hasFilePayload(value) {
  return typeof value === 'string' && value.length > 0
    || value instanceof ArrayBuffer
    || ArrayBuffer.isView(value);
}

function ensureBase64Payload(value, label) {
  const payload = bufferToBase64(value);
  if (!payload) throw new Error(`${label}内容为空或未正确传输，请重新选择文件后导入`);
  return payload;
}

async function injectUploader(tabId, url) {
  if (!url || !url.startsWith('https://y.qq.com/')) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: false }, files: ['content.js'] });
  } catch (error) {
    console.debug('qq-music-uploader injection skipped', String(error));
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete') injectUploader(tabId, tab.url);
});

chrome.runtime.onStartup.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: ['https://y.qq.com/*'] });
  for (const tab of tabs) if (tab.id) injectUploader(tab.id, tab.url);
});

chrome.runtime.onInstalled.addListener(async () => {
  const tabs = await chrome.tabs.query({ url: ['https://y.qq.com/*'] });
  for (const tab of tabs) if (tab.id) injectUploader(tab.id, tab.url);
});

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE, { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withStore(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const result = fn(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('IndexedDB transaction aborted'));
  });
}

async function listTasks() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE).objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function queueSummary() {
  const tasks = await listTasks();
  const summary = { queued: 0, working: 0, failed: 0, submitted: 0, unverified: 0, legacySubmitted: 0, lastError: '' };
  for (const task of tasks) {
    if (Object.hasOwn(summary, task.status)) summary[task.status] += 1;
    if (task.status === 'submitted' && !task.verifiedSubmission) summary.legacySubmitted += 1;
  }
  const lastError = tasks.filter(task => task.error && task.status !== 'submitted')
    .sort((a, b) => (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0))[0];
  if (lastError) summary.lastError = String(lastError.error).slice(0, 180);
  return summary;
}

async function importTasks(items) {
  await withStore('readwrite', store => {
    for (const item of items) {
      const audioBuffer = ensureBase64Payload(item.audio?.buffer, '音频');
      const lyricBuffer = item.lyric ? ensureBase64Payload(item.lyric.buffer, '歌词') : null;
      if (!item.lyric?.name || !lyricBuffer) throw new Error(`未找到“${item.audio?.name || item.title}”的歌词内容`);
      store.put({
        id: crypto.randomUUID(),
        title: item.title,
        artist: item.artist || '',
        lyricist: item.lyricist || item.artist || '',
        composer: item.composer || item.artist || '',
        description: item.description || '',
        audioName: item.audio.name,
        audioType: item.audio.type || 'audio/mpeg',
        audioBuffer,
        lyricName: item.lyric?.name || '',
        lyricType: item.lyric?.type || 'text/plain',
        lyricBuffer,
        status: 'queued',
        createdAt: Date.now()
      });
    }
  });
  return queueSummary();
}

async function nextTask() {
  const tasks = (await listTasks()).sort((a, b) => a.createdAt - b.createdAt);
  let skippedInvalid = 0;
  for (const task of tasks) {
    if (task.status !== 'queued') continue;
    if (!task.lyricName || !hasFilePayload(task.lyricBuffer)) {
      skippedInvalid += 1;
      task.status = 'failed';
      task.error = '缺少歌词文件内容，已跳过；请重新导入音频和同名歌词。';
      task.updatedAt = Date.now();
      await withStore('readwrite', store => store.put(task));
      continue;
    }
    if (!hasFilePayload(task.audioBuffer)) {
      skippedInvalid += 1;
      task.status = 'failed';
      task.error = '缺少音频文件内容，已跳过；请重新导入 MP3/WAV。';
      task.updatedAt = Date.now();
      await withStore('readwrite', store => store.put(task));
      continue;
    }
    task.status = 'working';
    task.updatedAt = Date.now();
    await withStore('readwrite', store => store.put(task));
    return taskForMessage(task);
  }
  return skippedInvalid
    ? { queueError: `队列中有 ${skippedInvalid} 条任务没有有效文件内容，请重新导入文件` }
    : null;
}

async function updateTask(id, status, error = '') {
  const tasks = await listTasks();
  const task = tasks.find(x => x.id === id);
  if (!task) return null;
  task.status = status;
  task.error = error;
  if (status === 'submitted') task.verifiedSubmission = true;
  task.updatedAt = Date.now();
  await withStore('readwrite', store => store.put(task));
  return task;
}

async function taskPayload(id, field, offset = 0, chunkSize = 262144) {
  const task = (await listTasks()).find(item => item.id === id);
  if (!task) throw new Error('任务不存在');
  const key = field === 'audio' ? 'audioBuffer' : field === 'lyric' ? 'lyricBuffer' : '';
  if (!key) throw new Error('未知文件类型');
  const payload = bufferToBase64(task[key]);
  if (!payload) throw new Error(`${field === 'audio' ? '音频' : '歌词'}内容为空`);
  return { chunk: payload.slice(offset, offset + chunkSize), offset, total: payload.length };
}

async function requeueAll() {
  const tasks = await listTasks();
  const { defaultArtist = '周华强' } = await chrome.storage.local.get('defaultArtist');
  let requeued = 0;
  for (const task of tasks) {
    const interrupted = task.status === 'working' && Date.now() - (task.updatedAt || task.createdAt || 0) > 120000;
    if (task.status !== 'failed' && !interrupted) continue;
    if (!task.lyricName || !hasFilePayload(task.lyricBuffer) || !hasFilePayload(task.audioBuffer)) continue;
    task.status = 'queued';
    task.error = '';
    if (defaultArtist) {
      if (!task.artist) task.artist = defaultArtist;
      if (!task.lyricist) task.lyricist = defaultArtist;
      if (!task.composer) task.composer = defaultArtist;
    }
    task.updatedAt = Date.now();
    await withStore('readwrite', store => store.put(task));
    requeued += 1;
  }
  return { requeued, summary: await queueSummary() };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === 'importTasks') sendResponse({ ok: true, summary: await importTasks(message.items) });
    else if (message.type === 'listTasks') sendResponse({ ok: true, tasks: await listTasks() });
    else if (message.type === 'queueSummary') sendResponse({ ok: true, summary: await queueSummary() });
    else if (message.type === 'taskPayload') sendResponse({ ok: true, payload: await taskPayload(message.id, message.field, message.offset || 0) });
    else if (message.type === 'nextTask') {
      const task = await nextTask();
      if (task?.queueError) sendResponse({ ok: false, error: task.queueError, summary: await queueSummary() });
      else sendResponse({ ok: true, task, summary: await queueSummary() });
    }
    else if (message.type === 'updateTask') sendResponse({ ok: true, task: await updateTask(message.id, message.status, message.error) });
    else if (message.type === 'requeueAll') sendResponse({ ok: true, ...await requeueAll() });
    else if (message.type === 'resetWorking') {
      sendResponse({ ok: true, ...await requeueAll() });
    } else sendResponse({ ok: false, error: 'unknown_message' });
  })().catch(error => sendResponse({ ok: false, error: String(error) }));
  return true;
});
