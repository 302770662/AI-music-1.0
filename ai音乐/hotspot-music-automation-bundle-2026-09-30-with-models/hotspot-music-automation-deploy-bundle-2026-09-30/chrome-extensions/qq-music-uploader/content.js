(() => {
  const DEFAULT_ARTIST = '周华强';
  const currentVersion = chrome.runtime.getManifest().version;
  if (window.__qqmuUploaderLoaded?.version === currentVersion) return;
  // Extension reloads invalidate the old isolated world while the page stays open.
  // Remove only this extension's old UI before starting the new context.
  document.querySelector('#qqmu-panel')?.remove();
  document.querySelector('#qqmu-panel-style')?.remove();
  window.__qqmuUploaderLoaded = { version: currentVersion };
  const state = { running: false, task: null };
  let stopRequested = false;
  let runPromise = null;
  const isUploadPage = () => {
    const route = `${location.pathname}${location.hash}`;
    return route.includes('/venus/personal/work/demo-trade/upload')
      || route.includes('/venus/demoupload/demo-trade/upload');
  };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function runtimeMessage(message, timeoutMs = 15000) {
    let timer;
    try {
      return await Promise.race([
        chrome.runtime.sendMessage(message),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`后台队列响应超时（${timeoutMs}ms）`)), timeoutMs);
        })
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function getPanel() { return document.querySelector('#qqmu-panel'); }
  function status(text) {
    const node = document.querySelector('#qqmu-panel #qqmu-status');
    if (node) node.textContent = text;
  }
  function unmountPanel() {
    const panel = getPanel();
    if (panel) panel.remove();
    const style = document.querySelector('#qqmu-panel-style');
    if (style) style.remove();
  }

  function mountPanel() {
    if (!isUploadPage() || !document.documentElement) return;
    if (getPanel()) return;
    const panel = document.createElement('div');
    panel.id = 'qqmu-panel';
    Object.assign(panel.style, {
      position: 'fixed', top: '72px', right: '18px', zIndex: '2147483647',
      width: '240px', padding: '12px', background: '#fff', color: '#15304b',
      border: '1px solid #c9d6e5', borderRadius: '8px', boxShadow: '0 4px 18px rgba(0,0,0,.18)',
      font: '13px/1.45 system-ui, sans-serif'
    });
    panel.innerHTML = `<b>腾讯音乐上传助手 v${currentVersion}</b><span id="qqmu-status">已连接</span><button id="qqmu-start">开始自动上传</button><button id="qqmu-stop">停止</button>`;
    document.documentElement.appendChild(panel);
    const style = document.createElement('style');
    style.id = 'qqmu-panel-style';
    style.textContent = '#qqmu-panel b,#qqmu-panel span{display:block;margin-bottom:7px}#qqmu-panel button{margin:3px 5px 0 0;padding:6px 9px;border:0;border-radius:4px;background:#1677ff;color:#fff;cursor:pointer}#qqmu-panel button:last-child{background:#6b7788}';
    document.documentElement.appendChild(style);
    panel.querySelector('#qqmu-start').onclick = () => { stopRequested = false; runPromise = run(); };
    panel.querySelector('#qqmu-stop').onclick = () => { stopRequested = true; state.running = false; status('已停止，新任务不会继续提交'); };
  }

  const visible = el => !!el && el.getClientRects().length > 0;
  const nativeValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;

  function deepQueryAll(selector, root = document, output = []) {
    output.push(...root.querySelectorAll(selector));
    for (const node of root.querySelectorAll('*')) {
      if (node.shadowRoot) deepQueryAll(selector, node.shadowRoot, output);
      if (node.tagName === 'IFRAME') {
        try {
          if (node.contentDocument) deepQueryAll(selector, node.contentDocument, output);
        } catch (_) {
          // Cross-origin frames are intentionally skipped.
        }
      }
    }
    return output;
  }
  function allText() { return deepQueryAll('button,a,span,div').filter(visible); }
  function isDisabled(el) {
    return !!el && (el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/.test(String(el.className || '')));
  }
  function findAction(text) {
    const normalize = value => String(value || '').replace(/\s+/g, '').trim();
    const wanted = normalize(text);
    const candidates = deepQueryAll('button,[role="button"],a,[onclick],[class*="btn"],[class*="button"],span,div')
      .filter(x => visible(x) && !x.closest('#qqmu-panel'));
    const node = candidates.find(x => normalize(x.textContent) === wanted)
      || candidates.find(x => normalize(x.textContent).includes(wanted));
    if (!node) return null;
    let current = node;
    for (let i = 0; current && i < 8; i += 1, current = current.parentElement) {
      if (current.matches?.('button,[role="button"],a,[onclick],[class*="btn"],[class*="button"]')) return current;
    }
    return node;
  }
  function clickText(text) {
    const el = findAction(text);
    if (el && !isDisabled(el)) {
      el.scrollIntoView({ block: 'center', inline: 'center' });
      el.focus?.();
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
      el.click();
      return true;
    }
    return false;
  }
  function infoModalOpen() {
    return !!deepQueryAll('input').find(input => visible(input) && /词作者|曲作者|含税价格/.test(input.placeholder || ''));
  }
  async function openInfoModal() {
    if (infoModalOpen()) return true;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline && isUploadPage()) {
      status('正在打开完善信息…');
      if (clickText('完善信息')) {
        if (await waitForInfoForm()) return true;
      }
      await sleep(250);
    }
    return infoModalOpen();
  }
  async function waitForFileReady(task) {
    const deadline = Date.now() + 20000;
    let stableSince = 0;
    while (Date.now() < deadline && isUploadPage()) {
      const audioInput = fileInput('audio');
      const pageText = allText().map(x => x.textContent.trim()).join(' ');
      if (/上传失败|文件上传失败|音频上传失败|歌词上传失败/.test(pageText)) {
        throw new Error('腾讯页面报告文件上传失败，请检查音频和歌词格式');
      }
      const nameVisible = pageText.includes(task.audioName) || pageText.includes(task.title);
      const stillProcessing = /上传中|正在上传|准备中|处理中/.test(pageText);
      if (nameVisible && !stillProcessing) {
        if (!stableSince) stableSince = Date.now();
        if (Date.now() - stableSince >= 4000) return;
      } else {
        stableSince = 0;
      }
      if (audioInput?.files?.length && !nameVisible) stableSince = 0;
      await sleep(500);
    }
    throw new Error('音频文件尚未准备完成，未点击上传');
  }
  async function waitForUploadAction() {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline && isUploadPage()) {
      status('等待上传按钮可用…');
      const upload = findAction('上传');
      if (upload && !isDisabled(upload)) return upload;
      await sleep(500);
    }
    return null;
  }
  async function waitForSubmissionResult(task) {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      if (!isUploadPage()) return { verified: true, reason: '页面已离开上传页' };
      const pageText = allText().map(x => x.textContent.trim()).join(' ');
      if (/上传成功|提交成功|作品上传成功|上传完成|已提交|审核中/.test(pageText)) {
        return { verified: true, reason: '页面显示已提交或审核中' };
      }
      if (/上传失败|提交失败|文件上传失败|音频上传失败|歌词上传失败/.test(pageText)) {
        throw new Error('腾讯页面报告提交失败');
      }
      if (/上传中|正在上传|处理中|准备中/.test(pageText)) status(`腾讯页面处理中：${task.title}`);
      await sleep(500);
    }
    throw new Error(`已点击上传，但页面未确认提交：${task.title}`);
  }
  function setInput(input, value) {
    const view = input.ownerDocument?.defaultView || window;
    const proto = input.tagName === 'TEXTAREA' ? view.HTMLTextAreaElement.prototype : view.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set || nativeValue;
    input.focus();
    setter.call(input, String(value));
    input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    input.blur();
    return input.value === String(value);
  }
  function setSelect(select, value) {
    const view = select.ownerDocument?.defaultView || window;
    const setter = Object.getOwnPropertyDescriptor(view.HTMLSelectElement.prototype, 'value')?.set;
    if (setter) setter.call(select, value); else select.value = value;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function nearbyText(el) {
    let node = el;
    let text = '';
    for (let i = 0; node && i < 5; i += 1, node = node.parentElement) {
      text += ` ${node.textContent || ''}`;
      if (text.length > 500) break;
    }
    return text;
  }
  function fieldContext(el, label) {
    let node = el.parentElement;
    for (let i = 0; node && i < 8; i += 1, node = node.parentElement) {
      const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
      if (text.includes(label) && text.length <= 400) return text;
    }
    return '';
  }
  function isPriceInput(input) {
    return input.type === 'number' || /价格|报价/.test(
      `${input.placeholder || ''} ${input.getAttribute('aria-label') || ''} ${fieldContext(input, '报价')} ${fieldContext(input, '价格')} ${fieldContext(input, '含税')}`
    );
  }
  function labeledInput(label) {
    const direct = deepQueryAll('input,textarea').find(input => visible(input) && !input.disabled && (
      input.placeholder?.includes(label) || input.getAttribute('aria-label')?.includes(label)
    ));
    if (direct) return direct;
    return deepQueryAll('input,textarea').filter(visible).find(input => fieldContext(input, label).includes(label));
  }
  function visibleInputByPlaceholder(pattern) {
    return deepQueryAll('input').find(input => visible(input) && !input.disabled && pattern.test(input.placeholder || '')) || null;
  }
  function customControl(label) {
    const controls = deepQueryAll('select,input,[role="combobox"],[aria-haspopup="listbox"],button,div,span').filter(visible);
    return controls.find(control => {
      const text = fieldContext(control, label);
      const ownText = `${control.value || ''} ${control.textContent || ''}`.trim();
      return text.includes(label) && (/请选择|选择是否|原创作品|不参加活动/.test(ownText) || control.getAttribute('role') === 'combobox');
    });
  }
  async function chooseCustom(label, preferredText) {
    const control = customControl(label);
    if (!control) {
      const placeholder = label === '活动列表' ? '请选择是否参加活动' : '请选择';
      const candidates = allText().filter(x => x.textContent.trim() === placeholder && !x.closest('#qqmu-panel'));
      const candidate = candidates[0];
      if (!candidate) return false;
      candidate.click();
      await sleep(250);
      if (clickText(preferredText)) return true;
      const option = deepQueryAll('[role="option"],li,button,div').filter(visible).find(x => {
        const text = x.textContent.trim();
        return text && !/请选择|选择是否/.test(text) && text.length < 40;
      });
      if (option) { option.click(); return true; }
      return false;
    }
    if (control.tagName === 'SELECT') {
      const option = [...control.options].find(x => x.textContent.trim() === preferredText)
        || [...control.options].find(x => x.value && !/请选择/.test(x.textContent));
      if (option) setSelect(control, option.value);
      return !!option;
    }
    if (!/请选择|选择是否/.test(`${control.value || ''} ${control.textContent || ''}`)) return true;
    control.click();
    await sleep(250);
    if (clickText(preferredText)) return true;
    const option = deepQueryAll('[role="option"],li,button,div').filter(visible).find(x => {
      const text = x.textContent.trim();
      return text && !/请选择|选择是否/.test(text) && text.length < 40;
    });
    if (option) { option.click(); return true; }
    return false;
  }
  async function fillRequiredInfo(task) {
    const { defaultArtist = DEFAULT_ARTIST } = await chrome.storage.local.get('defaultArtist');
    const author = task.lyricist || task.composer || task.artist || defaultArtist || DEFAULT_ARTIST;
    const inputs = deepQueryAll('input').filter(visible);
    const price = visibleInputByPlaceholder(/含税价格|报价|价格/) || inputs.find(isPriceInput);
    if (price && !price.value && !setInput(price, '100')) throw new Error('报价写入后未被页面接受');

    const lyricist = visibleInputByPlaceholder(/词作者/) || labeledInput('词作者');
    const composer = visibleInputByPlaceholder(/曲作者/) || labeledInput('曲作者');
    if (lyricist && !lyricist.value && author && !setInput(lyricist, author)) throw new Error('词作者写入后未被页面接受');
    if (composer && !composer.value && author && !setInput(composer, author)) throw new Error('曲作者写入后未被页面接受');

    if (lyricist && !lyricist.value) throw new Error('词作者没有填入：请在扩展弹窗中填写默认作者');
    if (composer && !composer.value) throw new Error('曲作者没有填入：请在扩展弹窗中填写默认作者');
    if (price && !price.value) throw new Error('报价没有填入');

    const textareas = deepQueryAll('textarea').filter(visible);
    const description = textareas.find(x => !/歌词|lyric/i.test(`${x.placeholder || ''} ${x.getAttribute('aria-label') || ''}`));
    if (description && !description.value) {
      const text = task.description || `原创音乐作品《${task.title}》`;
      if (!setInput(description, text)) throw new Error('作品描述写入后未被页面接受');
    }

    const selects = deepQueryAll('select').filter(visible);
    for (const select of selects) {
      const current = select.value || '';
      const option = [...select.options].find(x => x.value && !/请选择|不参加活动/.test(x.textContent.trim()));
      if (!current && option) setSelect(select, option.value);
    }
    const originalChosen = await chooseCustom('原创声明', '原创作品');
    const methodChosen = await chooseCustom('创作方式', '个人创作');
    const activityChosen = await chooseCustom('活动列表', '不参加活动');
    return {
      price: price ? price.value : '',
      description: description ? description.value : '',
      selectsFilled: selects.filter(x => x.value).length,
      originalChosen,
      methodChosen,
      activityChosen,
      lyricist: lyricist?.value || '',
      composer: composer?.value || ''
    };
  }
  async function waitForInfoForm() {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline && isUploadPage()) {
      const visibleInputs = deepQueryAll('input').filter(visible);
      const lyricist = visibleInputs.find(input => /词作者/.test(input.placeholder || ''));
      const composer = visibleInputs.find(input => /曲作者/.test(input.placeholder || ''));
      const price = visibleInputs.find(input => /含税价格|报价|价格/.test(input.placeholder || ''));
      if (lyricist && composer && price) return true;
      await sleep(200);
    }
    return false;
  }
  function validateRequiredInfo(info) {
    const missing = [];
    if (!info.lyricist) missing.push('词作者');
    if (!info.composer) missing.push('曲作者');
    if (!info.price) missing.push('报价');
    if (!info.originalChosen) missing.push('原创声明');
    if (!info.methodChosen) missing.push('创作方式');
    if (!info.activityChosen) missing.push('活动列表');
    if (missing.length) throw new Error(`必填信息未完成：${missing.join('、')}`);
  }
  function findTitleInput() {
    const inputs = deepQueryAll('input').filter(x => !x.disabled && x.type !== 'file');
    const semantic = x => /作品名称|作品名|自动读取|请输入|请填写/.test(`${x.placeholder || ''} ${x.getAttribute('aria-label') || ''}`);
    return inputs.find(x => visible(x) && semantic(x))
      || inputs.find(semantic)
      || inputs.find(x => visible(x) && x.type === 'text' && !/作者|价格|报价/.test(x.placeholder || ''));
  }
  function fileInput(kind) {
    const files = deepQueryAll('input[type=file]').filter(x => !x.disabled);
    if (!files.length) return null;
    const audioLike = x => /mp3|wav|audio|音频|歌曲/.test(
      `${x.accept || ''} ${x.name || ''} ${x.id || ''} ${x.getAttribute('aria-label') || ''}`.toLowerCase()
    );
    return kind === 'audio' ? (files.find(audioLike) || files[0]) : files.find(x => !audioLike(x)) || files[1] || files[0];
  }
  function putFile(input, file) {
    if (!input || !file) return false;
    const view = input.ownerDocument?.defaultView || window;
    const dt = new (view.DataTransfer || DataTransfer)();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return input.files.length === 1;
  }
  function decodeBuffer(value) {
    if (value instanceof ArrayBuffer) return value;
    if (ArrayBuffer.isView(value)) return value.buffer;
    if (typeof value !== 'string') throw new Error('文件内容未正确传输');
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }
  async function loadPayload(task, field) {
    const direct = field === 'audio' ? task.audioBuffer : task.lyricBuffer;
    if (typeof direct === 'string' && direct.length) return direct;
    const chunks = [];
    let offset = 0;
    let total = null;
    while (total === null || offset < total) {
      const result = await runtimeMessage({ type: 'taskPayload', id: task.id, field, offset });
      if (!result?.ok || !result.payload) throw new Error(`读取${field === 'audio' ? '音频' : '歌词'}分块失败`);
      chunks.push(result.payload.chunk || '');
      offset += (result.payload.chunk || '').length;
      total = result.payload.total;
      if (!result.payload.chunk && offset < total) throw new Error(`读取${field === 'audio' ? '音频' : '歌词'}内容中断`);
    }
    return chunks.join('');
  }
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
  async function fillLyric(file) {
    if (!file) return true;
    if (/\.(doc|docx)$/i.test(file.name)) {
      if (!clickText('编辑歌词')) return false;
      await sleep(400);
      const lyricInput = deepQueryAll('input[type=file]').find(x => !x.disabled && x !== fileInput('audio'));
      if (!lyricInput) return false;
      return putFile(lyricInput, file);
    }
    if (!clickText('编辑歌词')) return false;
    await sleep(400);
    const area = deepQueryAll('textarea').find(visible);
    if (!area) return false;
    const text = await file.text();
    area.value = text;
    area.dispatchEvent(new Event('input', { bubbles: true }));
    area.dispatchEvent(new Event('change', { bubbles: true }));
    if (!clickText('确认修改')) return false;
    await sleep(300);
    return true;
  }
  async function fillTask(task) {
    if (!task.lyricName) {
      throw new Error(`未找到与音频配对的歌词文件：${task.audioName || task.title}`);
    }
    task.audioBuffer = await loadPayload(task, 'audio');
    task.lyricBuffer = await loadPayload(task, 'lyric');
    const audio = new File([decodeBuffer(task.audioBuffer)], task.audioName, { type: task.audioType });
    const lyric = task.lyricBuffer ? new File([decodeBuffer(task.lyricBuffer)], task.lyricName, { type: task.lyricType }) : null;
    const title = findTitleInput();
    let audioInput = fileInput('audio') || deepQueryAll('input[type=file]')[0];
    if (!audioInput) {
      clickText('选择文件或拖拽至此');
      clickText('添加音频文件');
      await sleep(500);
      audioInput = fileInput('audio') || deepQueryAll('input[type=file]')[0];
    }
    if (!title || !audioInput) {
      const detail = [
        !title ? '作品名称输入框' : '',
        !audioInput ? `音频文件控件（页面检测到 ${deepQueryAll('input[type=file]').length} 个文件控件）` : ''
      ].filter(Boolean).join('、');
      throw new Error(`未找到${detail}`);
    }
    setInput(title, task.title);
    if (title.value !== task.title) throw new Error('作品名称写入后未被页面接受');
    if (!putFile(audioInput, audio)) throw new Error('音频文件未写入页面控件');
    if (!await fillLyric(lyric)) throw new Error('未找到歌词编辑框');
    status(`等待腾讯页面识别音频：${task.audioName}`);
    await waitForFileReady(task);
    status(`已填入：${task.title}，等待完善信息`);
    if (!await openInfoModal()) throw new Error('未找到或无法打开“完善信息”弹窗');
    const info = await fillRequiredInfo(task);
    status(`已自动填充：报价 ${info.price || '待填写'}，作品描述和创作方式`);
    const inputs = deepQueryAll('input').filter(visible);
    const requiredPrice = inputs.find(isPriceInput);
    if (requiredPrice && !requiredPrice.value) throw new Error('报价未填写，请先在页面输入报价');
    validateRequiredInfo(info);
    const confirmed = clickText('确认') || clickText('确定') || clickText('保存并继续');
    if (!confirmed) throw new Error('未找到完善信息弹窗的确认按钮');
    const closeDeadline = Date.now() + 10000;
    while (Date.now() < closeDeadline && infoModalOpen()) await sleep(200);
    if (infoModalOpen()) throw new Error('完善信息已填写，但弹窗未成功关闭');
    return true;
  }
  async function run() {
    if (state.running || !isUploadPage()) return;
    state.running = true;
    status('读取队列…');
    while (state.running && !stopRequested && isUploadPage()) {
      let result;
      try {
        result = await runtimeMessage({ type: 'nextTask' });
      } catch (error) {
        state.running = false;
        const message = String(error?.message || error);
        status(/context invalidated/i.test(message)
          ? '扩展已更新，请刷新当前页面后重试'
          : `读取队列失败：${message}`);
        break;
      }
      if (result?.ok === false) {
        state.running = false;
        status(`读取队列失败：${result.error || '后台返回错误'}`);
        break;
      }
      if (!result?.task) {
        const summary = result?.summary;
        status(summary?.failed
          ? `没有可运行任务：待处理 ${summary.queued} 首，失败 ${summary.failed} 首。请在扩展弹窗重新导入 MP3 和同名歌词。`
          : '队列已完成');
        break;
      }
      state.task = result.task;
      try {
        await fillTask(state.task);
        if (!state.running || stopRequested || !isUploadPage()) break;
        if (!isUploadPage()) throw new Error('上传页已离开，未执行提交');
        const uploadButton = await waitForUploadAction();
        if (!uploadButton) throw new Error('上传按钮仍不可用，腾讯页面尚未完成文件准备');
        uploadButton.click();
        status(`已点击上传，等待腾讯页面确认：${state.task.title}`);
        const submission = await waitForSubmissionResult(state.task);
        const submittedUrl = location.href;
        await runtimeMessage({
          type: 'updateTask',
          id: state.task.id,
          status: isUploadPage() ? 'submitted' : 'submitted',
          error: `${submission.reason}；地址：${submittedUrl}`
        });
        status(`已确认提交：${state.task.title}`);
      } catch (error) {
        try { await runtimeMessage({ type: 'updateTask', id: state.task.id, status: 'failed', error: String(error) }); } catch (_) {}
        status(`失败：${error.message || error}`);
        state.running = false;
      }
    }
    state.task = null;
    state.running = false;
    runPromise = null;
  }

  let lastUrl = location.href;
  function syncRoute() {
    const changed = location.href !== lastUrl;
    if (changed) {
      lastUrl = location.href;
      if (!isUploadPage()) {
        state.running = false;
        stopRequested = true;
        unmountPanel();
      }
    }
    if (isUploadPage()) mountPanel();
  }
  window.addEventListener('hashchange', syncRoute);
  window.addEventListener('popstate', syncRoute);
  const originalPushState = history.pushState;
  history.pushState = function (...args) { const result = originalPushState.apply(this, args); setTimeout(syncRoute, 0); return result; };
  const originalReplaceState = history.replaceState;
  history.replaceState = function (...args) { const result = originalReplaceState.apply(this, args); setTimeout(syncRoute, 0); return result; };
  syncRoute();
  setInterval(syncRoute, 500);
})();
