(function () {
  if (window.top !== window) return;

  let port = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let activeJobId = null;
  let observingJobId = null;
  let activeDownloadJobId = null;
  let observingDownloadJobId = null;
  let knownSongIds = new Set();
  // A Chrome extension reload can leave an older content-script instance in
  // the same document while the new instance is being injected. Keep one
  // execution per task across instances; otherwise both instances clear and
  // append the same lyrics concurrently.
  const EXECUTION_LOCK_KEY = "__hotspotSunoRunnerExecutionLock";
  const EXECUTION_LOCK_ATTR = "data-hotspot-suno-execution-lock";
  const FORM_RELOAD_MARKER = "hotspotSunoFormReloadFor";
  const FINAL_LYRICS_RELOAD_MARKER = "hotspotSunoFinalLyricsReloadFor";
  const RUNNER_VERSION = "1.8.61";
  const DOWNLOAD_EXECUTION_LOCK_KEY = "__hotspotSunoRunnerDownloadExecutionLock";
  const DOWNLOAD_EXECUTION_LOCK_ATTR = "data-hotspot-suno-download-execution-lock";
  let executionLockTimer = null;

  function acquireExecutionLock(taskId) {
    const root = document.documentElement;
    if (!root) return false;
    let current = null;
    try { current = JSON.parse(root.getAttribute(EXECUTION_LOCK_ATTR) || "null"); } catch (_error) {}
    if (current && current.taskId && current.taskId !== taskId && Date.now() - Number(current.at || 0) < 120000) return false;
    const write = () => root.setAttribute(EXECUTION_LOCK_ATTR, JSON.stringify({ taskId, at: Date.now() }));
    write();
    if (executionLockTimer) clearInterval(executionLockTimer);
    executionLockTimer = setInterval(write, 2000);
    return true;
  }

  function releaseExecutionLock(taskId) {
    if (executionLockTimer) { clearInterval(executionLockTimer); executionLockTimer = null; }
    const root = document.documentElement;
    if (!root) return;
    let current = null;
    try { current = JSON.parse(root.getAttribute(EXECUTION_LOCK_ATTR) || "null"); } catch (_error) {}
    if (current && current.taskId === taskId) root.removeAttribute(EXECUTION_LOCK_ATTR);
  }

  // Download commands can be redelivered when the extension reconnects or
  // when Suno remounts the song page. Keep the same task from entering the
  // visible Download flow twice, even if two content-script instances are
  // briefly alive in the same document.
  function acquireDownloadExecutionLock(taskId) {
    const root = document.documentElement;
    if (!root) return false;
    let current = null;
    try { current = JSON.parse(root.getAttribute(DOWNLOAD_EXECUTION_LOCK_ATTR) || "null"); } catch (_error) {}
    if (current && current.taskId && current.taskId !== taskId && Date.now() - Number(current.at || 0) < 30 * 60 * 1000) return false;
    root.setAttribute(DOWNLOAD_EXECUTION_LOCK_ATTR, JSON.stringify({ taskId, at: Date.now() }));
    return true;
  }

  function releaseDownloadExecutionLock(taskId) {
    const root = document.documentElement;
    if (!root) return;
    let current = null;
    try { current = JSON.parse(root.getAttribute(DOWNLOAD_EXECUTION_LOCK_ATTR) || "null"); } catch (_error) {}
    if (current && current.taskId === taskId) root.removeAttribute(DOWNLOAD_EXECUTION_LOCK_ATTR);
  }
  const cancelledJobs = new Set();
  const abortReportedJobs = new Set();
  const cancelledDownloadJobs = new Set();
  const downloadAbortReportedJobs = new Set();
  // Avoid immediately redispatching a form item after a transient React
  // remount. Otherwise the same lyric payload can be pasted repeatedly.
  const formRetryAfter = new Map();
  // Suno's counter can include formatting and stale DOM text while React is
  // reconciling the contenteditable. Keep the payload below the boundary and
  // never click Create until the visible editor has been read back.
  // Leave a large margin for Suno's counter, which can count line breaks and
  // transient React formatting differently from JavaScript string.length.
  // The source prompt remains untouched; this is only the visible submission
  // payload.
  const MAX_LYRICS_CHARS = 3200;
  const MAX_STYLE_CHARS = 600;
  const MAX_PAGE_COUNTER_CHARS = 4300;
  const EDITOR_CHUNK_CHARS = 120;
  const EDITOR_RESET_TIMEOUT_MS = 9000;
  const EDITOR_RESET_SETTLE_MS = 260;
  const EDITOR_RESET_STABLE_READS = 4;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  function safeSunoText(value, limit) {
    const text = String(value == null ? "" : value);
    if (text.length <= limit) return text;
    const cut = text.slice(0, limit);
    const boundary = Math.max(cut.lastIndexOf("\n\n"), cut.lastIndexOf("\n"));
    const result = (boundary >= Math.floor(limit * 0.7) ? cut.slice(0, boundary) : cut).trimEnd();
    return result.length <= limit ? result : result.slice(0, limit).trimEnd();
  }

  function visibleText() {
    return document.body ? document.body.innerText.toLowerCase() : "";
  }

  function blocker() {
    const text = visibleText();
    const patterns = [
      "captcha", "verify you are human", "验证你是人类", "安全验证", "登录后继续",
      "sign in to continue", "log in to continue", "out of credits", "insufficient credits",
      "limit reached", "额度不足", "达到上限", "风控"
    ];
    return patterns.find(pattern => text.includes(pattern)) || null;
  }

  function isValueControl(element) {
    return Boolean(element && (
      element instanceof HTMLTextAreaElement ||
      element instanceof HTMLInputElement
    ));
  }

  function setTextControl(element, value, blur = true) {
    if (!element) return;
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
    try {
      element.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true,
        composed: true,
        inputType: "insertText",
        data: String(value)
      }));
    } catch (_error) {}
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      composed: true,
      inputType: "insertText",
      data: String(value)
    }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    if (blur) element.blur();
  }

  function normalizeEditableText(value) {
    return String(value == null ? "" : value)
      .replace(/\u00a0/g, " ")
      .replace(/\u200b/g, "")
      .replace(/\r\n?/g, "\n")
      .replace(/\n+$/g, "")
      .trimEnd();
  }

  function editableText(element) {
    if (!element) return "";
    if (isValueControl(element)) return normalizeEditableText(element.value);
    const visible = typeof element.innerText === "string" ? element.innerText : "";
    return normalizeEditableText(visible || element.textContent || "");
  }

  function equivalentEditableText(actual, expected) {
    const left = normalizeEditableText(actual);
    const right = normalizeEditableText(expected);
    if (left === right) return true;
    // Some contenteditable implementations represent line breaks as nested
    // div/br nodes. Ignore only those representation differences, never text.
    return left.replace(/\n/g, "") === right.replace(/\n/g, "");
  }

  function dispatchEditableEvent(element, type, inputType, data = "") {
    try {
      element.dispatchEvent(new InputEvent(type, {
        bubbles: true,
        composed: true,
        inputType,
        data
      }));
    } catch (_error) {
      element.dispatchEvent(new Event(type, { bubbles: true, composed: true }));
    }
  }

  function selectEditableContents(element) {
    element.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
    return selection;
  }

  function placeCaretAtEnd(element) {
    element.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function clearEditable(element) {
    if (!element) return false;
    if (isValueControl(element)) {
      setTextControl(element, "", false);
      return editableText(element) === "";
    }
    // Use the browser editing command against the live Lexical selection.
    // Dispatching a synthetic beforeinput before the command can make
    // Lexical reconcile the old selection and restore the old draft.
    for (let pass = 0; pass < 2 && editableText(element); pass += 1) {
      selectEditableContents(element);
      try { document.execCommand("selectAll", false); } catch (_error) {}
      try { document.execCommand("delete", false); } catch (_error) {}
      // Some Lexical builds ignore execCommand(delete) but accept replacing
      // the selected range with an empty string. This updates the editor
      // state without touching its DOM directly.
      if (editableText(element)) {
        selectEditableContents(element);
        try { document.execCommand("insertText", false, ""); } catch (_error) {}
      }
    }
    if (editableText(element)) return false;
    placeCaretAtEnd(element);
    return editableText(element) === "";
  }

  function emptyLyricsState(element) {
    const text = editableText(element);
    const counter = visibleLyricsCounter(element);
    return {
      empty: text === "",
      text,
      counter,
      counterEmpty: counter == null || counter === 0
    };
  }

  async function clearLyricsCompletely(fallback = null, timeoutMs = EDITOR_RESET_TIMEOUT_MS) {
    const deadline = Date.now() + timeoutMs;
    let current = await waitForLiveLyricsEditor(fallback);
    let lastState = { empty: false, text: "", counter: null, counterEmpty: false };
    let stableReads = 0;
    let stableNode = null;
    while (current && Date.now() < deadline) {
      clearEditable(current);
      await sleep(EDITOR_RESET_SETTLE_MS);
      // Prefer a newly mounted empty editor over the old focused node. This is
      // important when React leaves the old contenteditable connected during a
      // reconciliation pass.
      current = await waitForLiveLyricsEditor(current, 1400);
      if (!current) break;
      lastState = emptyLyricsState(current);
      if (lastState.empty && lastState.counterEmpty) {
        stableReads = current === stableNode ? stableReads + 1 : 1;
        stableNode = current;
        // Require several reads on the same live node. React may repaint the
        // old value shortly after the first input event, especially on the
        // third queue item after the page has navigated/reconciled.
        if (stableReads >= EDITOR_RESET_STABLE_READS) {
          return { ok: true, element: current, ...lastState };
        }
        await sleep(EDITOR_RESET_SETTLE_MS);
        current = await waitForLiveLyricsEditor(current, 1400);
        if (!current) break;
        lastState = emptyLyricsState(current);
      } else {
        stableReads = 0;
        stableNode = null;
      }
      await sleep(180);
    }
    if (current) lastState = emptyLyricsState(current);
    return { ok: false, element: current, ...lastState };
  }

  async function clearFormForRetry(editor = null, style = null, titleInput = null) {
    const reset = await clearLyricsCompletely(editor);
    const liveStyle = styleEditor() || style;
    const liveTitleInput = titleEditor() || titleInput;
    if (liveStyle) {
      if (isValueControl(liveStyle)) setTextControl(liveStyle, "");
      else clearEditable(liveStyle);
    }
    if (liveTitleInput) setTextControl(liveTitleInput, "");
    await sleep(EDITOR_RESET_SETTLE_MS);
    return reset;
  }

  async function requeueAfterFormFailure(command, reason, editor = null, style = null, titleInput = null) {
    // A Create page can briefly expose Simple mode while Advanced/Lyrics and
    // Styles are still mounting. Treat that as retryable: blocking here stops
    // the whole queue and prevents the next song page from ever opening.
    // Preserve the visible page; the next poll will retry after the page has
    // settled. Permanent blockers (login, CAPTCHA, quota, etc.) are handled
    // separately by blocker() and remain blocked.
    send("/v1/browser/error", {
      task_id: command.task_id,
      queue_path: command.queue_path,
      action: "requeue",
      reason
    });
  }

  function insertEditableChunk(element, chunk) {
    if (isValueControl(element)) {
      const current = editableText(element);
      setTextControl(element, current + chunk, false);
      return true;
    }
    placeCaretAtEnd(element);
    let inserted = false;
    try { inserted = document.execCommand("insertText", false, chunk); } catch (_error) {}
    // Do not replace Lexical DOM directly: that only changes the screen and
    // leaves Suno's internal editor state empty, causing an immediate rollback.
    if (!inserted) return false;
    // execCommand already emits the beforeinput/input sequence consumed by
    // Lexical. Dispatching another input with the same payload duplicates the
    // document (the observed "plain + formatted" lyric pair).
    return true;
  }

  async function writeStyleEditable(element, value, limit = MAX_STYLE_CHARS) {
    const target = safeSunoText(value, limit);
    if (!element || hasLyricsDescriptor(element)) return { ok: false, value: "", element: null };
    if (isValueControl(element)) {
      setTextControl(element, target, false);
      const actual = editableText(element);
      return { ok: equivalentEditableText(actual, target), value: actual, element };
    }
    if (!clearEditable(element)) return { ok: false, value: editableText(element), element };
    for (let offset = 0; offset < target.length; offset += EDITOR_CHUNK_CHARS) {
      insertEditableChunk(element, target.slice(offset, offset + EDITOR_CHUNK_CHARS));
    }
    await sleep(180);
    const actual = editableText(element);
    return {
      ok: equivalentEditableText(actual, target) && actual.length <= limit,
      value: actual,
      element
    };
  }

  async function waitForLiveLyricsEditor(fallback = null, timeoutMs = 2500, expectedText = null) {
    const deadline = Date.now() + timeoutMs;
    let current = liveLyricsEditor(fallback, expectedText);
    while (!current && Date.now() < deadline) {
      await sleep(100);
      current = liveLyricsEditor(fallback, expectedText);
    }
    return current;
  }

  async function writeEditable(element, value, limit, alreadyReset = false) {
    const target = safeSunoText(value, limit);
    let current = await waitForLiveLyricsEditor(element);
    // A failed write must stop immediately. Retrying clears and pastes the
    // same item again, which caused the observed paste/delete loop.
    for (let attempt = 1; attempt <= 1; attempt += 1) {
      if (!current) {
        current = await waitForLiveLyricsEditor(null, 2500, "");
        if (!current) return { ok: false, value: "", attempts: attempt, element: null };
      }

      // React can replace the contenteditable during navigation or after the
      // previous Create. Re-resolve the visible node before every mutation.
      current = await waitForLiveLyricsEditor(current);
      if (!current) return { ok: false, value: "", attempts: attempt, element: null };
      if (!alreadyReset) {
        const reset = await clearLyricsCompletely(current);
        current = reset.element || await waitForLiveLyricsEditor(current, 1400);
        if (!reset.ok || !current) {
          await sleep(300);
          continue;
        }
      }

      // Insert the complete lyric text once, then require stable readback on
      // the same live editor before allowing the form to advance.
      current = await waitForLiveLyricsEditor(current, 1800, "");
      if (!current) return { ok: false, value: "", attempts: attempt, element: null };
      if (!insertEditableChunk(current, target)) {
        return { ok: false, value: editableText(current), attempts: attempt, element: current };
      }
      let stable = 0;
      let lastNode = null;
      for (let read = 0; read < 12; read += 1) {
        await sleep(180);
        const candidate = await waitForLiveLyricsEditor(current, 700, target);
        const actual = editableText(candidate);
        const counter = visibleLyricsCounter(candidate);
        const valid = candidate && equivalentEditableText(actual, target) &&
          actual.length <= limit && (counter == null || counter <= MAX_PAGE_COUNTER_CHARS);
        if (valid && candidate === lastNode) stable += 1;
        else if (valid) { stable = 1; lastNode = candidate; }
        else stable = 0;
        if (stable >= 3) return { ok: true, value: actual, attempts: attempt, element: candidate };
      }
      current = await waitForLiveLyricsEditor(current, 900, target);
      if (current) await sleep(350);
    }
    current = await waitForLiveLyricsEditor(current, 1400);
    return { ok: false, value: editableText(current), attempts: 2, element: current };
  }

  const LYRICS_COUNTER_RE = /(?:^|\D)(\d{1,5})\s*\/\s*5000(?:\D|$)/;
  const MAX_COUNTER_DISTANCE_PX = 900;

  function counterValueFromText(value) {
    const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
    // A counter node is deliberately kept short. This excludes body/form
    // containers whose text also happens to contain an old `5000/5000`.
    if (!text || text.length > 80) return null;
    const match = text.match(LYRICS_COUNTER_RE);
    return match ? Number(match[1]) : null;
  }

  function counterValueFromElement(element) {
    if (!element) return null;
    // A form/card wrapper can contain the editor and several historical
    // counters. It is not a counter itself, even when its combined text is
    // short enough to match the expression below.
    if (element.matches && element.matches('[contenteditable="true"], textarea, input')) return null;
    if (element.querySelector && element.querySelector('[contenteditable="true"], textarea, input')) return null;
    const values = [
      element.getAttribute && element.getAttribute("aria-label"),
      element.getAttribute && element.getAttribute("title"),
      element.textContent
    ];
    for (const value of values) {
      const parsed = counterValueFromText(value);
      if (parsed != null) return parsed;
    }
    return null;
  }

  function rectDistance(first, second) {
    if (!first || !second) return Number.POSITIVE_INFINITY;
    const firstRect = first.getBoundingClientRect();
    const secondRect = second.getBoundingClientRect();
    const dx = Math.max(firstRect.left - secondRect.right, secondRect.left - firstRect.right, 0);
    const dy = Math.max(firstRect.top - secondRect.bottom, secondRect.top - firstRect.bottom, 0);
    return Math.sqrt((dx * dx) + (dy * dy));
  }

  function counterCandidates(scope, seen) {
    const nodes = [scope, ...Array.from(scope.querySelectorAll("*"))];
    return nodes.filter(element => {
      if (seen.has(element) || !isVisible(element)) return false;
      seen.add(element);
      return counterValueFromElement(element) != null;
    });
  }

  function visibleLyricsCounter(editor = null) {
    // Never scan document.body.innerText here. Suno keeps old/reconciled
    // editor fragments in the DOM, and a stale `5000/5000` from one of those
    // fragments incorrectly blocked the third queue item.
    const target = editor && isVisible(editor) ? editor : lyricsEditor();
    if (!target) return null;

    let scope = target;
    const seen = new Set();
    for (let depth = 0; scope && scope !== document.body && depth < 12; depth += 1) {
      const candidates = counterCandidates(scope, seen)
        .map(element => ({ element, value: counterValueFromElement(element), distance: rectDistance(target, element) }))
        .filter(item => item.value != null && item.distance <= MAX_COUNTER_DISTANCE_PX)
        .sort((left, right) => left.distance - right.distance);
      if (candidates.length) return candidates[0].value;
      scope = scope.parentElement;
    }
    // A counter can be rendered in a portal outside the form. Only accept a
    // nearby, short, visible counter; unrelated page counters remain ignored.
    const nearby = counterCandidates(document.body, seen)
      .map(element => ({ value: counterValueFromElement(element), distance: rectDistance(target, element) }))
      .filter(item => item.value != null && item.distance <= MAX_COUNTER_DISTANCE_PX)
      .sort((left, right) => left.distance - right.distance);
    return nearby.length ? nearby[0].value : null;
  }

  function validateForm(editor, style, lyrics, stylePrompt) {
    const actualLyrics = editableText(editor);
    const actualStyle = editableText(style);
    const counter = visibleLyricsCounter(editor);
    return {
      ok: equivalentEditableText(actualLyrics, lyrics) &&
        actualLyrics.length <= MAX_LYRICS_CHARS &&
        equivalentEditableText(actualStyle, stylePrompt) &&
        actualStyle.length <= MAX_STYLE_CHARS &&
        (counter == null || counter <= MAX_PAGE_COUNTER_CHARS),
      actualLyrics,
      actualStyle,
      counter,
      lyrics_match: equivalentEditableText(actualLyrics, lyrics),
      style_match: equivalentEditableText(actualStyle, stylePrompt)
    };
  }

  function lyricsEditorCandidates() {
    const selectors = [
      '[aria-label="Lyrics editor"][contenteditable="true"]',
      '[placeholder*="lyrics" i][contenteditable="true"]',
      '[data-placeholder*="lyrics" i][contenteditable="true"]',
      '[contenteditable="true"][role="textbox"]',
      '[role="textbox"][contenteditable="true"]',
      '.lyrics-editor-content[contenteditable="true"]',
      '[contenteditable="true"]',
      'textarea'
    ];
    const candidates = [];
    selectors.forEach(selector => {
      document.querySelectorAll(selector).forEach(element => {
        if (!candidates.includes(element) && isLikelyLyricsEditor(element)) candidates.push(element);
      });
    });
    return candidates.sort((left, right) => lyricsEditorScore(left) - lyricsEditorScore(right));
  }

  function editorDescriptor(element) {
    if (!element) return "";
    return [
      element.getAttribute && element.getAttribute("aria-label"),
      element.getAttribute && element.getAttribute("placeholder"),
      element.getAttribute && element.getAttribute("data-placeholder"),
      element.getAttribute && element.getAttribute("name"),
      typeof element.className === "string" ? element.className : ""
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function nearbyLabelDescriptor(element) {
    // Lyrics and Styles share several ancestors in the current Suno form.
    // Using an ancestor's full textContent therefore makes both editors look
    // like both fields and can reject the correct target.
    const values = [];
    const add = value => {
      const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
      if (text && text.length <= 80) values.push(text);
    };
    if (!element) return "";
    ["aria-label", "title", "data-testid", "data-placeholder", "placeholder", "name"].forEach(name => {
      add(element.getAttribute && element.getAttribute(name));
    });
    let current = element;
    for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
      ["aria-label", "title", "data-testid", "data-placeholder"].forEach(name => {
        add(current.getAttribute && current.getAttribute(name));
      });
      const siblings = current.parentElement ? Array.from(current.parentElement.children) : [];
      siblings.forEach(sibling => {
        if (sibling === current || sibling.matches?.("textarea, input, [contenteditable=\"true\"], [role=\"textbox\"]")) return;
        const text = normalizeEditableText(sibling.textContent);
        if (/^(lyrics?|styles?|style of music|歌词|风格)$/i.test(text)) add(text);
      });
    }
    return values.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function directEditorDescriptor(element) {
    // Do not use sibling labels here.  Lyrics and Styles are rendered in one
    // shared card, so a broad ancestor can expose both labels and make every
    // editor look like both fields.  Only the editor and its own small field
    // wrappers are valid semantic evidence; geometry handles anonymous nodes.
    if (!element) return "";
    const values = [editorDescriptor(element)];
    let current = element.parentElement;
    for (let depth = 0; current && depth < 2; depth += 1, current = current.parentElement) {
      ["aria-label", "title", "data-testid", "data-placeholder", "placeholder", "name"].forEach(name => {
        const value = current.getAttribute && current.getAttribute(name);
        if (value) values.push(String(value));
      });
    }
    return values.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function isStyleCandidate(element) {
    if (!element || (!isValueControl(element) && element.getAttribute("contenteditable") !== "true" && element.getAttribute("role") !== "textbox")) {
      return false;
    }
    const descriptor = directEditorDescriptor(element);
    return /(^|\s)(styles?|style of music|sound|music style|genre|turntable scratches|melodic ballad|describe)(\s|$)/i.test(descriptor) &&
      !/lyrics|lyric|歌词/i.test(descriptor);
  }

  function hasLyricsDescriptor(element) {
    if (!element) return false;
    const descriptor = directEditorDescriptor(element);
    return /lyrics|lyric|歌词/i.test(descriptor);
  }

  function fieldEditorNearLabel(labelPattern) {
    const labels = Array.from(document.querySelectorAll("body *")).filter(element => {
      if (!isVisible(element) || element.matches("textarea, input, [contenteditable=\"true\"]")) return false;
      const text = normalizeEditableText(element.textContent).toLowerCase();
      return labelPattern.test(text) && text.length <= 40;
    });
    for (const label of labels) {
      let scope = label.parentElement;
      for (let depth = 0; scope && scope !== document.body && depth < 6; depth += 1, scope = scope.parentElement) {
        const editors = Array.from(scope.querySelectorAll(
          "textarea, [contenteditable=\"true\"], [role=\"textbox\"]"
        )).filter(element => isVisible(element) && element.isConnected);
        if (!editors.length) continue;
        const filtered = editors.filter(element => labelPattern.source.includes("style")
          ? !hasLyricsDescriptor(element)
          : !isStyleCandidate(element));
        if (filtered.length) {
          const labelRect = label.getBoundingClientRect();
          return filtered.sort((left, right) => {
            const leftRect = left.getBoundingClientRect();
            const rightRect = right.getBoundingClientRect();
            const leftDistance = Math.hypot(leftRect.left - labelRect.left, leftRect.top - labelRect.top);
            const rightDistance = Math.hypot(rightRect.left - labelRect.left, rightRect.top - labelRect.top);
            return leftDistance - rightDistance;
          })[0];
        }
      }
    }
    return null;
  }

  function isLikelyLyricsEditor(element) {
    if (!element || (!isValueControl(element) && element.getAttribute("contenteditable") !== "true")) return false;
    const descriptor = editorDescriptor(element);
    // The Advanced editor also contains a separate "Cowriter prompt" input.
    // It is a textarea and does not carry the word "style", so treating every
    // non-Styles textarea as Lyrics sends the song lyrics to the wrong field.
    // Only an explicitly lyric-labelled control may use this fallback path.
    if (/cowriter|ask anything|lyrics prompt|\bprompt\b/.test(descriptor) &&
        !/lyrics|lyric|歌词/.test(descriptor)) {
      return false;
    }
    if (/exclude|style of music|describe the sound|music style|genre|turntable scratches|melodic ballad/.test(descriptor) &&
        !/lyrics|lyric|歌词/.test(descriptor)) {
      return false;
    }
    if (isStyleCandidate(element)) return false;
    // Explicit lyrics hints are preferred, but a generic contenteditable is
    // still accepted because Suno has used several unlabeled editor nodes.
    // The visible-page check later prevents a hidden, reconciled old editor
    // from being selected.
    return true;
  }

  function lyricsEditorScore(element) {
    const descriptor = editorDescriptor(element);
    let score = 10;
    if (/lyrics|lyric|歌词/.test(descriptor)) score -= 8;
    if (element.getAttribute("role") === "textbox") score -= 1;
    if (element.getAttribute("aria-label") === "Lyrics editor") score -= 4;
    return score;
  }

  function lyricsEditor() {
    // Current Suno Advanced UI exposes a stable, exact label. Prefer it over
    // the broad nearby-label heuristic, which can select a stale editor while
    // React is reconciling the form.
    const exact = document.querySelector('[aria-label="Lyrics editor"][contenteditable="true"]');
    if (exact && isVisible(exact) && exact.isConnected) return exact;
    const labelled = fieldEditorNearLabel(/^lyrics?$/i);
    if (labelled) return labelled;
    const candidates = lyricsEditorCandidates()
      .filter(element => isVisible(element) && element.isConnected);
    if (!candidates.length) return null;
    const style = styleEditor();
    return candidates.sort((left, right) => {
      // The current Suno form can temporarily keep an old labelled editor
      // connected while mounting a new, less-labelled editor. The editor
      // nearest the current Styles control is the safer form anchor; use
      // semantic labels only as the tie-breaker.
      const distanceDelta = rectDistance(left, style) - rectDistance(right, style);
      if (distanceDelta) return distanceDelta;
      return lyricsEditorScore(left) - lyricsEditorScore(right);
    })[0];
  }

  function liveLyricsEditor(fallback = null, expectedText = null) {
    const exact = document.querySelector('[aria-label="Lyrics editor"][contenteditable="true"]');
    if (exact && isVisible(exact) && exact.isConnected) {
      if (expectedText === null || equivalentEditableText(editableText(exact), expectedText)) return exact;
    }
    const labelled = fieldEditorNearLabel(/^lyrics?$/i);
    const candidates = lyricsEditorCandidates()
      .filter(element => isVisible(element) && element.isConnected);
    if (expectedText !== null) {
      const matching = candidates.filter(element => equivalentEditableText(editableText(element), expectedText));
      if (matching.length) {
        const style = styleEditor();
        return matching.sort((left, right) => rectDistance(left, style) - rectDistance(right, style))[0];
      }
      // An expected prefix is a strict identity check. Never fall back to an
      // arbitrary focused/visible editor here, or a stale third-task node can
      // receive the next chunk and push the page to 5000/5000.
      return null;
    }
    if (labelled) return labelled;
    const current = lyricsEditor();
    if (current && isVisible(current) && current.isConnected) return current;
    return fallback && fallback.isConnected && isVisible(fallback) ? fallback : null;
  }

  function styleEditor() {
    const stylePattern = /styles?|style of music|sound|music|genre|turntable scratches|melodic ballad|describe/;
    const explicitStyleSelectors = [
      '[aria-label="Styles"] textarea',
      '[aria-label="Styles"] [contenteditable="true"]',
      '[aria-label="Styles"] [role="textbox"]',
      '[aria-label*="style" i] textarea',
      '[aria-label*="style" i][contenteditable="true"]',
      '[aria-label*="style" i][role="textbox"]',
      'textarea[placeholder*="style" i]',
      '[contenteditable="true"][data-placeholder*="style" i]',
      '[role="textbox"][data-placeholder*="style" i]'
    ];
    const allCandidates = Array.from(document.querySelectorAll(
      "textarea, [contenteditable=\"true\"], [role=\"textbox\"]"
    ));
    const candidates = allCandidates.filter(element => {
      if (!isVisible(element)) return false;
      // Do not use nearbyLabelDescriptor here: Suno renders Lyrics and Styles
      // in one shared card, so a Styles textarea can have a nearby Lyrics
      // sibling label. Broad sibling text caused the real Styles control to
      // be filtered out and the fallback to return the Lyrics editor.
      const descriptor = directEditorDescriptor(element);
      return !/exclude|lyrics|lyric|歌词/.test(descriptor);
    });
    const labelledContainers = Array.from(document.querySelectorAll(
      '[aria-label="Styles"], [data-testid*="style" i], [data-testid*="styles" i]'
    )).filter(isVisible);
    for (const selector of explicitStyleSelectors) {
      const explicit = Array.from(document.querySelectorAll(selector)).find(element =>
        isVisible(element) && !/lyrics|lyric|歌词/i.test(editorDescriptor(element))
      );
      if (explicit) return explicit;
    }
    // In the current Advanced form the Styles control is a plain textarea
    // whose placeholder is a rotating example and whose parent has no stable
    // aria/data-testid.  Select that textarea before generic fallbacks: the
    // other visible textareas are Cowriter, lyrics prompt, or Exclude styles.
    const semanticTextareas = candidates.filter(element => {
      if (!(element instanceof HTMLTextAreaElement) || hasLyricsDescriptor(element)) return false;
      const descriptor = directEditorDescriptor(element);
      const placeholder = String(element.getAttribute("placeholder") || "").toLowerCase();
      return !/cowriter|ask anything|lyrics prompt|exclude|describe the sound|modern ballad song|prompt/.test(descriptor) &&
        !/describe the sound|modern ballad song|exclude styles/.test(placeholder);
    });
    if (semanticTextareas.length === 1) return semanticTextareas[0];
    // In the current Advanced UI the Styles textarea has no aria-label and
    // its placeholder is a rotating recommendation, so the old broad label
    // lookup is unsafe: the Styles header's ancestor also contains Lyrics.
    // Anchor to the exact visible Styles section control and select the
    // nearest textarea below it, excluding Cowriter/Prompt/Lyrics controls.
    const styleAnchors = Array.from(document.querySelectorAll('button,[role="button"]'))
      .filter(element => {
        if (!isVisible(element)) return false;
        const label = normalizeEditableText(element.getAttribute("aria-label") || "").toLowerCase();
        const text = normalizeEditableText(element.textContent).toLowerCase();
        return /^styles?\b/.test(label) || text === "styles";
      });
    const unlabelledControls = allCandidates.filter(element => {
      if (!isVisible(element) || hasLyricsDescriptor(element)) return false;
      const descriptor = directEditorDescriptor(element);
      if (/cowriter|ask anything|lyrics prompt|\bprompt\b|exclude/i.test(descriptor)) return false;
      return element instanceof HTMLTextAreaElement || element.getAttribute("contenteditable") === "true" || element.getAttribute("role") === "textbox";
    });
    for (const anchor of styleAnchors) {
      const ar = anchor.getBoundingClientRect();
      const below = unlabelledControls
        .filter(element => element !== anchor && element.getBoundingClientRect().top >= ar.bottom - 20)
        .sort((left, right) => Math.abs(left.getBoundingClientRect().top - ar.bottom) - Math.abs(right.getBoundingClientRect().top - ar.bottom));
      if (below[0]) return below[0];
    }
    // Prefer the actual editable descendant of the labelled Styles field.
    // This avoids selecting a generic textarea or the lyrics editor while
    // React is mounting both controls at the same time.
    const direct = allCandidates.find(element =>
      isVisible(element) && labelledContainers.some(container => container.contains(element)) &&
      !/lyrics|lyric|歌词/i.test(editorDescriptor(element))
    );
    if (direct) return direct;
    const labelled = candidates.filter(element =>
      labelledContainers.some(container => container === element || container.contains(element)) &&
      !hasLyricsDescriptor(element)
    );
    if (labelled.length) return labelled[0];
    const preferred = candidates.filter(element => stylePattern.test(
      `${editorDescriptor(element)} ${nearbyLabelDescriptor(element)}`
    ) && !hasLyricsDescriptor(element));
    const textareaFallback = candidates.find(element =>
      element instanceof HTMLTextAreaElement && !hasLyricsDescriptor(element)
    );
    if (preferred[0] || textareaFallback) return preferred[0] || textareaFallback;

    // The current Create UI can render both editors as anonymous divs.  In
    // that version neither control has a useful aria-label or placeholder,
    // so semantic matching above cannot identify Styles.  Use geometry only
    // as a tightly-scoped fallback: find the nearest distinct editable to
    // the positively identified Lyrics editor, and accept it only when it is
    // not marked as Lyrics.  Never fall back to an arbitrary single editor.
    const lyric = lyricsEditorCandidates().find(element =>
      isVisible(element) && element.isConnected && hasLyricsDescriptor(element)
    );
    if (lyric) {
      const distinct = candidates
        .filter(element => element !== lyric && !hasLyricsDescriptor(element))
        .sort((left, right) => rectDistance(lyric, left) - rectDistance(lyric, right));
      if (distinct.length) return distinct[0];
    }
    return null;
  }

  function distinctStyleEditor(lyric) {
    const textareas = Array.from(document.querySelectorAll("textarea"))
      .filter(element => isVisible(element) && element.isConnected && element !== lyric)
      .filter(element => {
        const descriptor = `${directEditorDescriptor(element)} ${String(element.getAttribute("placeholder") || "")}`.toLowerCase();
        return !/cowriter|ask anything|lyrics|lyric|exclude styles|describe the sound|prompt/.test(descriptor);
      });
    if (!textareas.length) return null;
    // In the current Advanced form Styles is the only normal textarea below
    // the visible Styles button. Prefer that geometric relationship.
    const anchors = Array.from(document.querySelectorAll("button,[role=\"button\"]"))
      .filter(element => isVisible(element) && normalizeEditableText(element.textContent).toLowerCase() === "styles");
    for (const anchor of anchors) {
      const ar = anchor.getBoundingClientRect();
      const below = textareas
        .filter(element => element.getBoundingClientRect().top >= ar.bottom - 20)
        .sort((a, b) => Math.abs(a.getBoundingClientRect().top - ar.bottom) - Math.abs(b.getBoundingClientRect().top - ar.bottom));
      if (below[0]) return below[0];
    }
    // If the form is scrolled, the Styles value is commonly the only
    // non-empty eligible textarea. This remains distinct from Lyrics.
    return textareas.find(element => normalizeEditableText(element.value)) || textareas[0];
  }

  function titleEditor() {
    return Array.from(document.querySelectorAll('input[placeholder="Song Title (Optional)"]'))
      .find(element => element.getClientRects().length > 0) || null;
  }

  function clearAllFormInputsButton() {
    return Array.from(document.querySelectorAll('button,[role="button"]')).find(button => {
      if (!isVisible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
      return /clear all form inputs|clear all/i.test(elementLabel(button));
    }) || null;
  }

  function newDraftButton() {
    return Array.from(document.querySelectorAll('button,[role="button"]')).find(button => {
      if (!isVisible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
      return /^new draft$/i.test(elementLabel(button));
    }) || null;
  }

  async function resetSunoFormBeforeTask() {
    let editor = await waitForLiveLyricsEditor(null, 2500);
    // Suno's New draft resets Lexical's internal document, while clearing the
    // visible DOM alone can leave the old draft to reappear on the next
    // insert. Only use it when an old lyric payload is actually present.
    if (editor && editableText(editor)) {
      const draft = newDraftButton();
      if (draft) {
        draft.click();
        await sleep(900);
      }
    }
    const button = clearAllFormInputsButton();
    if (button) {
      button.click();
      await sleep(700);
    }
    editor = await waitForLiveLyricsEditor(null, 2500);
    const style = styleEditor();
    // Suno disables Clear all form inputs when the form is already empty,
    // especially immediately after a Create-page reload. That is a valid
    // clean state and must not block the first write of the new task.
    if (editor && editableText(editor) === "" && (!style || editableText(style) === "")) {
      return true;
    }
    if (!button) return Boolean(editor && editableText(editor) === "");
    return Boolean(editor && editableText(editor) === "");
  }

  async function resetLyricsBeforeFinalWrite(fallback = null, taskId = "", allowReload = true) {
    let editor = await waitForLiveLyricsEditor(fallback, 2500);
    if (!editor) return { ok: false, element: null };
    if (!allowReload) {
      // This is the continuation after the authoritative New draft reload.
      // Do not call the ordinary DOM clear again: it can reintroduce the old
      // Lexical document. Only accept the freshly reloaded empty editor.
      const state = emptyLyricsState(editor);
      return { ...state, ok: state.empty && state.counterEmpty, element: editor };
    }
    // Updating Styles can remount the Advanced form and restore the previous
    // Lexical document. Reset Lyrics again immediately before the final lyric
    // write, after all other controls have settled.
    const draft = allowReload ? newDraftButton() : null;
    if (draft) {
      // Always reset here, even when the visible editor looks empty: Lexical
      // can retain an invisible old document that reappears on insert.
      draft.click();
      await sleep(1800);
      // Commit the New draft to Suno's page state before writing Lyrics.
      if (taskId) sessionStorage.setItem(FINAL_LYRICS_RELOAD_MARKER, taskId);
      location.reload();
      return { ok: false, element: null, reloading: true };
    }
    const reset = await clearLyricsCompletely(editor, EDITOR_RESET_TIMEOUT_MS);
    editor = reset.element || await waitForLiveLyricsEditor(editor, 1500);
    return { ...reset, element: editor };
  }

  function formState() {
    const editor = lyricsEditor();
    let style = styleEditor();
    if (editor && style === editor) style = distinctStyleEditor(editor);
    return {
      editor,
      style,
      titleInput: titleEditor()
    };
  }

  function announceRunnerReadiness() {
    try {
      const state = formState();
      port && port.postMessage({
        type: "runner-ready",
        ready: Boolean(state.editor && state.style && state.editor !== state.style),
        url: location.href,
        runner_version: RUNNER_VERSION
      });
    } catch (_error) {
      scheduleReconnect();
    }
  }

  async function waitForForm(timeoutMs = 60000) {
    const deadline = Date.now() + timeoutMs;
    let state = formState();
    while ((!state.editor || !state.style) && Date.now() < deadline) {
      await sleep(250);
      state = formState();
    }
    return state;
  }

  function pageSongIds() {
    return new Set(Array.from(document.querySelectorAll('a[href*="/song/"]'))
      .map(anchor => anchor.href.split("/song/")[1].replace(/\/$/, ""))
      .filter(Boolean));
  }

  function createButton() {
    return Array.from(document.querySelectorAll('button,[role="button"]')).find(button => {
      if (!isVisible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
      const label = elementLabel(button);
      return label === "create song" || label === "create" || label.includes("create song");
    }) || null;
  }

  function activateCreateButton(button) {
    if (!button || !button.isConnected || !isVisible(button)) return false;
    try { button.scrollIntoView({ block: "center", inline: "center" }); } catch (_error) {}
    try { button.focus({ preventScroll: true }); } catch (_error) { button.focus(); }
    const init = { bubbles: true, cancelable: true, composed: true,
      view: window, button: 0, buttons: 1, detail: 1, clientX: 0, clientY: 0 };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      try { button.dispatchEvent(new EventCtor(type, { ...init, pointerType: "mouse" })); } catch (_error) {}
    }
    button.click();
    return true;
  }

  async function waitForCreateButton(timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs;
    let button = createButton();
    while (!button && Date.now() < deadline) {
      await sleep(250);
      button = createButton();
    }
    return button;
  }

  function isVisible(element) {
    if (!element || !element.getClientRects().length) return false;
    const style = window.getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = element.getBoundingClientRect();
    // Suno's Lyrics editor lives inside an independently scrolling card. Its
    // contenteditable can report a negative page Y while the card itself is
    // visible, so viewport-coordinate clipping falsely rejects the live
    // editor. Use layout/display visibility here; field-specific selectors
    // and connected-node checks still exclude hidden form controls.
    return rect.width > 0 && rect.height > 0;
  }

  function elementLabel(element) {
    return [
      element.getAttribute("aria-label"),
      element.getAttribute("title"),
      element.innerText,
      element.textContent
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function actionElements() {
    return Array.from(document.querySelectorAll('button,[role="button"],[role="menuitem"],a,[tabindex]'))
      .filter(isVisible);
  }

  function moreButton() {
    const labelled = Array.from(document.querySelectorAll('button[aria-label="More menu contents"]'))
      .filter(isVisible);
    // The same aria-label is also used by the persistent bottom playbar. Pick
    // a control in the song header/main content area first; DOM order is not
    // stable after React remounts and can otherwise select the playbar copy.
    const mainLabelled = labelled.filter(element => {
      const rect = element.getBoundingClientRect();
      return rect.top < window.innerHeight - 160 &&
        !element.closest('[aria-label*="Playbar" i], [class*="playbar" i], [class*="player" i]');
    });
    if (mainLabelled.length) {
      return mainLabelled.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0];
    }
    // Keep the old fallback for compact viewports where the main control is
    // near the bottom, but never use a clearly identified playbar control.
    const nonPlaybar = labelled.filter(element =>
      !element.closest('[aria-label*="Playbar" i], [class*="playbar" i], [class*="player" i]'));
    if (nonPlaybar.length) return nonPlaybar[0];
    const elements = actionElements();
    const semantic = elements.filter(element => {
      const label = elementLabel(element);
      const aria = normalizeEditableText(element.getAttribute("aria-label") || "").toLowerCase();
      const title = normalizeEditableText(element.getAttribute("title") || "").toLowerCase();
      return /\b(more|options)\b/.test(label) || /\b(more|options)\b/.test(aria) ||
        /\b(more|options)\b/.test(title) || label === "..." || label === "…" || label === "•••";
    });
    return semantic.find(element => /menu|song|action|option/.test(
      `${element.getAttribute("aria-label") || ""} ${element.getAttribute("title") || ""}`.toLowerCase()
    )) || semantic[0] || null;
  }

  function downloadMenuItem() {
    // Only inspect the currently visible action menu.  The song page also
    // contains a cover-image Download control, and broad page-wide matching
    // can falsely advance the worker to menu_open without opening the song
    // download submenu.
    const menus = Array.from(document.querySelectorAll('[role="menu"]')).filter(isVisible);
    const scoped = menus.flatMap(menu => Array.from(menu.querySelectorAll('button,[role="button"],[role="menuitem"],a,[tabindex]')).filter(isVisible));
    // The current Suno portal omits role="menu".  In that layout use only
    // an exact visible Download button and retain the cover/artwork filters
    // below; this avoids the old false negative while never selecting a
    // generic download-like control.
    const fallback = Array.from(document.querySelectorAll('button,[role="button"],[role="menuitem"],a,[tabindex]'))
      .filter(isVisible)
      .filter(element => normalizeEditableText(element.innerText || element.textContent || '').toLowerCase() === 'download');
    const elements = Array.from(new Set([...scoped, ...fallback]));
    const matches = elements.filter(element => {
      const label = elementLabel(element);
      // A song page also exposes a visible "Download Cover Image" control.
      // It is not the format menu and must never be clicked by this worker.
      if (!/\bdownload\b/.test(label) || label.includes("generating")) return false;
      if (/cover|image|artwork|封面|图片|jpeg|jpg|png/.test(label)) return false;
      // The circular download icon over the artwork can expose only the
      // generic aria-label "Download". Exclude controls inside an artwork
      // container or image/card toolbar; the real format menu is a menu item
      // or a standalone action outside the cover region.
      const parentText = normalizeEditableText(element.parentElement?.textContent || "").toLowerCase();
      const ancestorText = normalizeEditableText(element.closest("figure, [data-testid*='cover' i], [class*='cover' i]")?.textContent || "").toLowerCase();
      if (/cover|artwork|image|封面|图片|jpeg|jpg|png/.test(`${parentText} ${ancestorText}`)) return false;
      if (element.matches("img, [class*='cover' i] *, figure *")) return false;
      const aria = normalizeEditableText(element.getAttribute("aria-label") || "").toLowerCase();
      const text = normalizeEditableText(element.innerText || element.textContent || "").toLowerCase();
      return aria === "download" || text === "download" || label === "download menu" ||
        element.getAttribute("role") === "menuitem";
    });
    // Suno currently mounts the song menu in a portal and exposes a stable
    // aria-label on the visible action. Prefer that exact control before
    // applying the broader legacy menu-item ordering.
    const exact = matches.find(element => element.getAttribute("aria-label") === "Download" &&
      normalizeEditableText(element.innerText || element.textContent || "").toLowerCase() === "download");
    if (exact) return exact;
    return matches.sort((a, b) => {
      const ar = a.getAttribute("role") === "menuitem" ? 0 : 1;
      const br = b.getAttribute("role") === "menuitem" ? 0 : 1;
      return ar - br;
    })[0] || null;
  }

  function visibleSongDownloadMenuItem() {
    return Array.from(document.querySelectorAll('button[aria-label="Download"]'))
      .filter(element => {
        if (!isVisible(element)) return false;
        const text = normalizeEditableText(element.innerText || element.textContent || "").toLowerCase();
        if (text !== "download") return false;
        const context = normalizeEditableText(element.parentElement?.textContent || "").toLowerCase();
        return !/cover|image|artwork|封面|图片|jpeg|jpg|png/.test(context);
      })[0] || null;
  }

  function activateDownloadControl(element) {
    if (!element || !element.isConnected || !isVisible(element)) return false;
    try { element.scrollIntoView({ block: "center", inline: "center" }); } catch (_error) {}
    try { element.focus({ preventScroll: true }); } catch (_error) { try { element.focus(); } catch (_ignored) {} }
    const rect = element.getBoundingClientRect();
    const clientX = Math.round(rect.left + rect.width / 2);
    const clientY = Math.round(rect.top + rect.height / 2);
    const init = { bubbles: true, cancelable: true, composed: true,
      view: window, button: 0, buttons: 1, detail: 1, clientX, clientY };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      try { element.dispatchEvent(new EventCtor(type, { ...init, pointerType: "mouse" })); } catch (_error) {}
    }
    try { element.dispatchEvent(new MouseEvent("click", { ...init, buttons: 0 })); } catch (_error) {}
    return true;
  }

  // Suno's current format buttons are React controls.  A synthetic pointer
  // sequence can move focus without invoking the delegated React handler;
  // use the element's native click first, then fall back to the visible mouse
  // sequence for older releases.
  function clickDownloadControl(element) {
    if (!element || !element.isConnected || !isVisible(element)) return false;
    try { element.click(); } catch (_error) {}
    return true;
  }

  function mediaDownloadItem(downloadType) {
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    const menus = Array.from(document.querySelectorAll('[role="menu"]')).filter(isVisible);
    const scopes = [...dialogs, ...menus];
    const elements = scopes.length
      ? scopes.flatMap(scope => Array.from(scope.querySelectorAll('button,[role="button"],[role="menuitem"],a,[tabindex]')).filter(isVisible))
      : [];
    // The download dialog's format controls are plain buttons in the current
    // Suno release. Query them directly as a fallback because the generic
    // action collection can miss portal descendants during React updates.
    const dialogButtons = dialogs.flatMap(dialog => Array.from(dialog.querySelectorAll('button')).filter(isVisible));
    const candidates = Array.from(new Set([...elements, ...dialogButtons]));
    const matches = elements.filter(element => {
      const label = elementLabel(element);
      if (label.includes("generating")) return false;
      if (downloadType === "mp3") {
        return label.includes("mp3 audio") || label === "mp3" || (label.includes("audio") && !label.includes("video") && !label.includes("wav") && !label.includes("stem"));
      }
      // Current Suno download dialog calls this option "MP4 video asset";
      // older releases used "Video" / "Video Pro".  Match the current
      // wording without ever matching the cover-image action.
      return label === "video" || label.startsWith("video ") || label.startsWith("mp4 video") ||
        label.includes("video pro") || label.includes("download video") || label.includes("video (mp4)");
    });
    const directMatches = candidates.filter(element => {
      const text = normalizeEditableText(element.innerText || element.textContent || '').toLowerCase();
      if (downloadType === "mp3") return text === "mp3" || text.startsWith("mp3 ");
      return text === "mp4 video asset" || text === "video" || text.startsWith("video ");
    });
    return [...directMatches, ...matches].filter((element, index, list) => list.indexOf(element) === index).sort((a, b) => {
      const ar = a.getAttribute("role") === "menuitem" ? 0 : 1;
      const br = b.getAttribute("role") === "menuitem" ? 0 : 1;
      return ar - br;
    })[0] || null;
  }

  function finalMediaDownloadButton() {
    // Portal dialog roots can report a zero/unstable client rect while their
    // rendered buttons are visible. Inspect the dialog's buttons directly.
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    for (const dialog of dialogs) {
      const button = Array.from(dialog.querySelectorAll('button')).find(element => {
        if (!isVisible(element) || element.disabled || element.getAttribute("aria-disabled") === "true") return false;
        const label = normalizeEditableText(element.innerText || element.textContent || "").toLowerCase();
        return label === "download" || label === "unlock & download";
      });
      if (button) return button;
    }
    return null;
  }

  function videoIsGenerating() {
    return visibleText().includes("generating video") || actionElements().some(element => {
      const label = elementLabel(element);
      return label.includes("generating video") || label.includes("video generating");
    });
  }

  async function openDownloadMenu(downloadType) {
    // Suno may create the portal/dialog asynchronously after the action menu
    // opens. Allow the visible format controls time to render before treating
    // the menu as unavailable.
    const formatDeadline = Date.now() + 60000;
    let action = mediaDownloadItem(downloadType);
    if (action) return true;

    let button = moreButton();
    const buttonDeadline = Date.now() + 15000;
    while (!button && Date.now() < buttonDeadline) {
      await sleep(250);
      button = moreButton();
    }
    if (!button) throw new Error("歌曲页面没有找到可见的更多选项按钮");
    // The song action menu is handled by Suno's pointer listeners; native
    // HTMLElement.click() alone is ignored on the current release.
    activateDownloadControl(button);

    let item = null;
    const menuDeadline = Date.now() + 6000;
    while (Date.now() < menuDeadline) {
      await sleep(250);
      item = downloadMenuItem();
      if (item) break;
    }
    if (!item) throw new Error("Suno 更多菜单中没有找到歌曲 Download（未点击封面下载）");

    // Open the song format submenu with a real DOM click first.  Synthetic
    // pointer events are only a compatibility fallback for older releases.
    activateDownloadControl(item);
    item.focus();
    for (const type of ["pointerenter", "pointerover", "pointermove", "mousemove", "mouseenter", "mouseover"]) {
      try { item.dispatchEvent(new PointerEvent(type, { bubbles: true, composed: true, pointerType: "mouse", view: window })); } catch (_error) {}
    }
    while (Date.now() < formatDeadline) {
      await sleep(500);
      action = mediaDownloadItem(downloadType);
      if (action) return true;
    }
    throw new Error(`Suno Download 菜单未显示 ${downloadType === "video" ? "Video" : "MP3"} 选项`);
  }

  function sendDownloadPhase(command, phase, downloadId = null) {
    send("/v1/download/phase", {
      task_id: command.task_id,
      queue_path: command.queue_path,
      phase,
      download_id: downloadId || undefined
    });
  }

  function sendDownloadIntent(command) {
    try {
      port && port.postMessage({
        type: "download-intent",
        payload: {
          task_id: command.task_id,
          queue_path: command.queue_path,
          download_type: command.job.download_type,
          song_id: command.job.song_id
        }
      });
    } catch (_error) {
      scheduleReconnect();
    }
  }

  function sendDownloadError(command, reason, action = "block") {
    send("/v1/download/error", {
      task_id: command.task_id,
      queue_path: command.queue_path,
      action,
      reason
    });
  }

  async function executeDownload(command, observing = false) {
    const job = command && command.job;
    if (!job || !job.task_id) return;
    if (activeDownloadJobId === job.task_id || observingDownloadJobId === job.task_id) return;
    if (!acquireDownloadExecutionLock(job.task_id)) return;
    if (observing) observingDownloadJobId = job.task_id;
    else activeDownloadJobId = job.task_id;
    const downloadType = String(job.download_type || "");
    let waitingForChromeDownload = false;
    try {
      const target = String(job.song_url || "").replace(/\/$/, "");
      const current = String(location.href || "").split("#", 1)[0].replace(/\/$/, "");
      if (target && current !== target) {
        sendDownloadPhase(command, "navigating");
        location.href = target;
        return;
      }
        const issue = blocker();
      if (issue) throw new Error(`页面需要人工处理: ${issue}`);
      const deadline = Date.now() + (downloadType === "video" ? 30 * 60 * 1000 : 5 * 60 * 1000);
      let sawGeneratingVideo = false;
      while (Date.now() < deadline) {
        if (cancelledDownloadJobs.has(job.task_id)) return;
        if (blocker()) throw new Error(`页面需要人工处理: ${blocker()}`);
        await openDownloadMenu(downloadType);
        sendDownloadPhase(command, "menu_open");
        let action = mediaDownloadItem(downloadType);
        // Selecting a format can leave the same visible dialog in a
        // preparation state (for example, "Downloading MP3").  Keep the
        // dialog open and wait for that exact option to become enabled; do
        // not reopen the page menu or click the format repeatedly.
        const actionDeadline = Date.now() + (downloadType === "video" ? 30 * 60 * 1000 : 5 * 60 * 1000);
        while (action && action.disabled && Date.now() < actionDeadline) {
          sendDownloadPhase(command, "preparing");
          await sleep(downloadType === "video" ? 3000 : 1000);
          action = mediaDownloadItem(downloadType);
        }
        if (action && !action.disabled) {
          if (cancelledDownloadJobs.has(job.task_id)) return;
          // Suno's format rows are React controls. Use the same pointer
          // sequence as the menu entries; a bare HTMLElement.click() can be
          // ignored and leave the worker indefinitely at menu_open.
          clickDownloadControl(action);
          await sleep(500);
          // If React did not accept the native click, retry once through the
          // visible pointer path.  Never loop-click the same format control.
          if (!finalMediaDownloadButton()) activateDownloadControl(action);
          // Selecting MP3/MP4 only prepares the download dialog. Suno then
          // enables a second final Download button after the media is ready.
          // Wait for and click that button before binding the Chrome download.
          const finalDeadline = Date.now() + (downloadType === "video" ? 30 * 60 * 1000 : 5 * 60 * 1000);
          let finalButton = finalMediaDownloadButton();
          while (!finalButton && Date.now() < finalDeadline) {
            await sleep(downloadType === "video" ? 3000 : 1000);
            finalButton = finalMediaDownloadButton();
          }
          if (!finalButton) {
            if (downloadType === "video" && videoIsGenerating()) {
              sawGeneratingVideo = true;
              sendDownloadPhase(command, "observing");
              continue;
            }
            throw new Error("Suno 下载弹窗未出现最终 Download 按钮");
          }
          if (cancelledDownloadJobs.has(job.task_id)) return;
          // Bind immediately before the final click; selecting a format does
          // not create a Chrome download yet.
          sendDownloadIntent(command);
          clickDownloadControl(finalButton);
          await sleep(700);
          sendDownloadPhase(command, "download_started");
          waitingForChromeDownload = true;
          return;
        }
        if (downloadType === "video" && videoIsGenerating()) {
          sawGeneratingVideo = true;
          sendDownloadPhase(command, "observing");
          await sleep(10000);
          continue;
        }
        // A generated song can expose its action menu before the media
        // formats are ready. Keep polling the visible page instead of turning
        // this normal readiness delay into a permanent blocked item.
        await sleep(5000);
        continue;
      }
      if (sawGeneratingVideo) throw new Error("Suno Video 生成等待超过 30 分钟");
      throw new Error("等待 Chrome 下载项超过限制时间");
    } catch (error) {
      const reason = String(error.message || error);
      const retryable = /没有找到可见的更多选项按钮|更多菜单中没有找到歌曲 Download|Download 菜单未显示|没有可用的 MP3 Audio 项|没有可用的 Video 项/.test(reason);
      sendDownloadError(command, reason, retryable ? "requeue" : "block");
    } finally {
      if (!waitingForChromeDownload) {
        releaseDownloadExecutionLock(job.task_id);
        if (activeDownloadJobId === job.task_id) activeDownloadJobId = null;
      }
      if (observingDownloadJobId === job.task_id) observingDownloadJobId = null;
    }
  }

  function songLinks(title, before) {
    const ignored = before || knownSongIds;
    return Array.from(document.querySelectorAll('a[href*="/song/"]'))
      .map(anchor => ({ id: anchor.href.split("/song/")[1].replace(/\/$/, ""), url: anchor.href, text: (anchor.textContent || "").trim() }))
      .filter(item => item.id && !ignored.has(item.id) && (!title || item.text === title || item.text.includes(title) || title.includes(item.text)));
  }

  function send(path, payload) {
    if (!port) return;
    try {
      port.postMessage({ type: "worker-post", path, payload });
    } catch (error) {
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 2000);
  }

  function connect() {
    try {
      port = chrome.runtime.connect({ name: "hotspot-suno-runner" });
    } catch (error) {
      scheduleReconnect();
      return;
    }
    port.onMessage.addListener(message => {
      if (message.type === "suno-job" && message.command) execute(message.command);
      if (message.type === "suno-command" && message.command) {
        if (message.command.action === "abort" && message.command.job) {
          const taskId = message.command.job.task_id;
          if (activeJobId === taskId) cancelledJobs.add(taskId);
          if (abortReportedJobs.has(taskId)) return;
          abortReportedJobs.add(taskId);
          send("/v1/browser/error", {
            task_id: taskId,
            queue_path: message.command.queue_path,
            action: "requeue",
            reason: "用户点击停止，尚未提交 Create"
          });
        } else if (message.command.action === "observe") {
          observe(message.command);
        } else if (message.command.action === "execute") {
          // Generation commands are delivered on the same port as abort and
          // observe commands.  Without this branch a claimed queue item is
          // never filled, so the worker eventually returns it to the queue.
          execute(message.command);
        }
      }
      if (message.type === "suno-download" && message.command) executeDownload(message.command, false);
      if (message.type === "suno-download-command" && message.command) {
        if (message.command.action === "abort" && message.command.job) {
          const taskId = message.command.job.task_id;
          if (activeDownloadJobId === taskId) cancelledDownloadJobs.add(taskId);
          if (downloadAbortReportedJobs.has(taskId)) return;
          downloadAbortReportedJobs.add(taskId);
          sendDownloadError(message.command, "用户点击停止，尚未触发下载", "requeue");
        } else if (message.command.action === "execute") {
          executeDownload(message.command, false);
        } else if (message.command.action === "observe") {
          executeDownload(message.command, true);
        }
      }
      if (message.type === "suno-download-complete" && message.payload) {
        const taskId = message.payload.task_id;
        if (activeDownloadJobId === taskId) activeDownloadJobId = null;
        if (observingDownloadJobId === taskId) observingDownloadJobId = null;
        cancelledDownloadJobs.delete(taskId);
        downloadAbortReportedJobs.delete(taskId);
        releaseDownloadExecutionLock(taskId);
      }
      if (message.type === "suno-download-error" && message.payload) {
        const taskId = message.payload.task_id;
        if (activeDownloadJobId === taskId) activeDownloadJobId = null;
        if (observingDownloadJobId === taskId) observingDownloadJobId = null;
        cancelledDownloadJobs.delete(taskId);
        downloadAbortReportedJobs.delete(taskId);
        releaseDownloadExecutionLock(taskId);
      }
    });
    port.onDisconnect.addListener(() => {
      if (port) port = null;
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
      scheduleReconnect();
    });
    announceRunnerReadiness();
    setTimeout(announceRunnerReadiness, 1000);
    setTimeout(announceRunnerReadiness, 3000);
    setTimeout(announceRunnerReadiness, 8000);
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    const sendHeartbeats = () => {
      const payload = { url: location.href };
      send("/v1/browser/heartbeat", payload);
      send("/v1/download/heartbeat", payload);
      announceRunnerReadiness();
    };
    heartbeatTimer = setInterval(sendHeartbeats, 5000);
    sendHeartbeats();
  }

  async function execute(command) {
    const job = command.job;
    if (!job || activeJobId === job.task_id) return;
    const finalLyricsReloaded = sessionStorage.getItem(FINAL_LYRICS_RELOAD_MARKER) === job.task_id;
    if (finalLyricsReloaded) sessionStorage.removeItem(FINAL_LYRICS_RELOAD_MARKER);
    // Suno can retain a hidden Lexical draft after a visible reset. Reload the
    // same Create tab once per item so the next task starts from a clean form.
    if (!finalLyricsReloaded && sessionStorage.getItem(FORM_RELOAD_MARKER) !== job.task_id) {
      sessionStorage.setItem(FORM_RELOAD_MARKER, job.task_id);
      location.reload();
      return;
    }
    sessionStorage.removeItem(FORM_RELOAD_MARKER);
    if (window[EXECUTION_LOCK_KEY]) return;
    if (!acquireExecutionLock(job.task_id)) return;
    window[EXECUTION_LOCK_KEY] = job.task_id;
    const retryAt = Number(formRetryAfter.get(job.task_id) || 0);
    if (retryAt > Date.now()) return;
    cancelledJobs.delete(job.task_id);
    abortReportedJobs.delete(job.task_id);
    activeJobId = job.task_id;
    try {
      const title = String(job.title || "热点创作");
      const { editor, style, titleInput } = await waitForForm();
      if (!editor || !style) {
      await requeueAfterFormFailure(command, "Suno 可见创作表单未完整加载（需要独立的 Lyrics 和 Styles 字段），已保留页面现场并阻塞当前项", editor, style, titleInput);
        return;
      }
      if (editor === style) {
      await requeueAfterFormFailure(command, "Suno Lyrics 和 Styles 暂时定位到同一编辑器，已停止写入并保留页面现场；请刷新扩展后重试", editor, style, titleInput);
        return;
      }
      const issue = blocker();
      if (issue) throw new Error(`页面需要人工处理: ${issue}`);
      const lyrics = safeSunoText(job.lyrics || "", MAX_LYRICS_CHARS);
      const stylePrompt = safeSunoText(job.style_prompt || "", MAX_STYLE_CHARS);
      let validation = null;
      // Reset through Suno's own control so Lexical application state and the
      // visible editor are cleared together before the next queue item.
      const resetOk = await resetSunoFormBeforeTask();
      if (!resetOk) {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        await requeueAfterFormFailure(
          command,
          "Suno 未确认清空 Lyrics，已阻止写入，避免旧歌词与新歌词叠加",
          editor,
          style,
          titleInput
        );
        return;
      }
      // Treat filling as a transaction. Suno's React editor can restore the
      // previous task after a navigation or blur; repeat the whole write and
      // read back both fields before the queue is allowed to advance.
      let liveEditor = editor;
      for (let formAttempt = 1; formAttempt <= 3; formAttempt += 1) {
        const liveStyle = styleEditor() || style;
        const liveTitleInput = titleEditor() || titleInput;
        // Styles/title updates can cause Suno's React form to replace the
        // Lyrics node. Apply those controls first, then write Lyrics last so
        // no later form update can detach the text that was just entered.
        const styleResult = await writeStyleEditable(liveStyle, stylePrompt, MAX_STYLE_CHARS);
        if (liveTitleInput) setTextControl(liveTitleInput, title);
        await sleep(350);
        const lyricReset = await resetLyricsBeforeFinalWrite(liveEditor, job.task_id, !finalLyricsReloaded);
        if (lyricReset.reloading) return;
        liveEditor = lyricReset.element || await waitForLiveLyricsEditor(liveEditor, 1800);
        if (!lyricReset.ok || !liveEditor) {
          validation = {
            ok: false,
            actualLyrics: editableText(liveEditor),
            actualStyle: editableText(styleEditor() || liveStyle),
            counter: visibleLyricsCounter(liveEditor)
          };
          continue;
        }
        const writeResult = await writeEditable(liveEditor, lyrics, MAX_LYRICS_CHARS, true);
        liveEditor = writeResult.element || await waitForLiveLyricsEditor(liveEditor, 1000, lyrics);
        if (!writeResult.ok || !liveEditor) {
          validation = {
            ok: false,
            actualLyrics: writeResult.value || "",
            actualStyle: editableText(styleEditor() || liveStyle),
            counter: visibleLyricsCounter(liveEditor)
          };
          await sleep(500);
          continue;
        }
        // Re-resolve all controls after the final Lyrics write. This catches
        // a React replacement while preserving the exact node whose text was
        // read back successfully.
        liveEditor = await waitForLiveLyricsEditor(liveEditor, 1800, lyrics);
        const verifiedStyle = styleEditor() || liveStyle;
        const verifiedTitle = titleEditor() || liveTitleInput;
        validation = validateForm(liveEditor, verifiedStyle, lyrics, stylePrompt);
        if (verifiedTitle && normalizeEditableText(verifiedTitle.value) !== normalizeEditableText(title)) {
          validation.ok = false;
        }
        if (validation.ok) break;
        await sleep(500);
      }
      if (!validation || !validation.ok) {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        const counter = validation && validation.counter != null ? `，页面计数 ${validation.counter}/5000` : "";
        await requeueAfterFormFailure(
          command,
          `Suno 表单回读未通过，已阻止 Create 并保留页面现场${counter}（歌词实际 ${validation ? validation.actualLyrics.length : 0} 字，风格实际 ${validation ? validation.actualStyle.length : 0} 字，歌词匹配 ${validation && validation.lyrics_match ? "是" : "否"}，风格匹配 ${validation && validation.style_match ? "是" : "否"}）`,
          liveEditor,
          styleEditor() || style,
          titleEditor() || titleInput
        );
        return;
      }
      let button = await waitForCreateButton();
      if (!button) {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        // Form state can settle after the editor is visibly populated. A
        // transiently disabled/missing Create button must not poison the
        // whole queue; return this item for a later visible-page retry.
        await requeueAfterFormFailure(
          command,
          "Create 按钮尚未就绪，已保留页面现场并阻塞当前项",
          liveEditor,
          styleEditor() || style,
          titleEditor() || titleInput
        );
        return;
      }
      // Re-read immediately before the side effect. This closes the race in
      // which React reconciles the old lyrics while Create is becoming ready.
      liveEditor = await waitForLiveLyricsEditor(liveEditor, 1000);
      const finalStyle = styleEditor() || style;
      validation = validateForm(liveEditor, finalStyle, lyrics, stylePrompt);
      if (!validation.ok) {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        await requeueAfterFormFailure(
          command,
          `Suno 在提交前恢复了旧表单，已阻止 Create 并保留页面现场（歌词实际 ${validation.actualLyrics.length} 字）`,
          liveEditor,
          finalStyle,
          titleEditor() || titleInput
        );
        return;
      }
      // React may replace the button while the final form readback is
      // settling. Never click a detached/stale button captured earlier;
      // resolve the currently visible enabled Create control immediately
      // before the one allowed side effect.
      button = createButton();
      if (!button || !button.isConnected || button.disabled || button.getAttribute("aria-disabled") === "true") {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        await requeueAfterFormFailure(
          command,
          "Create 按钮在提交前被 Suno 重绘，已保留页面现场并阻塞当前项",
          liveEditor,
          finalStyle,
          titleEditor() || titleInput
        );
        return;
      }
      const beforeIds = Array.isArray(command.existing_song_ids) && command.existing_song_ids.length
        ? command.existing_song_ids
        : Array.from(pageSongIds());
      const before = new Set(beforeIds);
      knownSongIds = new Set([...knownSongIds, ...before]);
      send("/v1/browser/phase", { task_id: job.task_id, queue_path: command.queue_path, phase: "filled", existing_song_ids: Array.from(before) });
      if (!command.auto_create) {
        activeJobId = null;
        return;
      }
      if (cancelledJobs.has(job.task_id)) return;
      if (blocker()) throw new Error("提交前检测到页面阻断提示");
      if (!activateCreateButton(button)) {
        formRetryAfter.set(job.task_id, Date.now() + 12000);
        await requeueAfterFormFailure(command, "Create 按钮未能完成可见点击，已保留页面现场并阻塞当前项", liveEditor, finalStyle, titleEditor() || titleInput);
        return;
      }
      // Do not infer a rejected click from the button's post-click state.
      // Suno can keep Create enabled while its request is being submitted,
      // and clearing/requeueing here made the user see the lyrics disappear
      // without a Create action. The click itself is the authorized side
      // effect; the result wait below is the authoritative confirmation.
      formRetryAfter.delete(job.task_id);
      send("/v1/browser/phase", { task_id: job.task_id, queue_path: command.queue_path, phase: "submitted" });
      const deadline = Date.now() + 15 * 60 * 1000;
      while (Date.now() < deadline) {
        await sleep(5000);
        const issueAfterSubmit = blocker();
        if (issueAfterSubmit) throw new Error(`生成过程出现页面阻断: ${issueAfterSubmit}`);
        const links = songLinks(title, before);
        if (links.length) {
          links.forEach(item => knownSongIds.add(item.id));
          send("/v1/browser/result", {
            task_id: job.task_id,
            queue_path: command.queue_path,
            song_ids: links.map(item => item.id),
            song_urls: links.map(item => item.url),
            observed_at: new Date().toISOString()
          });
          return;
        }
      }
      throw new Error("等待 Suno 作品链接超过 15 分钟");
    } catch (error) {
      const reason = String(error.message || error);
      if (/表单未完整加载|需要 Lyrics 和 Styles|定位到同一编辑器|字段|编辑器|Create 按钮尚未就绪/.test(reason)) {
        await requeueAfterFormFailure(command, reason, null, null, null);
      } else {
        send("/v1/browser/error", {
          task_id: job.task_id,
          queue_path: command.queue_path,
          action: "block",
          reason
        });
      }
    } finally {
      activeJobId = null;
      releaseExecutionLock(job.task_id);
      if (window[EXECUTION_LOCK_KEY] === job.task_id) delete window[EXECUTION_LOCK_KEY];
    }
  }

  async function observe(command) {
    const job = command.job;
    if (!job || observingJobId === job.task_id) return;
    observingJobId = job.task_id;
    const existingIds = Array.isArray(command.existing_song_ids) ? command.existing_song_ids : [];
    existingIds.forEach(id => knownSongIds.add(String(id)));
    await sleep(3000);
    const links = songLinks(String(job.title || ""));
    if (links.length) {
      links.forEach(item => knownSongIds.add(item.id));
      send("/v1/browser/result", {
        task_id: job.task_id,
        queue_path: command.queue_path,
        song_ids: links.map(item => item.id),
        song_urls: links.map(item => item.url),
        observed_at: new Date().toISOString()
      });
    }
    observingJobId = null;
  }

  // A local, query-string-gated hook lets the regression harness exercise the
  // DOM safety helpers without connecting to a worker or touching Suno.
  if (window.location.search.includes("suno-test=1")) {
    window.__sunoRunnerTest = {
      counterValueFromText,
      visibleLyricsCounter,
      validateForm,
      safeSunoText,
      clearEditable,
      clearLyricsCompletely,
      writeEditable,
      editableText,
      lyricsEditor,
      liveLyricsEditor,
      styleEditor,
      writeStyleEditable
    };
    return;
  }

  knownSongIds = pageSongIds();
  connect();
})();
