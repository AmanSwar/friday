// Friday: is Aman working hard to understand this page? Runs in every page and local file and reports
// only counts to the extension: active reading time, how often he scrolls back up to re-read, text
// selections and copies, and how much text the page has. No page text leaves the page; Friday reads
// the page only if he says yes to her offer of help.
(() => {
  if (window.__fridayEngage) return;
  window.__fridayEngage = true;

  const IDLE_MS = 45000;        // reading counts as active only with some input in the last 45 s
  let lastInput = Date.now();
  let lastTick = Date.now();
  let activeMs = 0;
  let peak = window.scrollY;    // furthest point reached since the last scroll-back
  let backscrolls = 0;
  let selections = 0;
  let copies = 0;
  let lastSelection = "";
  let selTimer = null;
  let textLen = 0;
  let reported = "";
  let page = location.href;    // single-page sites change the address without reloading: start over

  const touch = () => { lastInput = Date.now(); };
  for (const ev of ["mousemove", "keydown", "pointerdown", "wheel", "touchstart"]) {
    addEventListener(ev, touch, { passive: true, capture: true });
  }
  addEventListener("scroll", () => {
    touch();
    const y = window.scrollY;
    if (y > peak) peak = y;
    if (peak - y > window.innerHeight * 0.5) {   // went back up half a screen or more: re-reading
      backscrolls++;
      peak = y;
    }
  }, { passive: true });
  document.addEventListener("selectionchange", () => {
    clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      const s = String(window.getSelection() || "").trim();
      if (s.length >= 15 && s !== lastSelection) { selections++; lastSelection = s; }
    }, 1200);
  });
  document.addEventListener("copy", () => { copies++; touch(); });

  function measureText() {
    try { textLen = (document.body && document.body.innerText.length) || 0; } catch { textLen = 0; }
  }
  measureText();
  setTimeout(measureText, 5000);

  setInterval(() => {
    if (location.href !== page) {
      page = location.href;
      activeMs = backscrolls = selections = copies = 0;
      peak = window.scrollY;
      lastSelection = "";
      setTimeout(measureText, 3000);
    }
    const now = Date.now();
    if (document.visibilityState === "visible" && document.hasFocus() && now - lastInput < IDLE_MS) {
      activeMs += now - lastTick;
    }
    lastTick = now;
  }, 5000);

  setInterval(() => {
    const state = { activeMs, backscrolls, selections, copies, textLen };
    const key = JSON.stringify(state);
    if (key === reported || document.visibilityState !== "visible") return;
    reported = key;
    try {
      chrome.runtime.sendMessage({ type: "engage", url: location.href, title: document.title, ...state });
    } catch {}   // the extension reloaded; this page's script is orphaned until the page reloads
  }, 15000);
})();
