(function () {
  if (window.top !== window) return;
  let port = null;
  try {
    port = chrome.runtime.connect({ name: "hotspot-suno-dashboard" });
  } catch (_error) {
    // The extension can be reloaded while the local dashboard is open.
    // A stale content-script must fail quietly until the dashboard is loaded again.
    return;
  }
  window.addEventListener("beforeunload", () => {
    try { port && port.disconnect(); } catch (_error) {}
  }, { once: true });
})();
