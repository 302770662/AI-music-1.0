const $ = id => document.getElementById(id);
const KEY = 'neteaseUploaderState';
const FILE_DB_NAME = 'neteaseUploaderFiles';
const FILE_DB_VERSION = 1;
const ext = chrome.runtime;
const emptyState = () => ({ queue: [], running: false, status: '等待操作', activeId: null, updatedAt: 0 });
let state = emptyState();

function baseName(name) { return name.replace(/\.[^.]+$/, '').trim(); }
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function normalizeState(value) {
  const source = value && typeof value === 'object' ? value : {};
  return { ...emptyState(), ...source, queue: Array.isArray(source.queue) ? source.queue : [] };
}
function statusText() { $('status').textContent = state.status || '等待操作'; }
function render() {
  state = normalizeState(state);
  statusText();
  $('queue').innerHTML = state.queue.length ? state.queue.map((item, i) => {
    const cls = item.status === 'completed' ? 'ok' : item.status === 'failed' ? 'bad' : 'wait';
    return `<div class="item"><span>${i + 1}. ${esc(item.title)}</span><br><small class="${cls}">${esc(item.status || 'pending')}${item.step ? ` · ${esc(item.step)}` : ''}${item.error ? ` · ${esc(item.error)}` : ''}</small></div>`;
  }).join('') : '<small>队列为空</small>';
}
async function send(message) {
  return await new Promise((resolve, reject) => {
    ext.sendMessage(message, response => {
      const runtimeError = chrome.runtime.lastError;
      if (runtimeError) return reject(new Error(runtimeError.message));
      if (response === undefined) return reject(new Error('后台服务未返回状态，请在 chrome://extensions 重新加载插件'));
      resolve(response);
    });
  });
}
async function refresh() {
  try { state = normalizeState(await send({ type: 'getState' })); render(); }
  catch (error) { $('status').textContent = `无法连接后台：${error.message}`; render(); }
}
async function filesToData(files) {
  return Promise.all(Array.from(files || []).map(file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, type: file.type, size: file.size, dataUrl: reader.result });
    reader.onerror = () => reject(reader.error || new Error(`读取失败：${file.name}`));
    reader.readAsDataURL(file);
  })));
}
function lyricFor(audio, lyrics) {
  const key = baseName(audio.name).toLowerCase();
  return (lyrics || []).find(x => baseName(x.name).toLowerCase() === key) || null;
}
function openFileDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(FILE_DB_NAME, FILE_DB_VERSION);
    request.onupgradeneeded = () => request.result.createObjectStore('files', { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('文件数据库打开失败'));
  });
}
async function persistFile(file) {
  const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const db = await openFileDb();
  await new Promise((resolve, reject) => {
    const request = db.transaction('files', 'readwrite').objectStore('files').put({ id, ...file });
    request.onsuccess = resolve;
    request.onerror = () => reject(request.error || new Error(`保存文件失败：${file.name}`));
  });
  return { fileId: id, name: file.name, type: file.type, size: file.size };
}
async function importQueueFromInputs() {
  $('status').textContent = '正在读取音频文件…';
  const audios = await filesToData($('audio').files);
  $('status').textContent = `已读取 ${audios.length} 个音频，正在读取歌词…`;
  const lyrics = await filesToData($('lyrics').files);
  if (!audios.length) throw new Error('请先选择至少一个 MP3/WAV/FLAC 音频文件');
  const metadata = {
    singer: $('singer').value.trim() || '周华强',
    lyricist: $('lyricist').value.trim() || '周华强',
    composer: $('composer').value.trim() || '周华强',
    genre: $('genre').value.trim() || '流行 Pop',
    language: $('language').value.trim() || '国语',
    version: $('version').value.trim() || '录音室版',
    aiType: $('aiType').value.trim() || 'Suno AI',
    original: 'original',
    isAi: true,
    publishMode: $('publishMode').value,
    autoSubmit: $('autoSubmit').checked
  };
  $('status').textContent = `正在保存 ${audios.length} 个音频文件…`;
  const audioRefs = await Promise.all(audios.map(persistFile));
  const lyricRefs = await Promise.all(lyrics.map(persistFile));
  const lyricsByName = new Map(lyricRefs.map(file => [baseName(file.name).toLowerCase(), file]));
  const queue = audioRefs.map((audio, index) => ({ id: `${Date.now()}-${index}`, title: baseName(audio.name), audio, lyric: lyricsByName.get(baseName(audio.name).toLowerCase()) || null, metadata, status: 'pending', step: '' }));
  $('status').textContent = `正在保存 ${queue.length} 首歌曲到队列…`;
  try {
    state = normalizeState(await send({ type: 'importQueue', queue }));
  } catch (error) {
    // Keep importing usable even if the service worker was restarted between
    // selecting the file and clicking the button.
    const stored = await chrome.storage.local.get(KEY);
    const current = normalizeState(stored[KEY]);
    state = { ...current, queue, running: false, activeId: null, status: `已导入 ${queue.length} 首`, updatedAt: Date.now() };
    await chrome.storage.local.set({ [KEY]: state });
  }
  if (!state.queue.length) throw new Error('导入后队列仍为空，请重新加载插件后再试');
  state.status = `已导入 ${state.queue.length} 首歌曲`;
  render();
  return state;
}
async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}
async function connectPage(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'ping' });
    return;
  } catch (_) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await new Promise(resolve => setTimeout(resolve, 250));
    await chrome.tabs.sendMessage(tabId, { type: 'ping' });
  }
}

$('import').onclick = async () => {
  try {
    await importQueueFromInputs();
  } catch (error) { $('status').textContent = `导入失败：${error.message}`; }
};
$('start').onclick = async () => {
  try {
    if (!state.queue.length) {
      if (!$('audio').files?.length) throw new Error('队列为空，请先选择音频文件；点击开始时会自动导入');
      await importQueueFromInputs();
    }
    const tab = await activeTab();
    if (!tab?.id || !/^https:\/\/music\.163\.com\//.test(tab.url || '')) throw new Error('请先把当前标签页打开到网易云版权上传页面');
    state = normalizeState(await send({ type: 'start' }));
    await connectPage(tab.id);
    await chrome.tabs.sendMessage(tab.id, { type: 'start' });
    state.status = `已开始处理 ${state.queue.length} 首歌曲`;
    render();
  } catch (error) {
    state.status = `开始失败：${error.message}`;
    render();
  }
};
$('stop').onclick = async () => { state = normalizeState(await send({ type: 'stop' })); render(); };
$('clear').onclick = async () => { state = normalizeState(await send({ type: 'clearQueue' })); render(); };
chrome.storage.onChanged.addListener(changes => { if (changes[KEY]) { state = normalizeState(changes[KEY].newValue); render(); } });
refresh();
