(function () {
  if (window.__hotspotWeiboCollectorLoaded) return;
  window.__hotspotWeiboCollectorLoaded = true;
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const visible = (node) => {
    if (!node || !(node instanceof Element)) return false;
    const style = getComputedStyle(node), rect = node.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const number = (value) => {
    const text = clean(value).replace(/,/g, ""), m = text.match(/(\d+(?:\.\d+)?)(亿|万|千|w|k)?/i);
    if (!m) return null;
    const scale = /亿/i.test(m[2] || "") ? 100000000 : /万|w/i.test(m[2] || "") ? 10000 : /千|k/i.test(m[2] || "") ? 1000 : 1;
    return Math.round(Number(m[1]) * scale);
  };
  const closestRow = (node) => node.closest("li, tr, [role='row'], [class*='item'], [class*='card'], [class*='rank']") || node.parentElement;
  const stat = (text, names) => {
    for (const name of names) {
      const m = text.match(new RegExp(`(?:${name})\\s*[:：]?\\s*([\\d,.]+(?:亿|万|千|w|k)?)`, "i"));
      if (m) return number(m[1]);
    }
    return null;
  };
  const rankFrom = (text, index) => {
    const m = clean(text).match(/^(?:置顶\s*)?(\d{1,3})(?:\s|[.、)]|$)/);
    return m ? Number(m[1]) : index;
  };
  const titleFrom = (anchor) => {
    const text = clean(anchor.innerText || anchor.getAttribute("aria-label") || anchor.getAttribute("title"));
    return text.replace(/^\d{1,3}[.、)\s]+/, "").trim();
  };
  const excluded = /^(首页|登录|注册|下载|更多|搜索|热搜|榜单|我的|视频|图片|发现|设置|帮助)$/;
  function collect() {
    if (!/^https:\/\/weibo\.com\/a\/hot\/realtime(?:[?#]|$)/i.test(location.href)) throw new Error("不是微博实时热点页面");
    const anchors = Array.from(document.querySelectorAll("a[href]"))
      .filter(visible)
      .map((a) => ({a, title: titleFrom(a), href: a.href}))
      .filter((x) => x.title.length >= 2 && x.title.length <= 160 && !excluded.test(x.title))
      .filter((x) => /^https:\/\/weibo\.com\/a\/hot\/[A-Za-z0-9_-]+(?:[.?/#]|$)/i.test(x.href) && !/\/realtime(?:[?#]|$)/i.test(x.href));
    const items = [], seen = new Set();
    for (const [offset, entry] of anchors.entries()) {
      const row = closestRow(entry.a), rowText = clean(row?.innerText || entry.title), key = `${entry.title}\n${entry.href}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const likes = stat(rowText, ["点赞", "赞"]), comments = stat(rowText, ["评论"]), forwards = stat(rowText, ["转发", "分享"]);
      const interactionTotal = [likes, comments, forwards].filter((v) => v !== null).reduce((a, b) => a + b, 0) || null;
      const authorLink = Array.from(row?.querySelectorAll("a[href]") || []).find((a) => a !== entry.a && /\/u\/\d+|\/profile\//.test(a.href) && visible(a));
      const published = rowText.match(/(?:\d{1,2}月\d{1,2}日\s*)?\d{1,2}:\d{2}/)?.[0] || null;
      items.push({title: entry.title, url: entry.href, rank: rankFrom(rowText, items.length + 1), author: clean(authorLink?.innerText) || null, published, likes, comments, forwards,
        weibo_stats: {likes, comments, forwards, interaction_total: interactionTotal}});
      if (items.length >= 30) break;
    }
    return {page: location.href.split("#")[0], captured_at: new Date().toISOString(), source_mode: "chrome_extension_visible_public_dom", license_status: "metadata_only_public", items};
  }
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "collect_weibo_public") return;
    try { sendResponse({snapshot: collect()}); } catch (error) { sendResponse({error: String(error)}); }
    return true;
  });
})();
