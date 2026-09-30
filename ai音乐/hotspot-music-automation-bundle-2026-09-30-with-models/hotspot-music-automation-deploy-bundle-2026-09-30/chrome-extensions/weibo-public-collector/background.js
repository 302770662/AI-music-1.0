function stamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}-${pad(d.getMinutes())}`;
}

function scheduleHourlyAlarm() {
  const now = new Date();
  const nextHour = new Date(now);
  nextHour.setMinutes(0, 0, 0);
  nextHour.setHours(nextHour.getHours() + 1);
  chrome.alarms.create("weibo-public-hourly", {when: nextHour.getTime()});
}

function scheduleRetries() {
  [1, 2, 3].forEach((minute) => {
    chrome.alarms.create(`weibo-public-retry-${minute}`, {
      delayInMinutes: minute,
    });
  });
}

const PAGE_RE = /^https:\/\/weibo\.com\/a\/hot\/realtime(?:[?#]|$)/i;
const PAGE_URL = "https://weibo.com/a/hot/realtime";

async function exportTab(tab) {
  if (!tab?.id || !PAGE_RE.test(tab.url || "")) return false;
  try {
    const result = await chrome.tabs.sendMessage(tab.id, {type: "collect_weibo_public"});
    if (!result || !result.snapshot || result.snapshot.items.length === 0) throw new Error(result?.error || "页面未返回公开热搜数据");
    const text = JSON.stringify(result.snapshot, null, 2);
    const url = "data:application/json;charset=utf-8," + encodeURIComponent(text);
    await chrome.downloads.download({
      url,
      filename: `weibo-browser-${stamp()}.json`,
      conflictAction: "uniquify",
      saveAs: false,
    });
    return true;
  } catch (error) {
    console.error("Weibo public collector failed", error);
    return false;
  }
}

async function collectCurrentPublicPage() {
  let tabs = await chrome.tabs.query({url: ["https://weibo.com/a/hot/realtime*"]});
  let tab = tabs.find((item) => item.id && item.status === "complete") || tabs.find((item) => item.id);
  if (!tab) {
    // Keep hourly collection unattended: open the public page in a background
    // tab when Chrome is running but the page was closed.
    tab = await chrome.tabs.create({url: PAGE_URL, active: false});
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      tabs = await chrome.tabs.query({url: ["https://weibo.com/a/hot/realtime*"]});
      tab = tabs.find((item) => item.id && item.status === "complete") || tab;
      if (tab?.status === "complete") break;
    }
  }
  return exportTab(tab);
}

chrome.action.onClicked.addListener(() => { collectCurrentPublicPage(); });

chrome.runtime.onInstalled.addListener(() => {
  scheduleHourlyAlarm();
});
chrome.runtime.onStartup.addListener(() => {
  scheduleHourlyAlarm();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "weibo-public-hourly") {
    scheduleHourlyAlarm();
    scheduleRetries();
    collectCurrentPublicPage();
    return;
  }
  if (alarm.name.startsWith("weibo-public-retry-")) {
    collectCurrentPublicPage();
  }
});
