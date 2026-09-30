const KEY = 'neteaseUploaderState';
const DB_NAME = 'neteaseUploaderFiles';
const DB_VERSION = 1;

const initial = {
  queue: [],
  running: false,
  activeId: null,
  status: '等待导入',
  updatedAt: 0
};

async function readState() {
  const data = await chrome.storage.local.get(KEY);
  return { ...initial, ...(data[KEY] || {}) };
}

async function writeState(patch) {
  const state = { ...(await readState()), ...patch, updatedAt: Date.now() };
  await chrome.storage.local.set({ [KEY]: state });
  return state;
}

function openFileDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => request.result.createObjectStore('files', { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('文件数据库打开失败'));
  });
}
async function readFileChunk(id, offset, length) {
  const db = await openFileDb();
  return await new Promise((resolve, reject) => {
    const request = db.transaction('files', 'readonly').objectStore('files').get(id);
    request.onsuccess = () => {
      const file = request.result;
      if (!file) return reject(new Error(`找不到本地文件：${id}`));
      const dataUrl = String(file.dataUrl || '');
      const start = Math.max(0, Number(offset) || 0);
      const end = Math.min(dataUrl.length, start + Math.max(1, Number(length) || 400000));
      resolve({ chunk: dataUrl.slice(start, end), nextOffset: end, done: end >= dataUrl.length, total: dataUrl.length });
    };
    request.onerror = () => reject(request.error || new Error('读取本地文件失败'));
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === 'getState') return sendResponse(await readState());
    if (message.type === 'getFileChunk') return sendResponse(await readFileChunk(message.fileId, message.offset, message.length));
    if (message.type === 'setState') return sendResponse(await writeState(message.patch || {}));
    if (message.type === 'clearQueue') return sendResponse(await writeState({ queue: [], activeId: null, status: '队列已清空' }));
    if (message.type === 'stop') return sendResponse(await writeState({ running: false, status: '已停止' }));
    if (message.type === 'importQueue') {
      const current = await readState();
      return sendResponse(await writeState({
        queue: message.queue || [],
        running: false,
        activeId: null,
        status: `已导入 ${message.queue?.length || 0} 首`
      }));
    }
    if (message.type === 'contentStatus') {
      const current = await readState();
      const queue = current.queue.map(item => item.id === message.id
        ? { ...item, status: message.status, error: message.error || '', step: message.step || '' }
        : item);
      return sendResponse(await writeState({ queue, status: message.summary || message.status }));
    }
    if (message.type === 'start') return sendResponse(await writeState({ running: true, status: '等待当前页面开始' }));
    sendResponse({ ok: false, error: '未知消息' });
  })().catch(error => sendResponse({ ok: false, error: error.message }));
  return true;
});
