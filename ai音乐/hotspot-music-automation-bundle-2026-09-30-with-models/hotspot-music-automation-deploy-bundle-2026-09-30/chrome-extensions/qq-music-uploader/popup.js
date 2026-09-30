const $ = id => document.getElementById(id);
const status = text => { $('status').textContent = text; };
const DEFAULT_ARTIST = '周华强';

function summaryText(summary) {
  if (!summary) return '队列状态未知';
  const counts = `待处理 ${summary.queued} · 处理中 ${summary.working} · 失败 ${summary.failed} · 结果未确认 ${summary.unverified} · 已确认提交 ${summary.submitted - summary.legacySubmitted}`;
  const legacy = summary.legacySubmitted ? `\n旧版标记已提交 ${summary.legacySubmitted} 首（未核实平台结果，请勿盲目重传）` : '';
  return counts + legacy + (summary.lastError ? `\n最近错误：${summary.lastError}` : '');
}

chrome.runtime.sendMessage({ type: 'queueSummary' }).then(result => {
  if (result?.ok) status(summaryText(result.summary));
}).catch(() => status('读取本地队列失败，请重新加载扩展。'));

chrome.storage.local.get('defaultArtist').then(({ defaultArtist }) => {
  $('artist').value = defaultArtist || DEFAULT_ARTIST;
  if (!defaultArtist) chrome.storage.local.set({ defaultArtist: DEFAULT_ARTIST });
});
$('artist').addEventListener('input', () => {
  chrome.storage.local.set({ defaultArtist: $('artist').value.trim() });
});

function stem(name) {
  return String(name || '')
    .normalize('NFKC')
    .replace(/[\u200B-\u200D\uFEFF]/g, '')
    .replace(/\.[^.]+$/, '')
    .replace(/\s*[（(【\[]\s*\d+\s*[）)】\]]\s*$/u, '')
    .replace(/[^\p{L}\p{N}]+/gu, '')
    .trim()
    .toLowerCase();
}
function base64FromBuffer(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
async function readFile(file) {
  return { name: file.name, type: file.type, buffer: base64FromBuffer(await file.arrayBuffer()) };
}

$('import').addEventListener('click', async () => {
  const audio = [...$('audio').files];
  const lyrics = [...$('lyrics').files];
  if (!audio.length) return status('请先选择 MP3/WAV 文件。');
  const lyricMap = new Map(lyrics.map(file => [stem(file.name), file]));
  const items = [];
  for (const file of audio) {
    const key = stem(file.name);
    const lyric = lyricMap.get(key) || null;
    items.push({
      title: file.name.replace(/\.[^.]+$/, ''),
      artist: $('artist').value.trim(),
      audio: await readFile(file),
      lyric: lyric ? await readFile(lyric) : null
    });
  }
  status('正在写入本地队列…');
  const result = await chrome.runtime.sendMessage({ type: 'importTasks', items });
  if (!result?.ok) return status(`导入失败：${result?.error || 'unknown'}`);
  const unmatched = items.filter(x => !x.lyric).length;
  status(`已导入 ${items.length} 首，歌词匹配 ${items.length - unmatched} 首。\n${summaryText(result.summary)}${unmatched ? '\n未匹配任务不会自动上传。' : ''}`);
});

$('reset').addEventListener('click', async () => {
  const result = await chrome.runtime.sendMessage({ type: 'resetWorking' });
  status(result?.ok ? `已恢复 ${result.requeued} 个失败/中断任务。\n${summaryText(result.summary)}` : `恢复失败：${result?.error || 'unknown'}`);
});

$('requeue').addEventListener('click', async () => {
  const result = await chrome.runtime.sendMessage({ type: 'requeueAll' });
  status(result?.ok ? `已重新排队 ${result.requeued} 个失败/中断任务；已提交或结果未确认的任务不会重传。\n${summaryText(result.summary)}` : `重新排队失败：${result?.error || 'unknown'}`);
});
