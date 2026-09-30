(() => {
  const currentVersion = chrome.runtime.getManifest().version;
  if (window.__neteaseUploaderLoaded === currentVersion) return;
  document.getElementById('netease-uploader-panel')?.remove();
  window.__neteaseUploaderLoaded = currentVersion;
  const panelId = 'netease-uploader-panel';
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => !!el && el.getClientRects().length > 0;
  const norm = value => String(value || '').replace(/\s+/g, '').trim();
  let stopRequested = false;

  function deepAll(selector, root = document, out = []) {
    out.push(...root.querySelectorAll(selector));
    for (const node of root.querySelectorAll('*')) if (node.shadowRoot) deepAll(selector, node.shadowRoot, out);
    return out;
  }
  function status(message) {
    const node = document.querySelector(`#${panelId} .nu-status`);
    if (node) node.textContent = message;
  }
  async function report(item, patch) {
    try { await chrome.runtime.sendMessage({ type: 'contentStatus', id: item.id, ...patch }); }
    catch (_) { /* The visible panel still reports the failure if the worker restarts. */ }
  }
  function mount() {
    if (document.getElementById(panelId)) return;
    const panel = document.createElement('div');
    panel.id = panelId;
    panel.innerHTML = '<b>网易云上传助手</b><span class="nu-status">已连接</span><button class="nu-stop">停止</button>';
    Object.assign(panel.style, { position:'fixed', right:'18px', top:'72px', zIndex:2147483647, width:'245px', padding:'12px', background:'#fff', color:'#172033', border:'1px solid #cbd5e1', borderRadius:'7px', boxShadow:'0 4px 18px rgba(0,0,0,.18)', font:'13px/1.45 system-ui,sans-serif' });
    const style = document.createElement('style'); style.textContent = `#${panelId} b,#${panelId} span{display:block;margin-bottom:7px}#${panelId} button{padding:6px 10px;border:0;border-radius:4px;background:#64748b;color:#fff;cursor:pointer}`; document.documentElement.append(panel, style);
    panel.querySelector('.nu-stop').onclick = async () => { stopRequested = true; status('正在停止…'); await chrome.runtime.sendMessage({ type:'stop' }); };
  }
  function textNodes() { return deepAll('button,[role="button"],a,label,span,div').filter(visible).filter(x => !x.closest(`#${panelId}`)); }
  function findText(text) { const wanted = norm(text); return textNodes().find(x => norm(x.textContent) === wanted) || textNodes().find(x => norm(x.textContent).includes(wanted)); }
  function clickText(text) {
    const node = findText(text); if (!node) return false;
    const target = node.closest('button,[role="button"],a') || node;
    if (target.getAttribute('aria-disabled') === 'true' || target.disabled) return false;
    target.scrollIntoView({ block:'center' }); target.click(); return true;
  }
  function exactVisibleText(text) {
    const wanted = norm(text);
    return textNodes().filter(x => norm(x.textContent) === wanted)
      .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length)[0] || null;
  }
  async function chooseVisible(text) {
    const node = exactVisibleText(text);
    if (!node) throw new Error(`找不到默认选项：${text}`);
    const target = node.closest('label,button,[role="radio"],[role="option"],[role="button"]') || node;
    if (target.getAttribute('aria-disabled') === 'true' || target.disabled) throw new Error(`默认选项不可用：${text}`);
    target.scrollIntoView({ block:'center', inline:'center' });
    target.click();
    await sleep(250);
    return true;
  }
  function clickOptionText(text) {
    const wanted = norm(text);
    const optionNodes = deepAll('li,[role="option"],[role="menuitem"],[data-value],[data-label],button,div,span,p')
      .filter(visible).filter(x => !x.closest(`#${panelId}`));
    const nodes = optionNodes.filter(x => norm(x.textContent) === wanted)
      .sort((a, b) => {
        const aScore = /option|menuitem|select-item|dropdown-item/i.test(`${a.getAttribute('role') || ''} ${a.className || ''}`) ? 0 : 1;
        const bScore = /option|menuitem|select-item|dropdown-item/i.test(`${b.getAttribute('role') || ''} ${b.className || ''}`) ? 0 : 1;
        return aScore - bScore || (a.textContent || '').length - (b.textContent || '').length;
      });
    const option = nodes.find(x => x.closest('[role="option"],li,[role="menuitem"],.ant-select-item,.el-select-dropdown__item')) || nodes[0];
    if (!option) return false;
    const target = option.closest('[role="option"],li,[role="menuitem"],button,[role="button"],.ant-select-item,.el-select-dropdown__item') || option;
    if (target.getAttribute('aria-disabled') === 'true' || target.classList.contains('disabled')) return false;
    target.scrollIntoView({ block:'center', inline:'center' });
    target.click();
    target.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true, view:window }));
    target.dispatchEvent(new MouseEvent('mouseup', { bubbles:true, cancelable:true, view:window }));
    target.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, view:window }));
    return true;
  }
  function clickDropdownOptionAt(control, text) {
    if (!control) return false;
    const wanted = norm(text);
    const rect = control.getBoundingClientRect();
    const xs = [rect.left + 28, rect.left + rect.width / 2, rect.right - 28];
    const ys = [rect.bottom + 20, rect.bottom + 42, rect.bottom + 64, rect.bottom + 86];
    for (const x of xs) for (const y of ys) {
      const nodes = document.elementsFromPoint(Math.max(1, x), Math.max(1, y)) || [];
      const hit = nodes.find(node => visible(node) && norm(node.textContent) === wanted)
        || nodes.find(node => visible(node) && /option|menuitem|select-item|dropdown-item/i.test(`${node.getAttribute?.('role') || ''} ${node.className || ''}`));
      if (!hit) continue;
      const target = hit.closest?.('[role="option"],[role="menuitem"],li,button,[data-value]') || hit;
      target.scrollIntoView({ block:'center', inline:'center' });
      for (const node of [...nodes].reverse()) {
        node.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true, view:window }));
        node.dispatchEvent(new MouseEvent('mouseup', { bubbles:true, cancelable:true, view:window }));
        node.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, view:window }));
      }
      target.click();
      return true;
    }
    return false;
  }
  async function chooseRadio(text, contextWords = []) {
    const wanted = norm(text);
    let scope = document;
    if (contextWords.length) {
      const label = labelNode(contextWords);
      let current = label;
      while (current && current !== document.body) {
        if (current.querySelector?.('input[type="radio"]')) { scope = current; break; }
        current = current.parentElement;
      }
      if (label) {
        const labelRect = label.getBoundingClientRect();
        const scopedText = textNodes().filter(node => norm(node.textContent) === wanted)
          .filter(node => {
            const rect = node.getBoundingClientRect();
            return Math.abs(rect.top - labelRect.top) < Math.max(36, labelRect.height * 3)
              && rect.left >= labelRect.left;
          })
          .sort((a, b) => Math.abs(a.getBoundingClientRect().left - labelRect.right) - Math.abs(b.getBoundingClientRect().left - labelRect.right))[0];
        const localRadios = deepAll('input[type="radio"]', scope).filter(x => !x.closest(`#${panelId}`));
        if (scopedText && localRadios.length) {
          const textRect = scopedText.getBoundingClientRect();
          const radio = localRadios.map(input => {
            const linked = input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null;
            const box = (linked || input.closest('label') || input.parentElement || input).getBoundingClientRect();
            const dx = box.left + box.width / 2 - (textRect.left + textRect.width / 2);
            const dy = box.top + box.height / 2 - (textRect.top + textRect.height / 2);
            return { input, linked, distance: Math.hypot(dx, dy) };
          }).sort((a, b) => a.distance - b.distance)[0];
          if (radio) {
            (radio.linked || radio.input.closest('label') || radio.input).click();
            if (!radio.input.checked) radio.input.click();
            radio.input.dispatchEvent(new Event('input', { bubbles:true }));
            radio.input.dispatchEvent(new Event('change', { bubbles:true }));
            await sleep(500);
            if (!radio.input.checked) throw new Error(`未能点击“${text}”单选项`);
            return true;
          }
        }
        if (scopedText) {
          const textBox = scopedText.getBoundingClientRect();
          const controls = deepAll('input[type="radio"],input[type="checkbox"],[role="radio"]')
            .filter(control => !control.closest(`#${panelId}`))
            .map(control => {
              const linked = control.id ? document.querySelector(`label[for="${CSS.escape(control.id)}"]`) : null;
              const owner = linked || control.closest('label') || control.parentElement || control;
              const box = owner.getBoundingClientRect();
              return { control, owner, box, distance: Math.hypot(box.left - textBox.left, box.top - textBox.top) };
            })
            .filter(item => item.box.width > 0 && item.box.height > 0 && Math.abs(item.box.top - textBox.top) < 40)
            .sort((a, b) => a.distance - b.distance);
          const nearest = controls[0];
          if (nearest) {
            nearest.owner.scrollIntoView({ block:'center', inline:'center' });
            nearest.owner.click();
            nearest.control.click?.();
            nearest.control.dispatchEvent(new Event('input', { bubbles:true }));
            nearest.control.dispatchEvent(new Event('change', { bubbles:true }));
            await sleep(500);
            if (nearest.control.checked || nearest.control.getAttribute('aria-checked') === 'true' || nearest.owner.getAttribute('aria-checked') === 'true') return true;
          }
        }
        if (scopedText) {
          const textRect = scopedText.getBoundingClientRect();
          const pointTargets = [-32, -28, -24, -20, -16, -12, -8, -4, 0, 5].map(offset => document.elementFromPoint(
            Math.max(1, textRect.left + offset),
            textRect.top + textRect.height / 2
          )).filter(Boolean);
          for (const pointTarget of pointTargets) {
            pointTarget.scrollIntoView({ block:'center', inline:'center' });
            pointTarget.click();
            pointTarget.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true, view:window }));
            pointTarget.dispatchEvent(new MouseEvent('mouseup', { bubbles:true, cancelable:true, view:window }));
            pointTarget.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, view:window }));
            await sleep(180);
            if (pointTarget.matches?.('[aria-checked="true"],[data-checked="true"],[data-selected="true"]') || pointTarget.closest?.('[aria-checked="true"],[data-checked="true"],[data-selected="true"]')) return true;
          }
          const candidates = [scopedText, scopedText.previousElementSibling, scopedText.nextElementSibling];
          let currentNode = scopedText;
          for (let i = 0; currentNode && i < 6; i += 1, currentNode = currentNode.parentElement) {
            if (currentNode !== document.body && currentNode !== document.documentElement) candidates.push(currentNode);
            if (currentNode?.parentElement) candidates.push(...Array.from(currentNode.parentElement.children));
          }
          const preferred = candidates.filter(node => node && /radio|checkbox|choice|option|select|label/i.test(`${node.getAttribute?.('role') || ''} ${node.className || ''}`));
          const clickTargets = [...preferred, ...candidates]
            .filter(node => node && visible(node))
            .sort((a, b) => (a.getClientRects()[0]?.width || 9999) - (b.getClientRects()[0]?.width || 9999))
            .filter((node, index, list) => list.indexOf(node) === index);
          for (const target of clickTargets) {
            target.scrollIntoView({ block:'center', inline:'center' });
            target.click();
            target.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true, view:window }));
            target.dispatchEvent(new MouseEvent('mouseup', { bubbles:true, cancelable:true, view:window }));
            target.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, view:window }));
            await sleep(180);
            const stateText = `${target.getAttribute('aria-checked') || ''} ${target.getAttribute('data-checked') || ''} ${target.getAttribute('data-selected') || ''} ${target.className || ''}`.toLowerCase();
            if (stateText.includes('true') || /checked|checked|selected|active/.test(stateText)) return true;
            const checked = target.querySelector?.('input:checked,[aria-checked="true"],[data-checked="true"],[data-selected="true"]');
            if (checked) return true;
          }
        }
      }
    }
    const radios = deepAll('input[type="radio"]', scope).filter(x => !x.closest(`#${panelId}`));
    const radio = radios.find(input => {
      const id = input.id;
      const linked = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      const candidates = [linked, input.closest('label'), input.parentElement, input.nextElementSibling, input.previousElementSibling]
        .filter(Boolean).map(node => norm(node.textContent || node.getAttribute?.('aria-label')));
      return candidates.some(value => value === wanted || value.startsWith(wanted));
    });
    if (radio) {
      const id = radio.id;
      const linked = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      (linked || radio.closest('label') || radio).click();
      if (!radio.checked) radio.click();
      radio.dispatchEvent(new Event('input', { bubbles:true }));
      radio.dispatchEvent(new Event('change', { bubbles:true }));
      await sleep(300);
      if (!radio.checked) throw new Error(`默认单选项未生效：${text}`);
      return true;
    }
    const fallback = chooseVisible(text);
    await sleep(350);
    return fallback;
  }
  function labelNode(words) {
    return textNodes().filter(node => words.some(word => norm(node.textContent) === norm(word)))
      .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length)[0]
      || textNodes().filter(node => words.some(word => norm(node.textContent).includes(norm(word))))
        .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length)[0];
  }
  function labeledContainer(words) {
    const label = labelNode(words);
    let current = label;
    for (let i = 0; current && i < 8; i += 1, current = current.parentElement) {
      if (current.querySelector?.('input,textarea,select,[role="combobox"],[contenteditable="true"]')) return current;
    }
    return label?.parentElement || null;
  }
  function setNativeValue(input, value) {
    if (!input) return false;
    input.focus();
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    setter?.call(input, value);
    input.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:value }));
    input.dispatchEvent(new Event('change', { bubbles:true }));
    return true;
  }
  async function chooseDropdownByPlaceholder(placeholderWords, option) {
    const input = fieldCandidates().find(x => placeholderWords.some(word => norm(x.placeholder).includes(norm(word))));
    if (!input) throw new Error(`找不到下拉输入框：${placeholderWords[0]}`);
    input.focus();
    input.click();
    await sleep(250);
    setNativeValue(input, option);
    input.dispatchEvent(new KeyboardEvent('keydown', { bubbles:true, key:'ArrowDown' }));
    input.dispatchEvent(new KeyboardEvent('keydown', { bubbles:true, key:'Enter' }));
    await sleep(350);
    clickOptionText(option);
    await sleep(300);
    const value = norm(input.value || input.textContent);
    const selected = textNodes().some(node => norm(node.textContent) === norm(option));
    if (!value.includes(norm(option)) && !selected) throw new Error(`下拉项未写入：${option}`);
  }
  async function chooseDropdownByLabel(labelWords, option) {
    const direct = fieldCandidates().find(input => labelWords.some(word => norm(input.placeholder).includes(norm(word))))
      || (labelWords.some(word => /语种/.test(word)) && fieldCandidates().find(input => /请选择语种/.test(input.placeholder || '')))
      || (labelWords.some(word => /曲风|标签/.test(word)) && fieldCandidates().find(input => /准确的标签|选择曲风/.test(input.placeholder || '')));
    const container = labeledContainer(labelWords);
    if (!direct && !container) throw new Error(`找不到下拉字段：${labelWords[0]}`);
    const control = direct || container.querySelector('[role="combobox"]') || Array.from(container.querySelectorAll('input,div,span'))
      .find(node => visible(node) && node !== container);
    const nativeSelect = direct?.matches?.('select') ? direct : container.querySelector('select');
    if (nativeSelect) {
      const wanted = norm(option);
      const choice = Array.from(nativeSelect.options).find(x => norm(x.textContent) === wanted || norm(x.textContent).includes(wanted));
      if (!choice) throw new Error(`下拉框没有选项：${option}`);
      nativeSelect.value = choice.value;
      nativeSelect.dispatchEvent(new Event('input', { bubbles:true }));
      nativeSelect.dispatchEvent(new Event('change', { bubbles:true }));
      await sleep(350);
      const selected = nativeSelect.options[nativeSelect.selectedIndex];
      if (!selected || !norm(selected.textContent).includes(wanted)) throw new Error(`下拉项未写入：${option}`);
      return;
    }
    (control || container).click();
    await sleep(250);
    if (control?.matches?.('input')) {
      setNativeValue(control, option);
      control.dispatchEvent(new KeyboardEvent('keydown', { bubbles:true, key:'ArrowDown' }));
      control.dispatchEvent(new KeyboardEvent('keydown', { bubbles:true, key:'Enter' }));
    }
    await sleep(300);
    if (!clickOptionText(option)) throw new Error(`下拉框没有选项：${option}`);
    await sleep(300);
    const current = String(control?.value || control?.textContent || container.textContent || '');
    if (!norm(current).includes(norm(option)) && !textNodes().some(node => norm(node.textContent) === norm(option))) throw new Error(`下拉项未写入：${option}`);
  }
  async function chooseAiType(option) {
    const wanted = norm(option);
    const deadline = Date.now() + 5000;
    let control = fieldCandidates().find(input => /请选择AI工具|选择AI工具/i.test(input.placeholder || '')) || null;
    while (Date.now() < deadline && !control) {
      control = deepAll('select').filter(visible).find(node => Array.from(node.options || []).some(item => norm(item.textContent).includes(wanted))) || null;
      if (control) break;
      const label = labelNode(['是否AI作品','AI作品']);
      let parent = label;
      for (let i = 0; parent && i < 6 && !control; i += 1, parent = parent.parentElement) {
        const controls = Array.from(parent.querySelectorAll('select,[role="combobox"],[aria-haspopup="listbox"],.ant-select,.ant-select-selector,.el-select,.select,.select-input'))
          .filter(visible).filter(node => !node.closest(`#${panelId}`));
        control = controls.find(node => !node.matches('input[type="radio"]')) || null;
      }
      if (!control) await sleep(250);
    }
    if (!control) throw new Error('选中“是”后未出现 AI 类型下拉框');
    if (control.matches('select')) {
      const choice = Array.from(control.options).find(node => norm(node.textContent) === wanted);
      if (!choice) throw new Error(`AI 类型下拉框没有选项：${option}`);
      control.value = choice.value;
      control.dispatchEvent(new Event('input', { bubbles:true }));
      control.dispatchEvent(new Event('change', { bubbles:true }));
      await sleep(300);
      if (norm(control.selectedOptions?.[0]?.textContent) !== wanted) throw new Error(`AI 类型未选中：${option}`);
      return;
    }
    const controlRoot = control.parentElement;
    const liveControl = () => control.isConnected ? control : controlRoot?.querySelector('input,select,[role="combobox"],[aria-haspopup="listbox"],.select,.select-input') || control;
    const menuOption = () => {
      const currentControl = liveControl();
      const box = currentControl.getBoundingClientRect();
      return deepAll('li,[role="option"],[role="menuitem"],div,span,p')
        .filter(node => visible(node) && !node.closest(`#${panelId}`) && norm(node.textContent) === wanted)
        .filter(node => {
          const rect = node.getBoundingClientRect();
          return rect.top >= box.bottom - 2 && rect.top < box.bottom + 220
            && rect.right > box.left && rect.left < box.right;
        })
        .sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0] || null;
    };
    const selected = () => {
      const currentControl = liveControl();
      const box = currentControl.getBoundingClientRect();
      const display = `${currentControl.value || ''} ${currentControl.textContent || ''} ${controlRoot?.textContent || ''}`;
      const placeholder = /请选择AI工具/.test(display);
      const inControl = deepAll('div,span,input', controlRoot || document).some(node => {
        if (!visible(node) || norm(node.value || node.textContent) !== wanted) return false;
        const rect = node.getBoundingClientRect();
        return rect.top >= box.top - 2 && rect.bottom <= box.bottom + 2 && rect.left >= box.left - 2 && rect.right <= box.right + 2;
      });
      return !menuOption() && !placeholder && (norm(control.value) === wanted || inControl || norm(display).includes(wanted));
    };
    if (selected()) return;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const currentControl = liveControl();
      currentControl.scrollIntoView({ block:'center', inline:'center' });
      if (!menuOption()) currentControl.click(); // One click opens the menu; a second click would close it.
      const limit = Date.now() + 2000;
      let choice = menuOption();
      while (!choice && Date.now() < limit) { await sleep(100); choice = menuOption(); }
      if (!choice) throw new Error(`点击“请选择AI工具”后没有出现：${option}`);
      const target = choice.closest('[role="option"],[role="menuitem"],li,[class*="option"],[class*="item"]') || choice;
      target.click(); // Select Suno AI once, after the menu is visibly open.
      await sleep(400);
      if (selected()) return;
    }
    throw new Error(`已点击 ${option}，但 AI 工具仍未选中`);
  }
  function fieldCandidates() { return deepAll('input,textarea,select,[contenteditable="true"]').filter(visible).filter(x => !x.closest(`#${panelId}`)); }
  function findField(words) {
    const candidates = fieldCandidates();
    const hit = candidates.find(input => words.some(word => norm(input.placeholder).includes(norm(word)) || norm(input.getAttribute('aria-label')).includes(norm(word))));
    if (hit) return hit;
    return candidates.find(input => { const parent = input.closest('label,div,section,form'); return words.some(word => norm(parent?.textContent).includes(norm(word))); });
  }
  function setField(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    if (input.matches('input,textarea')) input.select?.();
    if (input.matches('input,textarea')) {
      const setter = Object.getOwnPropertyDescriptor(input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value')?.set;
      setter?.call(input, value);
    } else input.textContent = value;
    input.dispatchEvent(new Event('beforeinput', { bubbles:true }));
    input.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:value }));
    input.dispatchEvent(new Event('change', { bubbles:true }));
    input.dispatchEvent(new KeyboardEvent('keyup', { bubbles:true, key:'End' }));
    input.dispatchEvent(new Event('blur', { bubbles:true }));
    return true;
  }
  async function applyDefaultChoices(item) {
    const m = item.metadata || {};
    await chooseRadio(m.original === 'cover' ? '翻唱' : '原唱');
    await chooseRadio(m.isAi === false ? '否' : '是', ['是否AI作品','AI作品']);
    if (m.isAi !== false) await chooseAiType(m.aiType || 'Suno AI');
    await chooseRadio(m.version || '录音室版');
    await chooseDropdownByLabel(['歌曲语种','语种'], m.language || '国语');
    await chooseDropdownByLabel(['曲风','准确的标签会帮助','标签'], m.genre || '流行 Pop');
  }
  async function loadDataUrl(file) {
    if (file?.dataUrl) return file.dataUrl;
    if (!file?.fileId) throw new Error('队列缺少本地文件引用');
    let offset = 0;
    let result = '';
    do {
      const part = await chrome.runtime.sendMessage({ type:'getFileChunk', fileId:file.fileId, offset, length:400000 });
      if (!part?.chunk) throw new Error(`读取文件失败：${file.name || file.fileId}`);
      result += part.chunk;
      offset = part.nextOffset;
      if (part.done) break;
    } while (true);
    return result;
  }
  function findAudioInput() {
    const fileInputs = deepAll('input[type="file"]');
    return fileInputs.find(x => /audio|mp3|wav|flac/i.test(`${x.accept} ${x.outerHTML}`))
      || fileInputs.find(x => /歌曲音频|音频|MP3|WAV|FLAC|歌曲文件/i.test(x.closest('label,form,section,div')?.textContent || ''));
  }
  function inputFile(file) {
    return new Promise(async (resolve, reject) => {
      const fileInputs = deepAll('input[type="file"]');
      const input = findAudioInput();
      if (!input) return reject(new Error(`页面没有可确认的音频文件框（共 ${fileInputs.length} 个文件框）；已停止，避免把音频传到封面或其他字段`));
      try {
        const response = await fetch(await loadDataUrl(file)); const blob = await response.blob();
        const local = new File([blob], file.name, { type: file.type || blob.type || 'audio/mpeg' });
        const transfer = new DataTransfer(); transfer.items.add(local); input.files = transfer.files;
        input.dispatchEvent(new Event('input', { bubbles:true }));
        input.dispatchEvent(new Event('change', { bubbles:true }));
        resolve();
      } catch (error) { reject(error); }
    });
  }
  async function waitForText(pattern, timeout = 30000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) { if (textNodes().some(x => pattern.test(x.textContent))) return true; await sleep(500); }
    return false;
  }
  async function waitForAudioReady(timeout = 120000) {
    const deadline = Date.now() + timeout;
    const pending = /请等待音频上传完毕|等待音频上传|正在上传|上传中|音频处理中|处理中/i;
    while (Date.now() < deadline) {
      const pendingVisible = textNodes().some(node => pending.test(node.textContent || ''));
      if (!pendingVisible) return true;
      await sleep(800);
    }
    return false;
  }
  async function waitForDrawer() {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (songEditorOpen()) return true;
      await sleep(400);
    }
    return false;
  }
  function songEditorOpen() {
    const creatorCount = fieldCandidates().filter(input => /输入艺人名|艺人id搜索/i.test(input.placeholder || '')).length;
    const hasAiField = !!labelNode(['是否AI作品','AI作品']);
    return hasAiField && creatorCount >= 3;
  }
  function findSongTitleField() {
    const direct = findField(['歌曲名','歌曲名称','歌名','title']);
    if (direct) return direct;
    return fieldCandidates()
      .filter(input => input.matches('input:not([type="file"]),textarea'))
      .filter(input => !/艺人名|艺人id搜索|翻译|标签|歌词/i.test(`${input.placeholder || ''} ${input.getAttribute('aria-label') || ''}`))
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0] || null;
  }
  async function openSongEditor(songTitle = '') {
    const deadline = Date.now() + 90000;
    let clickedTitle = false;
    while (Date.now() < deadline) {
      if (songEditorOpen()) return true;
      const edits = textNodes().filter(x => norm(x.textContent) === '编辑');
      const targets = [...new Set(edits.map(x => x.closest('button,[role="button"],a') || x))];
      if (targets.length >= 1) {
        const edit = targets[0];
        const target = edit.closest('button,[role="button"],a') || edit;
        target.scrollIntoView({ block:'center', inline:'center' });
        target.click();
        return await waitForDrawer();
      }
      if (songTitle && !clickedTitle) {
        const titleNode = textNodes().find(x => norm(x.textContent) === norm(songTitle));
        if (titleNode) {
          clickedTitle = true;
          const targets = [titleNode, titleNode.closest('button,[role="button"],a'), titleNode.parentElement, titleNode.parentElement?.parentElement]
            .filter(Boolean).filter((node, index, list) => list.indexOf(node) === index);
          for (const target of targets) {
            target.scrollIntoView({ block:'center', inline:'center' });
            target.click();
            await sleep(700);
            if (songEditorOpen()) return true;
          }
        }
      }
      await sleep(400);
    }
    return false;
  }
  async function uploadFromList(file) {
    if (!findAudioInput()) {
      const opened = clickText('点击或将歌曲文件拖到此处上传') || clickText('上传歌曲') || clickText('上传预售歌曲');
      if (opened) await sleep(500);
    }
    if (!findAudioInput()) return false;
    await inputFile(file);
    await sleep(1000);
    return true;
  }
  async function fillCreators(item) {
    const values = [item.metadata.singer, item.metadata.lyricist, item.metadata.composer];
    const creatorInputs = fieldCandidates().filter(input => /输入艺人名|艺人id搜索/i.test(input.placeholder || ''));
    const labels = ['演唱者', '词作者', '曲作者'];
    for (let index = creatorInputs.length; index < 3; index += 1) {
      const fallback = fieldCandidates().find(input => {
        let current = input;
        for (let i = 0; current && i < 5; i += 1, current = current.parentElement) {
          if (norm(current.textContent).includes(labels[index])) return true;
        }
        return false;
      });
      if (fallback && !creatorInputs.includes(fallback)) creatorInputs.push(fallback);
    }
    if (creatorInputs.length < 3) throw new Error(`只找到 ${creatorInputs.length} 个创制人输入框，至少需要 3 个`);
    const hasCreatorValue = (input, value) => {
      const wanted = norm(value);
      if (norm(input.value || input.textContent).includes(wanted)) return true;
      let current = input;
      for (let i = 0; current && i < 4; i += 1, current = current.parentElement) {
        if (norm(current.textContent).includes(wanted)) return true;
      }
      return false;
    };
    for (let index = 0; index < 3; index += 1) {
      const value = values[index];
      if (!value) throw new Error(`未提供第 ${index + 1} 个创制人名称`);
      const input = creatorInputs[index];
      if (hasCreatorValue(input, value)) continue;
      setField(input, '');
      setField(input, value);
      await sleep(300);
      if (!hasCreatorValue(input, value)) throw new Error(`第 ${index + 1} 个创制人输入未稳定为“${value}”`);
    }
  }
  async function processItem(item) {
    const m = item.metadata || {};
    status(`处理：${item.title}`); await report(item, { status:'running', step:'打开单曲编辑' });
    let audioUploaded = songEditorOpen();
    if (!audioUploaded) audioUploaded = await uploadFromList(item.audio);
    if (!(await openSongEditor(item.title))) throw new Error('音频已尝试上传，但未进入歌曲编辑表单；请确认上传完成后歌曲行已出现');
    await report(item, { status:'running', step:'等待音频上传完成' });
    if (!(await waitForAudioReady())) throw new Error('等待音频上传完成超时，页面仍显示“请等待音频上传完毕”');
    const title = findSongTitleField(); if (!setField(title, item.title)) throw new Error('找不到歌曲名输入框');
    if (!audioUploaded) {
      await report(item, { status:'running', step:'上传音频' });
      await inputFile(item.audio);
      if (!(await waitForText(/上传成功|已上传|上传完成|音频已就绪/i, 60000))) throw new Error('等待平台完成音频上传超时');
    }
    if (item.lyric) { await report(item, { status:'running', step:'填写歌词' }); const lyricField = findField(['歌词','lyrics']); if (!setField(lyricField, await (await fetch(await loadDataUrl(item.lyric))).text())) throw new Error('找不到歌词输入框'); }
    await report(item, { status:'running', step:'填写演唱者、词作者、曲作者' });
    await fillCreators(item);
    await report(item, { status:'running', step:'填写 AI 类型、语种、曲风等选项' });
    await applyDefaultChoices(item);
    await report(item, { status:'running', step:'基础信息已填写，等待提交确认' });
    if (m.autoSubmit) {
      if (!clickText('提交')) throw new Error('“提交”按钮不可用或未找到，请检查必填项');
      if (!(await waitForText(/提交成功|保存成功|上传成功|审核中|已提交/i, 20000))) throw new Error('点击提交后未观察到平台确认信息');
      await report(item, { status:'completed', step:'平台已确认提交', summary:`已提交：${item.title}` });
    } else await report(item, { status:'ready', step:'已填写，未自动提交', summary:`已准备：${item.title}` });
  }
  async function run() {
    stopRequested = false; mount();
    const state = await chrome.runtime.sendMessage({ type:'getState' });
    for (const item of state.queue || []) {
      if (stopRequested) break;
      if (!['pending','failed'].includes(item.status)) continue;
      try { await processItem(item); } catch (error) { status(`失败：${error.message}`); await report(item, { status:'failed', step:'已停止', error:error.message, summary:`失败：${item.title}` }); break; }
    }
    if (!stopRequested) status('本轮处理结束');
  }
  mount();
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === 'ping') { sendResponse({ ok: true, page: location.href }); return; }
    if (message.type === 'start') run();
    if (message.type === 'stop') { stopRequested = true; status('已停止'); }
  });
})();
