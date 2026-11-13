// Friday's hands in Aman's Brave. Connects out to fridayd (ws://127.0.0.1, token in config.js, written
// by scripts/brave-extension.sh) and runs its commands: list, open, read and click tabs, close Friday's tabs.
// Pages are read in this browser, signed in as Aman.
//
// Friday's group (v2, like Claude's extension): Aman pins Friday to the tab he's on (toolbar icon,
// Option-Shift-F, or right-click "Pin Friday to this tab"). The tab joins a purple "Friday" tab group, and
// FRIDAY SEES ONLY TABS IN THAT GROUP: listing, reading and clicking anything else fails here, not just in
// the prompt. Pages Friday opens (a URL or a Google search) open inside the group. The tab he pinned last
// is "this page" for voice turns and the only one engage.js watches. Dragging a tab into the group shares
// it; clicking the icon on a group tab takes it out; "Stop sharing with Friday" ungroups everything.
//
// Limits held here: only http(s)/file pages; scrolling, clicking and closing only in tabs Friday opened (his
// pinned tabs are read-only); no typing; no clicks on buttons that post, send, follow, buy, sell or pay.
// Friday's tabs he never looked at close after 30 minutes.

import { BRIDGE_URL, TOKEN } from "./config.js";

const VERSION = chrome.runtime.getManifest().version;
const GROUP_TITLE = "Friday";
const GROUP_COLOR = "purple";
const TAB_TTL_MS = 30 * 60 * 1000;
const UNSAFE_TESTID = /(like|retweet|reply|follow|tweet|send|confirm|buy|sell|pay|order|delete|block|mute)/i;
const UNSAFE_CLICK = /^(post|reply|repost|retweet|quote|like|unlike|follow|unfollow|subscribe|send|share|buy|sell|invest|redeem|place order|pay|checkout|transfer|withdraw|delete|remove|block|mute|report|confirm|submit|sign out|log out|logout|upvote|downvote|vote|flag|favorite|bookmark|star|unstar|join|accept|approve)(?![a-z])/i;   // "Star4.1k" is a star button
const PAGE = /^(https?|file):/;

let ws = null;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Friday's bookkeeping (its tabs, his pinned tab) lives in storage.local: storage.session is wiped when the
// extension reloads or updates, while the Friday group survives, so its tabs would look like his. Every
// read-modify-write goes through serial(), one at a time (two parallel opens used to lose a tab).
const store = chrome.storage.local;
let chain = Promise.resolve();
function serial(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}

// --- Friday's group -------------------------------------------------------------------------------

async function fridayGroups() {
  try { return await chrome.tabGroups.query({ title: GROUP_TITLE }); } catch { return []; }
}

/** Every tab in a Friday group (any window), in tab order. Friday sees these and nothing else. */
async function groupTabs() {
  const ids = new Set((await fridayGroups()).map((g) => g.id));
  if (!ids.size) return [];
  return (await chrome.tabs.query({})).filter((t) => ids.has(t.groupId));
}

async function inGroup(tabId) {
  const tab = await chrome.tabs.get(Number(tabId)).catch(() => null);
  if (!tab) return null;
  return (await fridayGroups()).some((g) => g.id === tab.groupId) ? tab : null;
}

async function requireInGroup(tabId) {
  const tab = await inGroup(tabId);
  if (!tab) throw new Error("that tab isn't in Friday's group: Friday only sees tabs Aman pinned it to, and ones it opened there");
  return tab;
}

async function joinGroup(tab) {
  const here = (await fridayGroups()).find((g) => g.windowId === tab.windowId);
  if (tab.pinned) await chrome.tabs.update(tab.id, { pinned: false });   // Brave can't group a pinned tab
  const groupId = await chrome.tabs.group(here ? { tabIds: [tab.id], groupId: here.id }
                                               : { tabIds: [tab.id], createProperties: { windowId: tab.windowId } });
  await chrome.tabGroups.update(groupId, { title: GROUP_TITLE, color: GROUP_COLOR, collapsed: false });
  return groupId;
}

async function getFocus() {
  return (await store.get("focusTab")).focusTab || null;
}

// by: "pin" when he pinned it (Friday reads it and asks what he wants), "moved" when the pinned tab closed
// or left the group and "this page" moved to another shared tab.
async function saveFocus(tab, by = "pin") {
  const focus = tab ? { id: tab.id, url: tab.url, title: tab.title, at: Date.now(), by } : null;
  await store.set({ focusTab: focus });
  if (tab) injectEngage(tab.id);
  return focus;
}

/** What fridayd is told: the tab he's on ("this page") and everything Friday can see. */
async function groupState() {
  const focus = await getFocus();
  const mine = await fridayTabs();
  const group = (await groupTabs()).map((t) => ({ id: t.id, title: t.title, url: t.url, friday: String(t.id) in mine,
                                                 focus: !!focus && focus.id === t.id }));
  return { focus, group };
}

async function notify() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "event", kind: "focus", ...(await groupState()) }));
  refreshBadge();
}

/** The group changed (a tab dragged in or out, closed, ungrouped): keep "this page" pointing at a tab in it. */
async function syncGroup() {
  const tabs = await groupTabs();
  const focus = await getFocus();
  if (!focus || !tabs.some((t) => t.id === focus.id)) {
    const mine = await fridayTabs();
    const his = tabs.filter((t) => !(String(t.id) in mine));
    await saveFocus(his.length ? his[his.length - 1] : null, "moved");
  }
  await notify();
}

async function pin(tab) {
  if (!tab || !PAGE.test(tab.url || "")) return null;
  try {
    await joinGroup(tab);
  } catch (e) {
    chrome.action.setTitle({ title: `Couldn't add this tab to Friday's group: ${(e && e.message) || e}` });
    return null;
  }
  await updateFridayTabs((mine) => { delete mine[tab.id]; });   // he pinned one of Friday's tabs: it's his now
  const focus = await saveFocus(await chrome.tabs.get(tab.id), "pin");
  await notify();
  return focus;
}

async function unpin(tab) {
  try { await chrome.tabs.ungroup(tab.id); } catch {}
  await syncGroup();
}

/** Stop sharing: his tabs leave the group; Friday's tabs he never looked at close. */
async function stopSharing() {
  await updateFridayTabs(async (mine) => {
    for (const t of await groupTabs()) {
      const f = mine[t.id];
      if (f && !f.seen) { try { await chrome.tabs.remove(t.id); } catch {} }
      else { try { await chrome.tabs.ungroup(t.id); } catch {} }
    }
    for (const id of Object.keys(mine)) delete mine[id];
  });
  await saveFocus(null);
  await notify();
}

async function togglePin(tab) {
  if (!tab) return;
  if (await inGroup(tab.id)) return unpin(tab);
  return pin(tab);
}

// Badge: "off" (grey) not connected; "on" (green) connected; "★" (purple) this tab is in Friday's group.
async function refreshBadge() {
  const on = !!ws && ws.readyState === WebSocket.OPEN;
  const here = await lookingAt().catch(() => null);
  const shared = !!here && !!(await inGroup(here.id));
  const count = (await groupTabs()).length;
  chrome.action.setBadgeText({ text: !on ? "off" : shared ? "★" : "on" });
  chrome.action.setBadgeBackgroundColor({ color: !on ? "#8a8a8a" : shared ? "#7e57c2" : "#2e7d32" });
  chrome.action.setTitle({ title: !on ? "Friday isn't connected (is FridayEars running?)"
    : shared ? "Friday can see this tab. Click to take it out of Friday's group."
    : count ? `Friday sees ${count} tab${count === 1 ? "" : "s"} in its group. Click to pin Friday to this one too.`
    : "Click to pin Friday to this tab (it sees nothing else)" });
  chrome.contextMenus.update("friday-pin", { title: shared ? "Unpin this tab from Friday" : "Pin Friday to this tab" },
    () => void chrome.runtime.lastError);
}

function injectEngage(tabId) {
  chrome.scripting.executeScript({ target: { tabId }, files: ["engage.js"] }).catch(() => {});
}

chrome.action.onClicked.addListener((tab) => { connect(); togglePin(tab); });
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "friday-pin") togglePin(tab);
  if (info.menuItemId === "friday-stop") stopSharing();
});
chrome.commands.onCommand.addListener(async (command) => { if (command === "toggle-focus") togglePin(await lookingAt()); });
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  await updateFridayTabs((mine) => { if (mine[tabId]) mine[tabId].seen = true; });   // he looked at it: never auto-close
  refreshBadge();
});
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.groupId !== undefined) return syncGroup();     // dragged into or out of a group
  if (!(info.url || info.title || info.status === "complete")) return;
  if (!(await inGroup(tabId))) return;
  const focus = await getFocus();
  if (focus && focus.id === tabId && (info.url || info.title)) {
    await serial(async () => {              // re-read: a pin in the meantime must not be overwritten
      const now = await getFocus();
      if (now && now.id === tabId) await store.set({ focusTab: { ...now, url: tab.url, title: tab.title } });
    });
  }
  if (focus && focus.id === tabId && info.status === "complete" && PAGE.test(tab.url || "")) injectEngage(tabId);
  if (info.url || info.title) notify();
});
chrome.tabGroups.onUpdated.addListener(() => syncGroup());
chrome.tabGroups.onRemoved.addListener(() => syncGroup());
chrome.runtime.onInstalled.addListener(async () => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "friday-pin", title: "Pin Friday to this tab", contexts: ["page", "action"] },
      () => void chrome.runtime.lastError);
    chrome.contextMenus.create({ id: "friday-stop", title: "Stop sharing with Friday", contexts: ["page", "action"] },
      () => void chrome.runtime.lastError);
  });
  const focus = await getFocus();
  if (focus) injectEngage(focus.id);
});

function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  let sock;
  try {
    sock = new WebSocket(`${BRIDGE_URL}/?token=${encodeURIComponent(TOKEN)}`);
  } catch {
    return;
  }
  ws = sock;
  sock.onopen = async () => {
    refreshBadge();
    const files = await fileAccess();
    sock.send(JSON.stringify({ type: "hello", version: VERSION, browser: navigator.userAgent, files, ...(await groupState()) }));
  };
  sock.onmessage = (ev) => onMessage(sock, ev.data);
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    refreshBadge();
    setTimeout(connect, 3000);
  };
}

// Messages on an open WebSocket keep this service worker alive; the alarm revives it if Brave stopped it.
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
}, 20000);
chrome.alarms.create("friday", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  connect();
  closeStaleTabs();
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await updateFridayTabs((mine) => { delete mine[tabId]; });
  syncGroup();
});
refreshBadge();
connect();

async function onMessage(sock, data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch {
    return;
  }
  if (msg.type !== "call") return;
  const reply = { type: "reply", id: msg.id };
  try {
    reply.result = await run(msg.action, msg.params || {});
    reply.ok = true;
  } catch (e) {
    reply.ok = false;
    reply.error = String((e && e.message) || e);
  }
  if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(reply));
}

// --- "Aman looks stuck on this page" ---------------------------------------------------------------
// engage.js runs only in the tab he pinned last and reports counts. When that page shows real effort to
// understand it (several minutes of active reading on a text-heavy page, plus scrolling back to re-read or
// selecting and copying passages), tell fridayd, which decides whether to offer help.

const DEEP_READ = { activeMs: 4 * 60 * 1000, backscrolls: 3, marks: 2, textLen: 1500 };
const RESEND_MS = 10 * 60 * 1000;
const NOT_READING = /(^|\.)(youtube\.com|netflix\.com|primevideo\.com|hotstar\.com|spotify\.com|whatsapp\.com|instagram\.com|facebook\.com|x\.com|twitter\.com|reddit\.com|slack\.com|discord\.com|mail\.google\.com|meet\.google\.com|zoom\.us)$/;

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "engage" && sender.tab) handleEngage(msg, sender.tab);
});

const lastEngage = {};   // tab id -> latest counts from engage.js (in memory; refilled every 15 s)

async function handleEngage(m, tab) {
  lastEngage[tab.id] = { ...m, at: Date.now() };
  const focus = await getFocus();
  if (!tab.active || !focus || focus.id !== tab.id) return;
  if (String(tab.id) in (await fridayTabs())) return;
  let host = "";
  try { host = new URL(m.url).hostname; } catch {}
  if (NOT_READING.test(host)) return;
  const struggling = m.activeMs >= DEEP_READ.activeMs && m.textLen >= DEEP_READ.textLen &&
    (m.backscrolls >= DEEP_READ.backscrolls || m.selections + m.copies >= DEEP_READ.marks);
  if (!struggling || !ws || ws.readyState !== WebSocket.OPEN) return;
  const { deepReadSent = {} } = await chrome.storage.session.get("deepReadSent");
  if (Date.now() - (deepReadSent[m.url] || 0) < RESEND_MS) return;
  deepReadSent[m.url] = Date.now();
  await chrome.storage.session.set({ deepReadSent });
  ws.send(JSON.stringify({ type: "event", kind: "deep_read", tab_id: tab.id, url: m.url, title: m.title,
    active_s: Math.round(m.activeMs / 1000), backscrolls: m.backscrolls, selections: m.selections, copies: m.copies }));
}

// --- Friday's own tabs (inside the group) ------------------------------------------------------------

async function fridayTabs() {
  const raw = (await store.get("fridayTabs")).fridayTabs || {};
  for (const [id, v] of Object.entries(raw)) if (typeof v === "number") raw[id] = { opened: v, seen: false };   // v1 kept a timestamp
  return raw;
}

async function saveFridayTabs(tabs) {
  await store.set({ fridayTabs: tabs });
}

/** Change Friday's tab list atomically: fn(mine) edits it in place. */
function updateFridayTabs(fn) {
  return serial(async () => {
    const mine = await fridayTabs();
    const out = await fn(mine);
    await saveFridayTabs(mine);
    return out;
  });
}

async function requireFridayTab(tabId) {
  const mine = await fridayTabs();
  if (!(String(tabId) in mine)) throw new Error("that tab is Aman's, not one Friday opened; Friday only reads his tabs");
}

async function closeStaleTabs() {
  await updateFridayTabs(async (mine) => {
    const now = Date.now();
    for (const [id, f] of Object.entries(mine)) {
      if (f.seen || now - f.opened < TAB_TTL_MS) continue;
      try {
        const tab = await chrome.tabs.get(Number(id));
        if (tab.active) continue;            // on screen right now: check again later, keep tracking it
        await chrome.tabs.remove(tab.id);
      } catch {}                             // already gone
      delete mine[id];
    }
  });
}

// --- page helpers -------------------------------------------------------------------------------

function cleanUrl(raw) {
  let url = String(raw || "").trim();
  if (!url) throw new Error("no url");
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = "https://" + url;
  const u = new URL(url);
  if (!["https:", "http:", "file:"].includes(u.protocol)) throw new Error("only web pages and local files");
  return u.href;
}

// executeScript can stall on a document that's mid-navigation; nothing here may wait on it forever.
function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} timed out`)), ms);
  })]).finally(() => clearTimeout(timer));
}

// Local files (file://) need Brave's per-extension "Allow access to file URLs" switch.
function fileAccess() {
  return new Promise((resolve) => chrome.extension.isAllowedFileSchemeAccess(resolve));
}

async function exec(tabId, func, args = [], ms = 8000) {
  let res;
  try {
    // injectImmediately: by default Brave holds the script until the page is idle, which a live site
    // (X keeps loading) may never reach.
    [res] = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, func, args, injectImmediately: true }),
                              ms, "reading the page");
  } catch (e) {
    const m = String((e && e.message) || e);
    if (/error page/i.test(m)) throw new Error("the site returned an error page (blocked, offline, or the address is wrong)");
    if (/file:\/\//i.test(m) || (/cannot access/i.test(m) && !(await fileAccess())))
      throw new Error("that's a local file, and Brave isn't letting the Friday extension read files yet: in brave://extensions, open Friday's Details and turn on \"Allow access to file URLs\"");
    if (/cannot access|cannot be scripted|extensions gallery/i.test(m)) throw new Error("Brave doesn't let extensions read that page");
    throw e;
  }
  return res ? res.result : undefined;
}

// A page is ready when its text stops changing. Live sites (X) never finish loading and fill in after
// the document is parsed, so "complete" isn't waited for. ``leaving``: the URL being navigated away
// from, so the old page's text isn't mistaken for the new one.
async function settle(tabId, ms, leaving = null) {
  const deadline = Date.now() + ms;
  let last = -1;
  let stable = 0;
  while (Date.now() < deadline) {
    const r = await exec(tabId, () => [location.href, document.readyState,
                                       document.body ? document.body.innerText.length : 0], [], 3000).catch(() => null);
    if (r && r[1] !== "loading" && (!leaving || r[0] !== leaving)) {
      const n = r[2];
      if (n > 0 && n === last) {
        if (++stable >= (r[1] === "complete" ? 2 : 3) + (n < 400 ? 2 : 0)) return Date.now();
      } else {
        stable = 0;
      }
      last = n;
    } else {
      stable = 0;
      last = -1;
    }
    await sleep(500);
  }
  return Date.now();
}

// Runs inside the page. Self-contained: executeScript serializes it.
function extract(selector, wantLinks) {
  let root = null;
  if (selector) root = document.querySelector(selector);
  if (!root) {
    const main = document.querySelector("main, [role=main]");
    root = main && main.innerText.trim().length > 200 ? main : document.body;
  }
  const text = ((root && root.innerText) || "").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  let links;
  if (wantLinks && root) {
    const seen = new Set();
    links = [];
    for (const a of root.querySelectorAll("a[href]")) {
      const label = (a.innerText || a.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ");
      if (!label || seen.has(a.href) || !/^https?:/.test(a.href)) continue;
      seen.add(a.href);
      links.push({ text: label.slice(0, 120), url: a.href });
      if (links.length >= 80) break;
    }
  }
  return { title: document.title, url: location.href, text, links };
}

// Appends what a scroll revealed. Long lists (X) drop rows that scrolled away, so the new text is
// joined after the last long line both reads share instead of being concatenated.
function merge(acc, next) {
  const a = acc.split("\n");
  const b = next.split("\n");
  for (let i = a.length - 1; i >= 0 && i >= a.length - 40; i--) {
    if (a[i].length < 25) continue;
    const j = b.lastIndexOf(a[i]);
    if (j >= 0) return a.concat(b.slice(j + 1)).join("\n");
  }
  return acc + "\n" + next;
}

async function readTab(tabId, p, mayScroll) {
  const max = Math.min(Number(p.max_chars) || 12000, 60000);
  const scrolls = mayScroll ? Math.min(Math.max(Number(p.scroll) || 0, 0), 15) : 0;
  let page;
  try {
    page = await exec(tabId, extract, [p.selector || null, !!p.links], 6000);
  } catch (e) {
    // A tab that stopped answering scripts usually recovers on a reload. Only Friday's own tabs.
    if (!mayScroll || !/timed out/.test(String(e && e.message))) throw e;
    await chrome.tabs.reload(tabId);
    await sleep(300);
    await settle(tabId, 15000);
    page = await exec(tabId, extract, [p.selector || null, !!p.links]);
  }
  if (!page) throw new Error("couldn't read that page");
  let text = page.text;
  for (let i = 0; i < scrolls; i++) {
    await exec(tabId, () => window.scrollBy(0, Math.round(window.innerHeight * 0.85)));
    await sleep(1300);
    const more = await exec(tabId, extract, [p.selector || null, false]);
    if (more && more.text) text = merge(text, more.text);
    if (text.length > max) break;
  }
  const truncated = text.length > max;
  const out = { tab_id: tabId, title: page.title, url: page.url, text: truncated ? text.slice(0, max) : text, truncated };
  if (page.links) out.links = page.links;
  if (p.scroll && !mayScroll) out.note = "not scrolled: that's Aman's own tab";
  return out;
}

// Runs inside the page.
function clickIn(selector, text, unsafeSource, unsafeTestidSource) {
  const unsafe = new RegExp(unsafeSource, "i");
  const unsafeTestid = new RegExp(unsafeTestidSource, "i");
  const label = (e) => (e.innerText || e.value || e.getAttribute("aria-label") || e.title || "").trim().replace(/\s+/g, " ");
  let el = null;
  if (selector) {
    el = document.querySelector(selector);
  } else if (text) {
    const want = String(text).trim().toLowerCase();
    const all = [...document.querySelectorAll("a[href], button, [role=button], [role=tab], [role=link], [role=menuitem], summary")]
      .filter((e) => e.getClientRects().length);
    el = all.find((e) => label(e).toLowerCase() === want) || all.find((e) => label(e).toLowerCase().includes(want));
  }
  if (!el) return { clicked: false, error: "nothing on the page matches that" };
  const name = label(el);
  const parts = name.split(/[.·|:]\s*/).map((s) => s.trim());   // "123 Likes. Like" is a like button
  if (parts.some((s) => unsafe.test(s)) || unsafeTestid.test(el.getAttribute("data-testid") || "") ||
      el.closest("form [type=submit]") === el) {
    return { clicked: false, error: `won't click "${name.slice(0, 60)}": it acts on Aman's account` };
  }
  // A plain link is followed by loading its URL from the extension: a page-started navigation can leave the
  // tab unscriptable, and background tabs don't paint, which stalls client-side routers (GitHub, X).
  if (el.tagName === "A" && /^https?:/.test(el.href) && !el.href.startsWith(location.href.split("#")[0] + "#")) {
    return { clicked: true, label: name.slice(0, 80), href: el.href, navigate: true };
  }
  el.scrollIntoView({ block: "center" });
  setTimeout(() => el.click(), 30);   // after this script returns: a navigation would swallow its result
  return { clicked: true, label: name.slice(0, 80), href: el.href || null };
}

// --- ducking: Friday is talking, lower the media playing in Aman's tabs ------------------------------

// Runs inside the page. Remembers each element's volume on the element itself.
function duckMedia(factor) {
  let n = 0;
  for (const el of document.querySelectorAll("video, audio")) {
    if (el.paused || el.muted || el.dataset.fridayVol !== undefined) continue;
    el.dataset.fridayVol = String(el.volume);
    el.volume = Math.max(0, Math.min(1, el.volume * factor));
    n++;
  }
  return n;
}

// Runs inside the page. Gives back the volume unless Aman changed it while Friday was talking.
function unduckMedia(factor) {
  for (const el of document.querySelectorAll("video, audio")) {
    if (el.dataset.fridayVol === undefined) continue;
    const before = parseFloat(el.dataset.fridayVol);
    if (Math.abs(el.volume - before * factor) < 0.02) el.volume = before;
    delete el.dataset.fridayVol;
  }
  return true;
}

// --- commands -----------------------------------------------------------------------------------

async function lookingAt() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

async function run(action, p) {
  if (action === "tabs") {   // only what's in Friday's group
    const { focus, group } = await groupState();
    const here = await lookingAt().catch(() => null);
    return { focus_tab: focus ? focus.id : null, looking_at_group_tab: !!here && group.some((t) => t.id === here.id),
             tabs: group, note: group.length ? undefined : "Aman hasn't pinned Friday to any tab, so it sees none" };
  }
  if (action === "open") {   // a page (url) or a Google search (search), in a tab inside Friday's group
    const url = p.search ? `https://www.google.com/search?q=${encodeURIComponent(String(p.search))}` : cleanUrl(p.url);
    const mine = await fridayTabs();
    let tab;
    let leaving = null;
    if (p.tab_id != null && String(p.tab_id) in mine && (await inGroup(p.tab_id))) {
      const old = (await chrome.tabs.get(Number(p.tab_id))).url;
      if (old !== url) leaving = old;
      tab = await chrome.tabs.update(Number(p.tab_id), p.show ? { url, active: true } : { url });
    } else {
      // the window that holds the group (his pinned tab's first), else the window he's in
      let windowId = null;
      const focus = await getFocus();
      const ft = focus ? await chrome.tabs.get(focus.id).catch(() => null) : null;
      if (ft) windowId = ft.windowId;
      if (windowId == null) {
        const groups = await fridayGroups();
        if (groups.length) windowId = groups[0].windowId;
      }
      if (windowId == null) {
        try { windowId = (await chrome.windows.getLastFocused({ windowTypes: ["normal"] })).id; } catch {}
      }
      if (windowId != null) {
        tab = await chrome.tabs.create({ url, active: !!p.show, windowId });
      } else {
        tab = (await chrome.windows.create({ url, focused: !!p.show })).tabs[0];
      }
      await joinGroup(tab);
    }
    await updateFridayTabs((m) => { m[tab.id] = { opened: Date.now(), seen: !!p.show }; });
    notify();
    const started = Date.now();
    await settle(tab.id, Math.min(Number(p.wait_ms) || 20000, 45000), leaving);
    return { load_ms: Date.now() - started, shown: !!p.show, ...(await readTab(tab.id, p, true)) };
  }
  if (action === "read") {   // a tab in Friday's group; no tab id: the one he pinned last
    let tab;
    if (p.tab_id != null) {
      tab = await requireInGroup(p.tab_id);
    } else {
      const focus = await getFocus();
      tab = focus ? await inGroup(focus.id) : null;
      if (!tab) {
        const tabs = await groupTabs();
        tab = tabs.find((t) => t.active) || tabs[0];
      }
      if (!tab) throw new Error("Aman hasn't pinned Friday to a tab, so it can't see any (he pins one with Friday's toolbar icon, Option-Shift-F, or right-click > Pin Friday to this tab)");
    }
    if (!PAGE.test(tab.url || "")) throw new Error(`can't read ${tab.url || "that tab"} (not a web page or file)`);
    const mine = await fridayTabs();
    return readTab(tab.id, p, String(tab.id) in mine);
  }
  if (action === "click") {   // only in Friday's own tabs, inside its group
    const tabId = Number(p.tab_id);
    await requireInGroup(tabId);
    await requireFridayTab(tabId);
    const before = (await chrome.tabs.get(tabId)).url;
    const res = await exec(tabId, clickIn, [p.selector || null, p.text || null, UNSAFE_CLICK.source, UNSAFE_TESTID.source]);
    if (!res || !res.clicked) throw new Error((res && res.error) || "click failed");
    if (res.navigate) {
      await chrome.tabs.update(tabId, { url: res.href });
      await settle(tabId, 20000, res.href !== before ? before : null);
    } else {
      await sleep(600);   // a button: let the page react, then wait for it to settle
      await settle(tabId, 8000);
    }
    return { clicked: res.label, ...(await readTab(tabId, p, true)) };
  }
  if (action === "close") {   // Friday's tabs only
    return updateFridayTabs(async (mine) => {
      const ids = p.tab_id != null ? [String(p.tab_id)] : Object.keys(mine);
      let closed = 0;
      for (const id of ids) {
        if (!(id in mine)) continue;
        try {
          await chrome.tabs.remove(Number(id));
          closed++;
        } catch {}
        delete mine[id];
      }
      return { closed };
    });
  }
  if (action === "engagement") {   // how hard he's been working on his pinned tab: counts only, no page text
    const focus = await getFocus();
    const tab = focus ? await inGroup(focus.id) : null;
    if (!tab) return { note: "Aman hasn't pinned Friday to a tab, so no reading activity is tracked" };
    const e = lastEngage[tab.id];
    if (!e) return { tab_id: tab.id, note: "no reading activity recorded on that tab yet" };
    return { tab_id: tab.id, title: e.title, url: e.url, active_s: Math.round(e.activeMs / 1000), backscrolls: e.backscrolls,
             selections: e.selections, copies: e.copies, text_chars: e.textLen, reported_s_ago: Math.round((Date.now() - e.at) / 1000) };
  }
  if (action === "focus") {   // op: get (default) | set: pin the tab he's looking at (or tab_id) | clear: stop sharing
    if (p.op === "clear") {
      await stopSharing();
      return await groupState();
    }
    if (p.op === "set") {   // the tab he's looking at, or one already in the group; never an arbitrary tab id
      const tab = p.tab_id != null ? await requireInGroup(p.tab_id) : await lookingAt();
      if (!tab || !PAGE.test(tab.url || "")) throw new Error("that tab isn't a web page or file");
      if (!(await pin(tab))) throw new Error("couldn't add that tab to Friday's group");
      return await groupState();
    }
    return await groupState();
  }
  if (action === "duck") {   // lower media in tabs that are playing sound; remember which
    const factor = Math.max(0, Math.min(1, Number(p.factor) || 0.1));
    const ducked = {};
    for (const tab of await chrome.tabs.query({ audible: true })) {
      if (!/^(https?|file):/.test(tab.url || "")) continue;
      const n = await exec(tab.id, duckMedia, [factor], 3000).catch(() => 0);
      if (n) ducked[tab.id] = factor;
    }
    await chrome.storage.session.set({ duckedTabs: ducked });
    return { ducked: Object.keys(ducked).length };
  }
  if (action === "unduck") {
    const { duckedTabs = {} } = await chrome.storage.session.get("duckedTabs");
    for (const [id, factor] of Object.entries(duckedTabs)) {
      await exec(Number(id), unduckMedia, [factor], 3000).catch(() => {});
    }
    await chrome.storage.session.set({ duckedTabs: {} });
    return { restored: Object.keys(duckedTabs).length };
  }
  if (action === "reload") {   // pick up a new version of this extension from disk (fridayd sends this)
    setTimeout(() => chrome.runtime.reload(), 100);
    return { reloading: VERSION };
  }
  throw new Error(`unknown action ${action}`);
}
