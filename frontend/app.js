// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
};
let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3600);
}
const fmtSize = (b) => b > 1e9 ? (b/1e9).toFixed(1)+" GB" : (b/1e6).toFixed(1)+" MB";

// ------------------------------------------------------------------ tabs
// Top tabs, in order: channel overviews (from /api/niches) → Search → Library →
// Formatter. "valorant"/"cars" are channel-overview panels; "search" holds the
// clip searcher + Auto-Layout editor; "library" is the file-management system.
async function initTabs() {
  const niches = await api("/api/niches");
  const tabsEl = $("#tabs");
  AL.tabBtns = {};
  const addTab = (id, label, inactive) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.innerHTML = inactive ? `${label} <span class="dot">•</span>` : label;
    b.onclick = () => selectTab(id, b);
    tabsEl.appendChild(b);
    AL.tabBtns[id] = b;
    return b;
  };
  niches.forEach(n => addTab(n.id, n.label, !n.active));   // Valorant, Cars
  addTab("search", "Search");
  addTab("library", "Library");
  addTab("formatter", "Formatter");
}
function selectTab(id, btn) {
  if (!AL.tabBtns[id]) id = "valorant";              // unknown hash → default
  btn = btn || AL.tabBtns[id];
  $$(".tab").forEach(t => t.classList.remove("active"));
  if (btn) btn.classList.add("active");
  $$(".panel").forEach(p => p.classList.toggle("hidden", p.dataset.niche !== id));
  if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
  if (id === "valorant" || id === "cars") loadOverview(id);
  if (id === "library") loadLibraryTab();
  if (id === "formatter") loadFormatter();
}

// ------------------------------------------------------------------ search
let lastResults = [];   // ranked results from the backend (server "smart" order)

const fmtDur = (s) => !s ? "" :
  (s >= 3600 ? new Date(s*1000).toISOString().substr(11,8)
             : new Date(s*1000).toISOString().substr(14,5)).replace(/^0/, "");
const fmtViews = (v) => !v ? "" :
  v >= 1e6 ? (v/1e6).toFixed(v >= 1e7 ? 0 : 1)+"M" :
  v >= 1e3 ? Math.round(v/1e3)+"K" : String(v);

async function doSearch() {
  const creator = $("#v-creator").value.trim();
  const query = $("#v-query").value.trim();
  if (!creator && !query) return toast("Enter a creator or query", true);
  const box = $("#v-results");
  box.innerHTML = `<div class="hint">Searching…</div>`;
  try {
    lastResults = await api("/api/clips/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creator, query, limit: 16,
                             include_twitch: $("#v-twitch").checked }),
    });
    renderResults();
  } catch (e) { box.innerHTML = `<div class="hint">Error: ${e.message}</div>`; }
}

// "1:23" / "1:02:03" / "83" -> seconds (or null when blank/invalid)
function parseTime(str) {
  str = (str || "").trim();
  if (!str) return null;
  if (str.includes(":")) {
    let s = 0;
    for (const p of str.split(":")) s = s * 60 + (parseFloat(p) || 0);
    return s;
  }
  const n = parseFloat(str);
  return isFinite(n) && n >= 0 ? n : null;
}

// Build one search-result row. `onGet(result, btn, start, end)` runs when Get is
// clicked; start/end are seconds (or null for a full download). The ✂ toggle
// reveals from/to inputs so only a section of a long video is fetched.
function resultRowEl(r, onGet) {
  const src = r.source === "twitch" ? "Twitch" : "YT";
  const dur = fmtDur(r.duration), views = fmtViews(r.view_count);
  const el = document.createElement("div");
  el.className = "result";
  el.innerHTML = `
    <img src="${r.thumbnail||""}" onerror="this.style.visibility='hidden'"/>
    <div class="meta">
      <div class="title">${r.title||"(untitled)"}</div>
      <div class="sub">
        <span class="src src-${r.source}">${src}</span>
        ${dur ? `<span class="dur dur-${r.tier}">${dur}</span>` : ""}
        ${r.is_short_form ? `<span class="res-flag" title="Already vertical — the reflow tool wants landscape footage">⬍ Short</span>` : ""}
        ${r.channel ? `<span class="ch">${r.channel}</span>` : ""}
        ${views ? `<span class="vc">${views} views</span>` : ""}
      </div>
    </div>
    <div class="res-actions">
      <button class="res-preview ghost" type="button" title="Preview this clip">▶</button>
      <button class="res-range-tg ghost" type="button" title="Download only a section">✂</button>
      <button class="res-get">Get</button>
    </div>
    <div class="res-range hidden">
      <span class="res-range-lbl">Section</span>
      <label>from <input class="res-from" type="text" placeholder="0:00" /></label>
      <label>to <input class="res-to" type="text" placeholder="${dur || "end"}" /></label>
      <span class="hint">blank = whole video</span>
    </div>`;

  const getBtn = el.querySelector(".res-get");
  const rangeRow = el.querySelector(".res-range");
  el.querySelector(".res-preview").onclick = ev => previewRemoteClip(r.url, r.title, ev.currentTarget);
  el.querySelector(".res-range-tg").onclick = () => rangeRow.classList.toggle("hidden");
  getBtn.onclick = () => {
    let start = null, end = null;
    if (!rangeRow.classList.contains("hidden")) {
      const from = parseTime(el.querySelector(".res-from").value);
      const to = parseTime(el.querySelector(".res-to").value);
      if (from != null || to != null) {              // both blank -> full download
        start = from ?? 0;
        end = to ?? r.duration ?? null;              // "to" blank -> clip's end
        if (end != null && end <= start) return toast("Section end must be after its start", true);
      }
    }
    onGet(r, getBtn, start, end);
  };
  return el;
}

// Sort client-side (no re-fetch) and render, grouping long-form under a divider.
function renderResults() {
  const box = $("#v-results");
  if (!lastResults.length) { box.innerHTML = `<div class="hint">No results.</div>`; return; }

  const sort = $("#v-sort").value;
  let rows = lastResults.slice();
  if (sort === "short") rows.sort((a,b) => (a.duration||1e9) - (b.duration||1e9));
  else if (sort === "views") rows.sort((a,b) => (b.view_count||0) - (a.view_count||0));
  // "smart" keeps the server ranking as-is.

  box.innerHTML = "";
  let dividerShown = false;
  rows.forEach(r => {
    // In smart order the long-form tail is grouped behind a divider.
    if (sort === "smart" && r.tier === "long" && !dividerShown) {
      const d = document.createElement("div");
      d.className = "result-divider";
      d.textContent = "Longer clips & VODs — crop a moment out";
      box.appendChild(d);
      dividerShown = true;
    }
    box.appendChild(resultRowEl(r, (res, btn, start, end) => downloadClip(res.url, btn, start, end)));
  });
}

async function downloadClip(url, btn, start = null, end = null, keep = null) {
  if (keep === null) keep = !!($("#v-keep") && $("#v-keep").checked);
  btn.disabled = true; btn.textContent = "…";
  try {
    const res = await api("/api/clips/download", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, start, end, keep }),
    });
    toast(`Downloaded: ${res.title || res.name}${keep ? " (kept)" : ""}`);
    await loadLibrary();
    btn.textContent = "✓";
    return res;
  } catch (e) { toast("Download failed: " + e.message, true); btn.disabled = false; btn.textContent = "Get"; throw e; }
}

// Flag a clip long-term (kept) or ephemeral. Best-effort; used by the library
// pins and whenever a clip is committed to a ranking rank.
async function keepClip(name, keep = true) {
  if (!name) return;
  try {
    await api("/api/clips/keep", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, keep }),
    });
  } catch { /* non-fatal */ }
}

// ------------------------------------------------------------------ Nb1 grabber
// Browse the Im_Nb1 compilation channel, scrub a video, and grab just a section.
// Reusable: openNb1(onGrab, label) is launched from Find clips (-> library) and
// the rank finder (-> a rank). The grab reuses /api/clips/download (section aware).
function nb1FmtT(t) { return fmtDur(t) || "0:00"; }   // fmtDur("")s a falsy 0
function nb1Pct(t) { const d = AL.nb1.dur || 1; return Math.max(0, Math.min(1, t / d)) * 100; }

function openNb1(onGrab, label) {
  AL.nb1.ctx = { onGrab, label: label || "" };
  $("#nb1-ctx").textContent = label ? `→ ${label}` : "";
  nb1ShowBrowse();
  $("#nb1-modal").classList.remove("hidden");
  nb1Load(true);
}
function closeNb1() {
  const v = $("#nb1-video");
  try { v.pause(); v.removeAttribute("src"); v.load(); } catch { /* ignore */ }
  $("#nb1-modal").classList.add("hidden");
  AL.nb1.ctx = null; AL.nb1.cur = null;
}
function nb1ShowBrowse() {
  $("#nb1-browse").classList.remove("hidden");
  $("#nb1-grab").classList.add("hidden");
}

async function nb1Load(reset, force) {
  const n = AL.nb1;
  if (n.loading) return;
  if (reset) { n.offset = 0; $("#nb1-grid").innerHTML = `<div class="hint">Loading…</div>`; }
  n.loading = true;
  try {
    const qs = new URLSearchParams();
    qs.set("limit", n.limit); qs.set("offset", n.offset); qs.set("sort", n.sort);
    if (n.filter) qs.set("query", n.filter);
    if (force) qs.set("force", "true");
    const r = await api(`/api/nb1/videos?${qs.toString()}`);
    n.total = r.total || 0;
    if (reset) $("#nb1-grid").innerHTML = "";
    const vids = r.videos || [];
    vids.forEach(v => $("#nb1-grid").appendChild(nb1Card(v)));
    if (reset && !vids.length) $("#nb1-grid").innerHTML = `<div class="hint">No videos match.</div>`;
    n.offset += vids.length;
    $("#nb1-count").textContent = `${n.total} video${n.total === 1 ? "" : "s"}`;
    $("#nb1-more").classList.toggle("hidden", n.offset >= n.total);
  } catch (e) {
    $("#nb1-grid").innerHTML = `<div class="hint">Error: ${e.message}</div>`;
  } finally { n.loading = false; }
}

function nb1Card(v) {
  const el = document.createElement("div");
  el.className = "result nb1-card";
  const dur = fmtDur(v.duration), views = fmtViews(v.view_count);
  el.innerHTML = `
    <img src="${v.thumbnail || ""}" onerror="this.style.visibility='hidden'"/>
    <div class="meta">
      <div class="title">${v.title || "(untitled)"}</div>
      <div class="sub">
        ${dur ? `<span class="dur dur-medium">${dur}</span>` : ""}
        ${views ? `<span class="vc">${views} views</span>` : ""}
      </div>
    </div>
    <div class="res-actions"><button class="res-get">Scrub ✂</button></div>`;
  el.querySelector(".res-get").onclick = () => nb1OpenGrab(v);
  return el;
}

async function nb1OpenGrab(v) {
  const n = AL.nb1;
  n.cur = v; n.in = 0; n.out = v.duration || null; n.dur = v.duration || 0; n.drag = null;
  $("#nb1-grab-title").textContent = v.title || "";
  $("#nb1-browse").classList.add("hidden");
  $("#nb1-grab").classList.remove("hidden");
  const vid = $("#nb1-video");
  vid.removeAttribute("src"); vid.poster = v.thumbnail || "";
  $("#nb1-sel-len").textContent = "Loading stream…";
  nb1Layout(); nb1SyncFields();
  try {
    const s = await api("/api/nb1/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: v.url }),
    });
    if (n.cur !== v) return;                       // user backed out while resolving
    n.dur = s.duration || v.duration || 0;
    if (n.out == null || n.out > n.dur) n.out = n.dur;
    vid.src = s.url; vid.load();
    nb1Layout(); nb1SyncFields();
  } catch (e) {
    if (n.cur === v) $("#nb1-sel-len").textContent = "Stream failed: " + e.message;
  }
}

function nb1Layout() {
  const n = AL.nb1;
  const out = n.out ?? n.dur;
  const inP = nb1Pct(n.in), outP = nb1Pct(out);
  $("#nb1-scrub .nb1-handle-in").style.left = inP + "%";
  $("#nb1-scrub .nb1-handle-out").style.left = outP + "%";
  const sel = $("#nb1-scrub .nb1-scrub-sel");
  sel.style.left = inP + "%"; sel.style.width = Math.max(0, outP - inP) + "%";
  const len = Math.max(0, out - n.in);
  $("#nb1-sel-len").textContent =
    `Selection: ${nb1FmtT(len)}  (${nb1FmtT(n.in)} → ${nb1FmtT(out)})`;
}
function nb1SyncFields() {
  $("#nb1-in").value = nb1FmtT(AL.nb1.in);
  $("#nb1-out").value = AL.nb1.out == null ? "" : nb1FmtT(AL.nb1.out);
}

// Scrub bar: drag the in/out handles (video preview follows the drag); a plain
// click on the track just seeks the playhead.
function initNb1Scrub() {
  const bar = $("#nb1-scrub"), vid = $("#nb1-video");
  const timeAt = (clientX) => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * (AL.nb1.dur || 0);
  };
  bar.addEventListener("pointerdown", (e) => {
    const h = e.target.closest(".nb1-handle");
    if (h) {
      AL.nb1.drag = h.dataset.h;
      bar.setPointerCapture(e.pointerId);
      e.preventDefault();
    } else {
      const t = timeAt(e.clientX);
      if (isFinite(t)) vid.currentTime = t;        // seek only
    }
  });
  bar.addEventListener("pointermove", (e) => {
    const n = AL.nb1;
    if (!n.drag) return;
    let t = timeAt(e.clientX);
    if (n.drag === "in") n.in = Math.max(0, Math.min(t, (n.out ?? n.dur) - 0.1));
    else n.out = Math.min(n.dur, Math.max(t, n.in + 0.1));
    if (isFinite(t)) vid.currentTime = t;          // preview follows the handle
    nb1Layout(); nb1SyncFields();
  });
  const end = (e) => {
    if (AL.nb1.drag) { AL.nb1.drag = null; try { bar.releasePointerCapture(e.pointerId); } catch { /* */ } }
  };
  bar.addEventListener("pointerup", end);
  bar.addEventListener("pointercancel", end);

  vid.addEventListener("timeupdate", () => {
    $("#nb1-scrub .nb1-playhead").style.left = nb1Pct(vid.currentTime) + "%";
  });
  vid.addEventListener("play", () => { $("#nb1-play").textContent = "❚❚"; });
  vid.addEventListener("pause", () => { $("#nb1-play").textContent = "▶"; });
}

async function nb1DoGrab() {
  const n = AL.nb1;
  if (!n.cur || !n.ctx) return;
  const btn = $("#nb1-do-grab");
  let s = n.in, e = n.out;
  // whole-video selection -> full download (null/null); else validate the span
  if (s <= 0.05 && (e == null || e >= n.dur - 0.05)) { s = null; e = null; }
  else if (e != null && e <= s) return toast("Out must be after In", true);
  btn.disabled = true; btn.textContent = "Grabbing…";
  try {
    await n.ctx.onGrab({ url: n.cur.url, start: s, end: e, title: n.cur.title }, btn);
    closeNb1();
  } catch { btn.disabled = false; btn.textContent = "Grab section"; }
}

// Destination callbacks -------------------------------------------------------
function nb1GrabToLibrary({ url, start, end }, btn) {
  return downloadClip(url, btn, start, end, $("#nb1-keep").checked);
}
function nb1GrabToRank({ url, start, end }, btn) {
  return downloadAndAssign({ url }, btn, start, end);   // uses rankSearchIndex
}

function initNb1() {
  AL.nb1 = { sort: "recent", filter: "", offset: 0, limit: 24, total: 0,
             loading: false, ctx: null, cur: null, dur: 0, in: 0, out: null, drag: null };

  $("#v-nb1").onclick = () => openNb1(nb1GrabToLibrary, "Library");
  $("#rs-nb1").onclick = () => {
    if (rankSearchIndex == null) return;
    openNb1(nb1GrabToRank, `Rank #${rankSearchIndex + 1}`);
  };

  $("#nb1-close").onclick = closeNb1;
  $("#nb1-modal").addEventListener("click", e => { if (e.target.id === "nb1-modal") closeNb1(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#nb1-modal").classList.contains("hidden")) closeNb1();
  });
  $("#nb1-back").onclick = () => { try { $("#nb1-video").pause(); } catch { /* */ } nb1ShowBrowse(); };
  $("#nb1-sort").onchange = () => { AL.nb1.sort = $("#nb1-sort").value; nb1Load(true); };
  $("#nb1-refresh").onclick = () => nb1Load(true, true);
  $("#nb1-more").onclick = () => nb1Load(false);
  let ft;
  $("#nb1-filter").addEventListener("input", () => {
    clearTimeout(ft);
    ft = setTimeout(() => { AL.nb1.filter = $("#nb1-filter").value.trim(); nb1Load(true); }, 250);
  });

  $("#nb1-play").onclick = () => { const v = $("#nb1-video"); if (v.paused) v.play(); else v.pause(); };
  $("#nb1-set-in").onclick = () => {
    AL.nb1.in = Math.max(0, Math.min($("#nb1-video").currentTime, (AL.nb1.out ?? AL.nb1.dur) - 0.1));
    nb1Layout(); nb1SyncFields();
  };
  $("#nb1-set-out").onclick = () => {
    AL.nb1.out = Math.min(AL.nb1.dur, Math.max($("#nb1-video").currentTime, AL.nb1.in + 0.1));
    nb1Layout(); nb1SyncFields();
  };
  $("#nb1-in").addEventListener("change", () => {
    const t = parseTime($("#nb1-in").value) ?? 0;
    AL.nb1.in = Math.max(0, Math.min(t, (AL.nb1.out ?? AL.nb1.dur) - 0.1));
    nb1Layout(); nb1SyncFields();
  });
  $("#nb1-out").addEventListener("change", () => {
    const t = parseTime($("#nb1-out").value);
    AL.nb1.out = t == null ? AL.nb1.dur : Math.min(AL.nb1.dur, Math.max(t, AL.nb1.in + 0.1));
    nb1Layout(); nb1SyncFields();
  });
  $("#nb1-do-grab").onclick = nb1DoGrab;

  initNb1Scrub();
}

// ------------------------------------------------------------------ music bed
// Formatter background music: pick from the license-checked catalog, grab a
// track into local storage, preview it, and lay it under the built render.
function initFmtMusic() {
  AL.fmtMusic = { track: null, title: null, artist: null, tier: null, credit: null,
                  file: null, downloaded: false, catalog: null,
                  volume: 0.28, keep: true, clipVol: 1.0, start: 0, fade: 2 };

  $("#fmt-music-pick").onclick = openMusicPicker;
  $("#fmt-music-clear").onclick = clearMusic;
  $("#music-close").onclick = closeMusicPicker;
  $("#music-modal").addEventListener("click", e => { if (e.target.id === "music-modal") closeMusicPicker(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#music-modal").classList.contains("hidden")) closeMusicPicker();
  });
  $("#music-filter").addEventListener("input", renderMusicList);
  $("#music-tier").addEventListener("change", renderMusicList);

  const vol = $("#fmt-music-vol"), cv = $("#fmt-music-clipvol");
  vol.addEventListener("input", () => { AL.fmtMusic.volume = +vol.value / 100; $("#fmt-music-vol-v").textContent = vol.value + "%"; });
  cv.addEventListener("input", () => { AL.fmtMusic.clipVol = +cv.value / 100; $("#fmt-music-clipvol-v").textContent = cv.value + "%"; });
  $("#fmt-music-keep").addEventListener("change", e => {
    AL.fmtMusic.keep = e.target.checked;
    $("#fmt-music-clipvol-wrap").style.opacity = e.target.checked ? "" : ".4";
  });
  $("#fmt-music-start").addEventListener("change", e => { AL.fmtMusic.start = Math.max(0, +e.target.value || 0); });
  $("#fmt-music-fade").addEventListener("change", e => { AL.fmtMusic.fade = Math.max(0, +e.target.value || 0); });
  $("#fmt-music-start-set").onclick = () => {
    const t = Math.floor($("#fmt-music-audio").currentTime || 0);
    $("#fmt-music-start").value = t; AL.fmtMusic.start = t;
    toast("Bed starts at " + t + "s into the track");
  };
  $("#fmt-credit-copy").onclick = async () => {
    await navigator.clipboard.writeText($("#fmt-credit-text").textContent);
    toast("Credit copied — paste into the description");
  };
}

// Subscribe prompt: a clean, audioless VALDaily chip on the last X seconds of the
// finished ranking video. Mirrors the music-bed control pattern.
function initFmtSub() {
  AL.fmtSub = { on: false, style: "classic", dur: 4.0, scale: 1.0, anim: 0.7, sound: "pop",
                accent: "#ff0033", matchTitle: false };
  const on = $("#fmt-sub-on"), ctrls = $("#fmt-sub-ctrls");
  const dur = $("#fmt-sub-dur"), sc = $("#fmt-sub-scale"), scv = $("#fmt-sub-scale-v");
  const anim = $("#fmt-sub-anim"), animv = $("#fmt-sub-anim-v"), snd = $("#fmt-sub-sound");
  const styleSeg = $("#fmt-sub-style");
  const match = $("#fmt-sub-match"), color = $("#fmt-sub-color"), colorRow = $("#fmt-sub-color-row");
  const syncColorRow = () => { if (colorRow) colorRow.classList.toggle("hidden", !!AL.fmtSub.matchTitle); };
  on.addEventListener("change", () => {
    AL.fmtSub.on = on.checked;
    ctrls.classList.toggle("hidden", !on.checked);
  });
  styleSeg.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    AL.fmtSub.style = b.dataset.v;
    styleSeg.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
  });
  dur.addEventListener("change", () => {
    AL.fmtSub.dur = Math.max(1, Math.min(+dur.value || 4, 10));
    dur.value = AL.fmtSub.dur;
  });
  sc.addEventListener("input", () => {
    AL.fmtSub.scale = +sc.value;
    scv.textContent = Math.round(AL.fmtSub.scale * 100) + "%";
  });
  anim.addEventListener("input", () => {
    AL.fmtSub.anim = +anim.value;
    animv.textContent = AL.fmtSub.anim.toFixed(2) + "s";
  });
  snd.addEventListener("change", () => { AL.fmtSub.sound = snd.value; });
  if (match) match.addEventListener("change", () => {
    AL.fmtSub.matchTitle = match.checked; syncColorRow();
  });
  if (color) color.addEventListener("input", () => { AL.fmtSub.accent = color.value; });
  syncColorRow();
}

// Clip Hook: a framed clip-moment cold-open on the opening of the Top-N. The
// backend overlays it + holds the first segment's reveal until it shrinks away.
const HOOK_COLORS = ["#2ea6ff", "#ff0033", "#f5b942", "#59e0c5", "#a259ff", "#ffffff"];
function initFmtHook() {
  AL.fmtHook = { on: false, clip: null, moment: 0, length: 1.5, in_speed: 1, out_speed: 1,
                 style: "glow", accent: "#2ea6ff", sound: "whoosh", volume: 1, freeze_bg: false };
  const on = $("#fmt-hook-on"), ctrls = $("#fmt-hook-ctrls");
  const styleSeg = $("#fmt-hook-style"), sw = $("#fmt-hook-swatches");
  const moment = $("#fmt-hook-moment"), len = $("#fmt-hook-length"), lenv = $("#fmt-hook-length-v");
  const outsp = $("#fmt-hook-outspeed"), outspv = $("#fmt-hook-outspeed-v");
  const snd = $("#fmt-hook-sound");
  on.addEventListener("change", () => {
    AL.fmtHook.on = on.checked;
    ctrls.classList.toggle("hidden", !on.checked);
    if (on.checked) renderHookSrc();
  });
  styleSeg.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    AL.fmtHook.style = b.dataset.v;
    styleSeg.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
  });
  // accent swatches
  sw.innerHTML = "";
  HOOK_COLORS.forEach(c => {
    const b = document.createElement("button");
    b.style.background = c;
    b.dataset.hex = c;
    b.classList.toggle("active", c === AL.fmtHook.accent);
    b.addEventListener("click", () => {
      AL.fmtHook.accent = c;
      sw.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
    });
    sw.appendChild(b);
  });
  moment.addEventListener("change", () => { AL.fmtHook.moment = Math.max(0, +moment.value || 0); });
  len.addEventListener("input", () => {
    AL.fmtHook.length = +len.value; lenv.textContent = AL.fmtHook.length.toFixed(1) + "s";
  });
  outsp.addEventListener("input", () => {
    AL.fmtHook.out_speed = +outsp.value; outspv.textContent = AL.fmtHook.out_speed.toFixed(1) + "×";
  });
  snd.addEventListener("change", () => { AL.fmtHook.sound = snd.value; });
  const hvol = $("#fmt-hook-vol"), hvolv = $("#fmt-hook-vol-v");
  hvol.addEventListener("input", () => {
    AL.fmtHook.volume = +hvol.value / 100; hvolv.textContent = hvol.value + "%";
  });
  $("#fmt-hook-freeze").addEventListener("change", (e) => { AL.fmtHook.freeze_bg = e.target.checked; });
  $("#fmt-hook-preview").addEventListener("click", (e) => previewHook(e.currentTarget));
}

// Source-clip buttons: "Auto" (first-played) + one per filled rank. Selecting a
// rank pins the hook to that rank's clip identity.
function renderHookSrc() {
  const box = $("#fmt-hook-src"); if (!box) return;
  const filled = (AL.fmt.ranks || []).filter(r => r.clip);
  const cur = AL.fmtHook.clip;
  const mk = (label, clip) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.classList.toggle("active", (clip || null) === (cur || null));
    b.addEventListener("click", () => { AL.fmtHook.clip = clip || null; renderHookSrc(); });
    return b;
  };
  box.innerHTML = "";
  box.appendChild(mk("Auto (first)", null));
  filled.forEach((r, i) => box.appendChild(mk(r.label ? `#${i + 1} ${r.label}` : `Clip ${i + 1}`, r.clip)));
}

async function previewHook(btn) {
  const hk = AL.fmtHook;
  if (!hk || !hk.on) return toast("Turn the hook on first", true);
  if (!AL.fmt.ranks.some(r => r.clip)) return toast("Assign a clip to at least one rank", true);
  const payload = buildFmtPayload();
  payload.hook_preview = true;
  delete payload.music; delete payload.subscribe;   // cold-open only, fast
  const status = $("#fmt-hook-status");
  if (btn) { btn.disabled = true; btn.textContent = "▶ …"; }
  status.textContent = "Rendering hook…";
  try {
    const { job } = await api("/api/formatter/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await new Promise(resolve => {
      const tick = async () => {
        try {
          const s = await api("/api/jobs/" + job);
          if (s.status === "running" || s.status === "queued") { status.textContent = "Rendering…"; setTimeout(tick, 900); }
          else resolve(s);
        } catch (e) { resolve({ status: "error", error: e.message }); }
      };
      tick();
    });
    if (j.status === "done") { status.textContent = "Hook preview ✓"; openPlayer(j.output, "Clip Hook preview"); }
    else { status.textContent = ""; toast("Hook preview error: " + (j.error || "unknown"), true); }
  } catch (e) { status.textContent = ""; toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "▶ Preview hook"; } }
}

async function openMusicPicker() {
  $("#music-modal").classList.remove("hidden");
  if (!AL.fmtMusic.catalog) {
    $("#music-list").innerHTML = `<div class="hint">Loading…</div>`;
    try { AL.fmtMusic.catalog = await api("/api/music/catalog"); }
    catch (e) { $("#music-list").innerHTML = `<div class="hint">Error: ${e.message}</div>`; return; }
  }
  renderMusicList();
}
function closeMusicPicker() {
  $("#music-modal").classList.add("hidden");
  $$("#music-list audio").forEach(a => { try { a.pause(); } catch { /* */ } });
}

function renderMusicList() {
  const cat = AL.fmtMusic.catalog; if (!cat) return;
  const q = $("#music-filter").value.trim().toLowerCase();
  const tier = $("#music-tier").value;
  const box = $("#music-list"); box.innerHTML = ""; let shown = 0;
  cat.genres.forEach(g => {
    const rows = g.tracks.filter(t =>
      (!tier || t.tier === tier) &&
      (!q || (t.title + " " + t.artist).toLowerCase().includes(q)));
    if (!rows.length) return;
    const h = document.createElement("div"); h.className = "result-divider";
    h.textContent = g.label; box.appendChild(h);
    rows.forEach(t => { box.appendChild(musicRow(t)); shown++; });
  });
  if (!shown) box.innerHTML = `<div class="hint">No tracks match.</div>`;
  $("#music-hint").textContent = shown ? `${shown} tracks` : "";
}

function musicRow(t) {
  const el = document.createElement("div");
  el.className = "result music-row";
  const isSel = AL.fmtMusic.track === t.id;
  el.innerHTML = `
    <div class="meta">
      <div class="title"><span class="mtier mtier-${t.tier}">${t.tier}</span> ${t.title}</div>
      <div class="sub"><span class="ch">${t.artist}</span> <span class="vc">${t.license}</span></div>
    </div>
    <div class="res-actions">
      ${t.downloaded
        ? `<audio class="music-prev" src="${t.file}" controls preload="none"></audio>
           <button class="res-get music-use">${isSel ? "✓ In use" : "Use"}</button>`
        : `<button class="res-get music-grab">Grab ↓</button>`}
    </div>`;
  const grab = el.querySelector(".music-grab");
  if (grab) grab.onclick = () => grabMusic(t, grab);
  const use = el.querySelector(".music-use");
  if (use) use.onclick = () => selectMusic(t);
  return el;
}

async function grabMusic(t, btn) {
  btn.disabled = true; btn.textContent = "Grabbing…";
  try {
    const res = await api("/api/music/grab", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track: t.id }),
    });
    t.downloaded = true; t.file = res.file;
    toast(`Grabbed: ${t.title}`);
    renderMusicList();
  } catch (e) {
    btn.disabled = false; btn.textContent = "Grab ↓";
    toast("Grab failed — try the source link. " + e.message, true);
  }
}

function selectMusic(t) {
  const m = AL.fmtMusic;
  m.track = t.id; m.title = t.title; m.artist = t.artist; m.tier = t.tier;
  m.credit = t.credit; m.file = t.file; m.downloaded = true;
  $("#fmt-music-sel").innerHTML = `<b>${t.title}</b> — ${t.artist} <span class="mtier mtier-${t.tier}">${t.tier}</span>`;
  $("#fmt-music-clear").classList.remove("hidden");
  $("#fmt-music-ctrls").classList.remove("hidden");
  $("#fmt-music-audio").src = t.file;
  $("#fmt-music-note").textContent = t.credit
    ? "NCS track — a paste-ready credit line appears after you render."
    : (t.tier === "A" ? "Bundle-safe (CC-BY) — no credit needed."
      : "Pixabay — no credit needed; use in output only.");
  closeMusicPicker();
  toast(`Music bed: ${t.title}`);
}

function clearMusic() {
  const m = AL.fmtMusic;
  m.track = null; m.credit = null; m.file = null; m.downloaded = false;
  $("#fmt-music-sel").textContent = "No track — clip audio only.";
  $("#fmt-music-clear").classList.add("hidden");
  $("#fmt-music-ctrls").classList.add("hidden");
  $("#fmt-music-audio").removeAttribute("src");
}

// ------------------------------------------------------------------ import
async function doImport(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  toast("Importing…");
  try {
    const res = await api("/api/clips/import", { method: "POST", body: fd });
    toast("Imported: " + res.name);
    await loadLibrary();
  } catch (e) { toast("Import failed: " + e.message, true); }
}

// ------------------------------------------------------------------ library
let selectedClip = null;
async function loadLibrary() {
  const data = await api("/api/clips");        // { target, folders, creators, tags }
  AL.libFolders = data.folders || [];
  AL.libTarget = data.target;
  AL.libClips = AL.libFolders.flatMap(f => f.clips);   // flat list for the rank picker
  AL.libRoster = data.creator_roster || [];            // every player + streamer + custom (for the picker)
  AL.libCreators = [...new Set(AL.libClips.map(c => c.creator).filter(Boolean))]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));   // creators actually in use (filter chips)
  AL.libTags = data.tags || [];
  renderLibrary();                              // Search-panel picker
  if (AL._libTabInit) renderClipBrowser($("#lib-browser"), { mode: "manage" });   // Library tab
}

// The Search-panel "Your clips" picker is the browser in compact pick mode.
function renderLibrary() {
  renderClipBrowser($("#v-library"), {
    mode: "pick", compact: true,
    onPick: c => selectClip(c),
    activePath: () => selectedClip,
  });
}

// ==================================================================
// Clip browser — one folder + thumbnail UI used everywhere (Library tab, the
// Search editor's "your clips", and the rank/move pickers). No dropdowns:
// folders with thumbnail cards carrying favorite ⭐ / #tags / creator, plus
// duplicate / move / pin / delete. Filter by search / favorites / creator / tag.
// ==================================================================
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m])); }

function cbFilterState(box) {
  if (!box._cbF) box._cbF = { q: "", fav: false, creator: "", tag: "" };
  return box._cbF;
}
function cbMatch(c, f) {
  if (f.fav && !c.favorite) return false;
  if (f.creator && (c.creator || "").toLowerCase() !== f.creator.toLowerCase()) return false;
  if (f.tag && !(c.tags || []).includes(f.tag)) return false;
  if (f.q) {
    const hay = (c.name + " " + (c.creator || "") + " " + (c.tags || []).join(" ")).toLowerCase();
    if (!hay.includes(f.q.toLowerCase())) return false;
  }
  return true;
}
const cbActive = f => !!(f.fav || f.creator || f.tag || f.q);

function renderClipBrowser(box, cfg) {
  if (!box) return;
  box._cbCfg = cfg;
  const f = cbFilterState(box);
  box.className = "cb" + (cfg.compact ? " cb-compact" : "") + (cfg.mode === "pick" ? " cb-pick" : " cb-manage");
  box.innerHTML = "";

  // ---- toolbar (search + favorites + folder ops) ----
  const bar = document.createElement("div");
  bar.className = "cb-toolbar";
  bar.innerHTML = `
    <input class="cb-search" placeholder="Search name · #tag · creator" value="${esc(f.q)}">
    <button class="cb-chip${f.fav ? " on" : ""}" data-role="fav" type="button">⭐ Favorites</button>
    ${cfg.mode === "manage" ? `
      <span class="cb-spacer"></span>
      <span class="cb-target" title="New downloads land here">⤓ <b>${esc(AL.libTarget || "default")}</b></span>
      <button class="cb-btn cb-newfolder" type="button">+ Folder</button>
      <button class="cb-btn cb-refresh" type="button" title="Refresh">↻</button>` : ""}`;
  box.appendChild(bar);
  bar.querySelector(".cb-search").oninput = e => { f.q = e.target.value; cbBody(box); };
  bar.querySelector('[data-role="fav"]').onclick = () => { f.fav = !f.fav; renderClipBrowser(box, cfg); };
  if (cfg.mode === "manage") {
    bar.querySelector(".cb-newfolder").onclick = newFolder;
    bar.querySelector(".cb-refresh").onclick = loadLibrary;
  }

  // ---- filter chips: creators + tags ----
  const creators = AL.libCreators || [], tags = AL.libTags || [];
  if (creators.length || tags.length) {
    const fb = document.createElement("div");
    fb.className = "cb-filters";
    const cChips = creators.map(c => `<button class="cb-chip sm${f.creator === c ? " on" : ""}" data-creator="${esc(c)}" type="button">👤 ${esc(c)}</button>`).join("");
    const tChips = tags.map(t => `<button class="cb-chip sm${f.tag === t ? " on" : ""}" data-tag="${esc(t)}" type="button">#${esc(t)}</button>`).join("");
    fb.innerHTML = (cChips ? `<span class="cb-flabel">Creators</span>${cChips}` : "") +
      (tChips ? `<span class="cb-flabel">Tags</span>${tChips}` : "");
    box.appendChild(fb);
    fb.querySelectorAll("[data-creator]").forEach(b => b.onclick = () => { f.creator = f.creator === b.dataset.creator ? "" : b.dataset.creator; renderClipBrowser(box, cfg); });
    fb.querySelectorAll("[data-tag]").forEach(b => b.onclick = () => { f.tag = f.tag === b.dataset.tag ? "" : b.dataset.tag; renderClipBrowser(box, cfg); });
  }

  const body = document.createElement("div");
  body.className = "cb-body";
  box.appendChild(body);
  cbBody(box);
}

// Re-render just the results region (keeps toolbar focus while typing).
function cbBody(box) {
  const cfg = box._cbCfg, f = cbFilterState(box);
  const body = box.querySelector(".cb-body");
  if (!body) return;
  body.innerHTML = "";
  const folders = AL.libFolders || [];

  if (!(AL.libClips || []).length) {
    body.innerHTML = `<div class="cb-empty">Empty — search, grab, or import a clip in the <b>Search</b> tab.</div>`;
    return;
  }

  if (cbActive(f)) {                       // filtered → flat matching grid
    const hits = (AL.libClips || []).filter(c => cbMatch(c, f));
    const head = document.createElement("div");
    head.className = "cb-results-head";
    head.innerHTML = `<span>${hits.length} match${hits.length === 1 ? "" : "es"}</span><button class="cb-clear" type="button">✕ clear filters</button>`;
    head.querySelector(".cb-clear").onclick = () => { box._cbF = { q: "", fav: false, creator: "", tag: "" }; renderClipBrowser(box, cfg); };
    body.appendChild(head);
    const grid = document.createElement("div");
    grid.className = "cb-grid";
    if (!hits.length) grid.innerHTML = `<div class="cb-empty">No clips match.</div>`;
    hits.forEach(c => grid.appendChild(cbCard(c, cfg)));
    body.appendChild(grid);
    return;
  }

  // unfiltered → folder groups
  folders.forEach(fld => {
    const grp = document.createElement("div");
    grp.className = "cb-folder";
    const head = document.createElement("div");
    head.className = "cb-folder-head";
    head.innerHTML = `
      <span class="cb-fname">${esc(fld.name)}</span>
      ${fld.target ? `<span class="cb-badge tgt">↓ saves here</span>` : ""}
      <span class="cb-fcount">${fld.clips.length}</span>
      ${cfg.mode === "manage" ? `<span class="cb-factions">
        ${fld.target ? "" : `<button class="cb-fa settarget" type="button" title="Make this the download target">⤓</button>`}
        <button class="cb-fa rename" type="button" title="Rename folder">✎</button>
        ${fld.name !== "default" ? `<button class="cb-fa del" type="button" title="Delete (clips move to default)">🗑</button>` : ""}
      </span>` : ""}`;
    grp.appendChild(head);
    if (cfg.mode === "manage") {
      const st = head.querySelector(".settarget"); if (st) st.onclick = () => setDownloadTarget(fld.name);
      head.querySelector(".rename").onclick = () => renameFolder(fld.name);
      const del = head.querySelector(".del"); if (del) del.onclick = () => deleteFolder(fld.name, fld.clips.length);
    }
    const grid = document.createElement("div");
    grid.className = "cb-grid";
    if (!fld.clips.length) grid.innerHTML = `<div class="cb-fempty">empty</div>`;
    fld.clips.forEach(c => grid.appendChild(cbCard(c, cfg)));
    grp.appendChild(grid);
    body.appendChild(grp);
  });
}

function cbCard(c, cfg) {
  const el = document.createElement("div");
  const active = cfg.activePath && cfg.activePath() === c.path;
  el.className = "cb-card" + (active ? " active" : "") + (c.favorite ? " fav" : "");
  const tagsHtml = (c.tags || []).map(t => `<span class="cb-tag">#${esc(t)}</span>`).join("");
  el.innerHTML = `
    <div class="cb-thumb">
      <img loading="lazy" src="${esc(c.thumb)}" alt="" onerror="this.classList.add('noimg')">
      <button class="cb-play" type="button" title="Preview (play without opening the editor)">▶</button>
      <button class="cb-star${c.favorite ? " on" : ""}" type="button" title="Favorite">${c.favorite ? "★" : "☆"}</button>
      ${c.kept ? `<span class="cb-pin" title="Pinned — won't auto-clear">📌</span>` : ""}
      ${cfg.mode === "pick" ? `<span class="cb-pickhint">Use this clip →</span>` : ""}
    </div>
    <div class="cb-info">
      <div class="cb-name" title="${esc(c.name)}">${esc(c.name)}</div>
      <div class="cb-meta">
        ${c.creator ? `<span class="cb-creator">👤 ${esc(c.creator)}</span>` : ""}
        <span class="cb-size">${fmtSize(c.size)}</span>
      </div>
      <div class="cb-tags">${tagsHtml}${cfg.mode === "manage" ? `<button class="cb-tagadd" type="button" title="Edit tags">＋#</button>` : ""}</div>
    </div>
    ${cfg.mode === "manage" ? `<button class="cb-kebab" type="button" title="More">⋯</button>` : ""}`;

  el.querySelector(".cb-star").onclick = async e => { e.stopPropagation(); await setFavorite(c.path, !c.favorite); };
  el.querySelector(".cb-play").onclick = e => {
    e.stopPropagation();
    openPlayer(c.url || `/storage/clips/${c.path}`, c.name, {
      actions: [{ label: "✎ Open in editor", primary: true, onClick: () => { closePlayer(); openInEditor(c); } }],
    });
  };

  if (cfg.mode === "pick") {
    el.querySelector(".cb-thumb").onclick = () => cfg.onPick && cfg.onPick(c);
    el.querySelector(".cb-name").onclick = () => cfg.onPick && cfg.onPick(c);
  } else {
    el.querySelector(".cb-tagadd").onclick = e => { e.stopPropagation(); openTagEditor(e.currentTarget, c); };
    el.querySelector(".cb-kebab").onclick = e => { e.stopPropagation(); openClipMenu(e.currentTarget, c); };
    const cr = el.querySelector(".cb-creator");
    if (cr) cr.onclick = e => { e.stopPropagation(); openCreatorPicker(e.currentTarget, c); };
    el.querySelector(".cb-thumb").onclick = () => openInEditor(c);
  }
  return el;
}

// ---- metadata mutations (all refresh the library) ----
async function setFavorite(path, value) {
  try { await api("/api/clips/favorite", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, value }) }); await loadLibrary(); }
  catch (e) { toast("Failed: " + e.message, true); }
}
async function setClipTags(path, tags) {
  try { await api("/api/clips/tags", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, tags }) }); await loadLibrary(); }
  catch (e) { toast("Failed: " + e.message, true); }
}
async function setClipCreator(path, creator) {
  try { await api("/api/clips/creator", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, creator }) }); await loadLibrary(); }
  catch (e) { toast("Failed: " + e.message, true); }
}
async function duplicateClipFile(path) {
  try { const r = await api("/api/clips/duplicate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) }); toast("Duplicated"); await loadLibrary(); return r.path; }
  catch (e) { toast("Duplicate failed: " + e.message, true); }
}
function openInEditor(c) {
  selectTab("search", AL.tabBtns["search"]);
  selectSubTab($('[data-niche="search"]'), "valorant");
  selectClip(c);
}

// ---- floating popovers (dropdown-free menus) ----
const CB = {};   // shared transient popover state (the one open popover)
function closePopover() { if (CB.pop) { CB.pop.remove(); CB.pop = null; document.removeEventListener("mousedown", CB.popOutside, true); } }
function openPopover(anchor, html, wire) {
  closePopover();
  const p = document.createElement("div");
  p.className = "cb-popover";
  p.innerHTML = html;
  document.body.appendChild(p);
  const r = anchor.getBoundingClientRect();
  let top = r.bottom + 6, left = Math.min(r.left, window.innerWidth - p.offsetWidth - 12);
  if (top + p.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - p.offsetHeight - 6);
  p.style.top = top + "px"; p.style.left = Math.max(8, left) + "px";
  CB.pop = p;
  CB.popOutside = e => { if (!p.contains(e.target) && e.target !== anchor) closePopover(); };
  setTimeout(() => document.addEventListener("mousedown", CB.popOutside, true), 0);
  wire && wire(p);
  return p;
}

function openClipMenu(anchor, c) {
  const others = (AL.libFolders || []).map(f => f.name).filter(n => n !== c.folder);
  openPopover(anchor, `
    <button class="cb-mi" data-a="edit">✎ Open in editor</button>
    <button class="cb-mi" data-a="dup">⧉ Duplicate</button>
    <button class="cb-mi" data-a="creator">👤 Set creator</button>
    <button class="cb-mi" data-a="tags"># Edit tags</button>
    <button class="cb-mi" data-a="pin">${c.kept ? "📌 Unpin" : "📍 Pin"}</button>
    <div class="cb-mi-sub">Move to</div>
    ${others.length ? others.map(n => `<button class="cb-mi move" data-move="${esc(n)}">→ ${esc(n)}</button>`).join("") : `<div class="cb-mi-none">no other folders</div>`}
    <button class="cb-mi danger" data-a="del">🗑 Delete</button>`, p => {
    p.querySelectorAll("[data-a]").forEach(b => b.onclick = async () => {
      const a = b.dataset.a; closePopover();
      if (a === "edit") openInEditor(c);
      else if (a === "dup") duplicateClipFile(c.path);
      else if (a === "creator") setTimeout(() => openCreatorPicker(anchor, c), 0);
      else if (a === "tags") setTimeout(() => openTagEditor(anchor, c), 0);
      else if (a === "pin") { await keepClip(c.path, !c.kept); loadLibrary(); }
      else if (a === "del") { if (confirm(`Delete "${c.name}"?`)) deleteClipFile(c.path); }
    });
    p.querySelectorAll("[data-move]").forEach(b => b.onclick = () => { closePopover(); moveClip(c.path, b.dataset.move); });
  });
}

// Creator picker: search the full roster (every player + streamer + custom names)
// and mark any clip under one — or type a brand-new name.
function openCreatorPicker(anchor, c) {
  const roster = AL.libRoster || [];
  const cur = c.creator || "";
  const rowsHtml = q => {
    q = q.trim().toLowerCase();
    let list = q ? roster.filter(r => r.name.toLowerCase().includes(q)) : roster.slice();
    // bubble the current pick to the top when unfiltered
    if (!q && cur) list.sort((a, b) => (b.name.toLowerCase() === cur.toLowerCase()) - (a.name.toLowerCase() === cur.toLowerCase()));
    if (!list.length) return `<span class="cb-mi-none">no match — type a name and hit Set</span>`;
    return list.slice(0, 60).map(r => `<button class="cb-rchip${r.name.toLowerCase() === cur.toLowerCase() ? " on" : ""}" data-c="${esc(r.name)}">${r.src ? `<img loading="lazy" src="${esc(r.src)}" onerror="this.remove()">` : `<span class="cb-rchip-ph">👤</span>`}<span>${esc(r.name)}</span></button>`).join("");
  };
  openPopover(anchor, `
    <div class="cb-pop-title">Creator${cur ? ` · <span class="cb-cur">${esc(cur)}</span>` : ""}</div>
    <div class="cb-pop-row"><input class="cb-pop-in" placeholder="Search players / streamers…"><button class="cb-btn" data-add>Set</button></div>
    <div class="cb-roster">${rowsHtml("")}</div>
    ${cur ? `<button class="cb-mi danger" data-clear>✕ Clear creator</button>` : ""}`, p => {
    const inp = p.querySelector(".cb-pop-in");
    const list = p.querySelector(".cb-roster");
    const wire = () => list.querySelectorAll("[data-c]").forEach(b => b.onclick = () => { closePopover(); setClipCreator(c.path, b.dataset.c); });
    inp.oninput = () => { list.innerHTML = rowsHtml(inp.value); wire(); };
    const add = () => { const v = inp.value.trim(); if (v) { closePopover(); setClipCreator(c.path, v); } };
    p.querySelector("[data-add]").onclick = add;
    inp.onkeydown = e => { if (e.key === "Enter") add(); };
    const cl = p.querySelector("[data-clear]"); if (cl) cl.onclick = () => { closePopover(); setClipCreator(c.path, ""); };
    wire(); inp.focus();
  });
}

function openTagEditor(anchor, c) {
  const cur = (c.tags || []).slice();
  const suggest = (AL.libTags || []).filter(t => !cur.includes(t));
  const chips = () => cur.map(t => `<span class="cb-tag edit">#${esc(t)}<button data-rm="${esc(t)}">✕</button></span>`).join("") || `<span class="cb-mi-none">no tags</span>`;
  openPopover(anchor, `
    <div class="cb-pop-title">Tags</div>
    <div class="cb-pop-tags">${chips()}</div>
    <div class="cb-pop-row"><input class="cb-pop-in" placeholder="add a tag (e.g. jett)"><button class="cb-btn" data-add>Add</button></div>
    ${suggest.length ? `<div class="cb-pop-sub">quick add</div><div class="cb-pop-chips">${suggest.slice(0, 12).map(t => `<button class="cb-chip sm" data-s="${esc(t)}">#${esc(t)}</button>`).join("")}</div>` : ""}`, p => {
    const redraw = () => { p.querySelector(".cb-pop-tags").innerHTML = chips(); wireRm(); };
    const wireRm = () => p.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { const i = cur.indexOf(b.dataset.rm); if (i >= 0) cur.splice(i, 1); setClipTags(c.path, cur); redraw(); });
    const inp = p.querySelector(".cb-pop-in");
    const add = v => { v = (v || "").replace(/^#/, "").trim().toLowerCase(); if (v && !cur.includes(v)) { cur.push(v); setClipTags(c.path, cur); redraw(); } inp.value = ""; };
    p.querySelector("[data-add]").onclick = () => add(inp.value);
    inp.onkeydown = e => { if (e.key === "Enter") add(inp.value); };
    p.querySelectorAll("[data-s]").forEach(b => b.onclick = () => add(b.dataset.s));
    wireRm(); inp.focus();
  });
}

// ---- modal clip picker (used by rank slots) ----
function openClipPicker(onPick, title) {
  let modal = $("#clip-pick");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "clip-pick"; modal.className = "modal-backdrop hidden";
    modal.innerHTML = `<div class="modal modal-wide"><div class="modal-head"><h3 id="clip-pick-title">Pick a clip</h3><button class="ghost" id="clip-pick-close" type="button">✕</button></div><div id="clip-pick-body"></div></div>`;
    document.body.appendChild(modal);
    modal.querySelector("#clip-pick-close").onclick = () => modal.classList.add("hidden");
    modal.onclick = e => { if (e.target === modal) modal.classList.add("hidden"); };
  }
  $("#clip-pick-title").textContent = title || "Pick a clip";
  const body = $("#clip-pick-body");
  body._cbF = null;                       // fresh filter each open
  modal.classList.remove("hidden");
  renderClipBrowser(body, { mode: "pick", onPick: c => { modal.classList.add("hidden"); onPick(c); } });
}

// ---- Library tab ----
function loadLibraryTab() {
  const panel = $("#library-panel");
  if (!panel) return;
  if (!AL._libTabInit) {
    panel.innerHTML = `
      <div class="lib-head">
        <h2>Library</h2>
        <p class="hint">All your clips, organized into folders. Favorite ⭐, tag with #labels, mark a creator, duplicate, and move — then pull them anywhere by filter.</p>
      </div>
      <div id="lib-browser"></div>`;
    AL._libTabInit = true;
  }
  renderClipBrowser($("#lib-browser"), { mode: "manage" });
}

// ==================================================================
// Channel overview (Valorant = live YouTube stats; Cars = placeholder)
// ==================================================================
async function loadOverview(niche) {
  const box = $(`#ov-${niche}`);
  if (!box) return;
  if (niche === "cars") {
    box.innerHTML = `<div class="ov-blank"><div class="ov-blank-ico">🚗</div><h2>Cars</h2>
      <p>No channel connected yet. This overview lights up once a Cars channel is wired in.</p></div>`;
    return;
  }
  if (!box._loaded) box.innerHTML = `<div class="ov-loading">Loading channel stats…</div>`;
  try {
    const d = await api(`/api/youtube/overview?niche=${niche}`);
    box._loaded = true;
    renderOverview(box, d);
  } catch (e) {
    box.innerHTML = `<div class="ov-blank"><h2>Couldn't load stats</h2><p>${esc(e.message)}</p></div>`;
  }
}

function renderOverview(box, d) {
  if (d.configured === false) { box.innerHTML = `<div class="ov-blank"><h2>${esc(d.niche)}</h2><p>No channel configured.</p></div>`; return; }
  if (d.authorized === false) {
    box.innerHTML = `<div class="ov-blank"><div class="ov-blank-ico">🔒</div><h2>YouTube not connected</h2>
      <p>Authorize the YouTube account to show live channel stats.${d.client_configured ? "" : " (client_secret.json missing)"}</p></div>`;
    return;
  }
  if (d.error) { box.innerHTML = `<div class="ov-blank"><h2>${esc(d.channel ? d.channel.title : "Channel")}</h2><p>Stats error: ${esc(d.error)}</p></div>`; return; }

  const ch = d.channel, t = d.totals || {}, pv = d.prev_totals || {};
  const pct = (cur, prev) => { if (!prev) return cur ? "+new" : "±0"; const p = Math.round(((cur - prev) / prev) * 100); return (p >= 0 ? "+" : "") + p + "%"; };
  const cls = (cur, prev) => cur >= prev ? "up" : "down";
  box.innerHTML = `
    <div class="ov-hero">
      ${ch.thumb ? `<img class="ov-avatar" src="${esc(ch.thumb)}" alt="">` : ""}
      <div class="ov-hero-info">
        <h2>${esc(ch.title)}</h2>
        <div class="ov-handle">${esc(ch.handle || "")}</div>
      </div>
      <div class="ov-window">last ${d.days} days</div>
    </div>
    <div class="ov-cards">
      ${statCard("Subscribers", fmtNum(ch.subscribers), `+${fmtNum(t.subs || 0)} this period`, cls(t.subs, pv.subs))}
      ${statCard("Views (lifetime)", fmtNum(ch.views), `${fmtNum(t.views || 0)} · ${pct(t.views, pv.views)} vs prev`, cls(t.views, pv.views))}
      ${statCard("Watch time", fmtHours(t.minutes || 0), `${pct(t.minutes, pv.minutes)} vs prev`, cls(t.minutes, pv.minutes))}
      ${statCard("Videos", fmtNum(ch.videos), `${fmtNum(t.likes || 0)} likes this period`, "flat")}
    </div>
    <div class="ov-grid">
      <div class="ov-panel ov-chart-panel">
        <div class="ov-panel-head"><h3>Views · last ${d.days} days</h3><span class="ov-chart-total">${fmtNum(t.views || 0)} views</span></div>
        <div class="ov-chart">${sparkline(d.series || [])}</div>
      </div>
      <div class="ov-panel">
        <div class="ov-panel-head"><h3>Recent uploads</h3></div>
        <div class="ov-vids">${(d.recent || []).map(videoRow).join("") || `<div class="hint">No uploads.</div>`}</div>
      </div>
    </div>
    <div class="ov-panel">
      <div class="ov-panel-head"><h3>Top videos</h3><span class="hint">by views</span></div>
      <div class="ov-vids ov-top">${(d.top || []).map(videoRow).join("")}</div>
    </div>`;
}

function statCard(label, big, sub, dir) {
  return `<div class="ov-card">
    <div class="ov-card-label">${esc(label)}</div>
    <div class="ov-card-big">${esc(big)}</div>
    <div class="ov-card-sub ${dir}">${esc(sub)}</div>
  </div>`;
}
function videoRow(v) {
  return `<a class="ov-vid" href="${esc(v.url)}" target="_blank" rel="noopener">
    <img loading="lazy" src="${esc(v.thumb)}" alt="" onerror="this.style.visibility='hidden'">
    <div class="ov-vid-info">
      <div class="ov-vid-title" title="${esc(v.title)}">${esc(v.title)}</div>
      <div class="ov-vid-meta">${fmtNum(v.views)} views · ${fmtNum(v.likes)} likes${v.comments ? " · " + fmtNum(v.comments) + " comments" : ""}</div>
    </div>
    <span class="ov-vid-dur">${fmtDur(v.duration) || ""}</span>
  </a>`;
}
// Inline SVG sparkline (no chart lib): area + line over the daily views series.
function sparkline(series) {
  if (!series.length) return `<div class="hint">No analytics data yet.</div>`;
  const W = 640, H = 150, pad = 6;
  const vals = series.map(p => p.views || 0);
  const max = Math.max(1, ...vals);
  const n = series.length;
  const x = i => pad + (i / Math.max(1, n - 1)) * (W - pad * 2);
  const y = v => H - pad - (v / max) * (H - pad * 2);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = "M" + pts.join(" L");
  const area = `M${x(0).toFixed(1)},${(H - pad).toFixed(1)} L${pts.join(" L")} L${x(n - 1).toFixed(1)},${(H - pad).toFixed(1)} Z`;
  const peak = vals.indexOf(Math.max(...vals));
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="ov-spark">
    <defs><linearGradient id="ovg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--accent)" stop-opacity="0.35"/>
      <stop offset="1" stop-color="var(--accent)" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#ovg)"/>
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(peak).toFixed(1)}" cy="${y(vals[peak]).toFixed(1)}" r="4" fill="var(--accent)"/>
  </svg>
  <div class="ov-spark-x"><span>${esc((series[0].date || "").slice(5))}</span><span>${esc((series[n - 1].date || "").slice(5))}</span></div>`;
}
const fmtNum = n => { n = +n || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : "" + n; };
const fmtHours = m => { m = +m || 0; const h = m / 60; return h >= 1 ? h.toFixed(h >= 10 ? 0 : 1) + " hrs" : Math.round(m) + " min"; };

async function setDownloadTarget(folder) {
  try {
    await api("/api/folders/target", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder }) });
    AL.libTarget = folder; toast(`Downloads now save to "${folder}"`); loadLibrary();
  } catch (e) { toast("Failed: " + e.message, true); }
}
async function newFolder() {
  const name = (prompt("New folder name:") || "").trim();
  if (!name) return;
  try {
    await api("/api/folders/create", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`Folder "${name}" created`); loadLibrary();
  } catch (e) { toast("Failed: " + e.message, true); }
}
async function renameFolder(name) {
  const to = (prompt(`Rename "${name}" to:`, name) || "").trim();
  if (!to || to === name) return;
  try {
    await api("/api/folders/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, to }) });
    toast("Renamed"); loadLibrary();
  } catch (e) { toast("Failed: " + e.message, true); }
}
async function deleteFolder(name, count) {
  if (!confirm(count ? `Delete "${name}"? Its ${count} clip(s) move to the default folder.` : `Delete empty folder "${name}"?`)) return;
  try {
    await api("/api/folders/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`Deleted "${name}"`); loadLibrary();
  } catch (e) { toast("Failed: " + e.message, true); }
}
async function moveClip(path, folder) {
  try {
    const r = await api("/api/clips/move", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, folder }) });
    // keep references consistent: the editor selection + any rank using this clip
    if (selectedClip === path) selectedClip = r.path;
    (AL.fmt.ranks || []).forEach(rk => { if (rk.clip === path) { rk.clip = r.path; if (rk.layout) rk.layout.clip = r.path; } });
    toast(`Moved to "${folder}"`); loadLibrary();
    if (typeof renderRankSlots === "function") renderRankSlots();
  } catch (e) { toast("Move failed: " + e.message, true); }
}
async function deleteClipFile(path) {
  try {
    await api("/api/clips/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
    toast("Deleted"); loadLibrary();
  } catch (e) { toast("Delete failed: " + e.message, true); }
}

// ------------------------------------------------------------------ auto-layout
// Two draggable regions in the SOURCE video, each keyed by name.
const BOXES = {
  facecam:  { elId: "al-box",    coord: "al-",    color: "red" },
  mousecam: { elId: "al-box-mc", coord: "al-mc-", color: "green" },
};
const AL = {
  natW: 0, natH: 0, duration: 0,
  rect: { facecam: null, mousecam: null }, cuts: [],
  // scrubber zoom: view = {start,end} sub-window shown on the bar (null = whole
  // clip); zoomFull = user chose to show the whole clip despite a small trim.
  view: null, zoomFull: false,
  // default Transition FX applied to newly-added cuts (set via the FX studio)
  fx: { autoApply: true, sound: "whoosh", visual: "swipeleft", volume: 1.0, speed: 2.0 },
  fxData: { sounds: [], visuals: [], effects: [] },  // catalog from /api/fx
  effects: [],                             // B-roll point effects on the current clip: {type, t}
  moment: { on: false, start: 0, end: 0 }, // "render just this span" window (source secs)
  pkg: { cur: null, sel: new Set(), unseen: 0 },     // clip-package viewer state
  // Video Formatter state (separate tool)
  fmt: { order: "desc", ranks: [] },       // ranks[i] = {label, clip, layout, join, playPos}
  fmtMeta: { templates: [], styles: [], fonts: [], joins: [], sounds: [], anim_modes: [], anim_motions: [] },
  // "Valorant Clip Ranking" editor state (ported from the Claude Design
  // "Shorts Ranking Templates" project). `off` holds per-style drag offsets
  // {tx,ty,lx,ly,ts,ls}, seeded from /api/formatter/meta styles.
  vce: {
    style: "pill", mode: "intro", target: 0,
    // Reveal Motion/Sound/Volume/Speed are stored PER phase. The Reveal toggle
    // (mode: intro | new_item) selects which phase the controls edit + preview;
    // the build applies intro to the first segment, new_item to each later one.
    anim: {
      intro:    { motion: "pop", sound: "pop", volume: 1.0, speed: 1.0 },
      new_item: { motion: "pop", sound: "pop", volume: 1.0, speed: 1.0 },
    },
    resize: true, overClip: false, phone: false, bake: true,
    title: "Top 5 s0m Moments",
    titleLines: 2,                         // 1 = force single line (auto-shrink); 2 = wrap up to 2 (backend title.max_lines)
    // per-style accent colour (title accent-words + list numbers), the pill
    // background colour (pill style only), and which title words are accented
    // (lowercased; matched by text, all styles). Backend keys: title.accent,
    // title.pill_fill, title.accent_words, list.accent.
    accent: { pill: "#2ea6ff", minimal: "#F5B942", editorial: "#FF5A5F" },
    pillFill: "#a9c9ec",
    medalsOn: false,                       // pill style only: #1/2/3 numbers → gold/silver/bronze
    medalText: false,                      // pill style only: #1/2/3 label text → gold/silver/bronze (independent)
    accentWords: [],
    off: null,
    widgets: [], clips: [],                // per rank: widget {src,kind,name} / clip-info {a,b,caption}
    clipStyle: { bg: "#0c0f15", accent: "#2ea6ff", text: "#ffffff", size: 1, textSize: 1, opacity: 0.78 },
    bonus: { on: false, text: "just wait for the second one", style: "sticker", pos: "above", size: 1, hideOn: true, hideSecs: 5, delayOn: false, delay: 1.0, speed: 1.0, sound: "none", volume: 1.0 },
    subtitle: { on: false, text: "VALORANT", after: "", size: 0.70, streamers: false },
    icon: { on: false, color: "#ffffff", opacity: 7, size: 468 },
  },
  vcePicker: null,                         // /assets/catalog.json (players/weapons/orgs), lazy
  editingSlot: null,                       // rank index the single-clip editor is editing (or null)
};

// ------------------------------------------------------------------ trim
// Read + normalize the start/end inputs against the clip duration.
function readTrim() {
  const dur = AL.duration || 0;
  let start = parseFloat($("#al-trim-start").value);
  let end = parseFloat($("#al-trim-end").value);
  if (!isFinite(start) || start < 0) start = 0;
  if (dur) start = Math.min(start, dur);
  if (!isFinite(end) || end <= 0) end = dur || null;
  if (dur && end) end = Math.min(end, dur);
  // keep at least a 0.1s window
  if (end !== null && end <= start) end = dur ? Math.min(start + 0.1, dur) : start + 0.1;
  return { start, end };
}
function updateTrimInfo() {
  const { start, end } = readTrim();
  // reflect the normalized values back into the inputs
  $("#al-trim-start").value = (Math.round(start * 10) / 10);
  if (end !== null) $("#al-trim-end").value = (Math.round(end * 10) / 10);
  const info = $("#al-trim-info");
  if (end !== null) {
    const len = Math.max(0, end - start);
    info.textContent = `window: ${len.toFixed(1)}s`;
  } else {
    info.textContent = "";
  }
  updateLengthInfo();
  drawTrimBar();
}

// speed multiplier from the slider
function readSpeed() {
  const s = parseFloat($("#al-speed").value);
  return isFinite(s) && s > 0 ? s : 1;
}
// total seconds removed by cuts that fall inside the current trim window
function cutSecondsInWindow() {
  const { start, end } = readTrim();
  const e = end === null ? (AL.duration || 0) : end;
  return AL.cuts.reduce((acc, c) => {
    const s = Math.max(c.start, start), en = Math.min(c.end, e);
    return acc + Math.max(0, en - s);
  }, 0);
}
// final output length = (window − removed − speed-up savings − slide overlaps) / speed
function updateLengthInfo() {
  const { start, end } = readTrim();
  const e = end === null ? (AL.duration || 0) : end;
  const window = Math.max(0, e - start);
  let saved = 0, slides = 0;                 // seconds shaved off; # of swipe joins
  AL.cuts.forEach(c => {
    const L = Math.max(0, Math.min(c.end, e) - Math.max(c.start, start));
    if (L <= 0.001) return;
    if (isSpeedupTr(c.transition)) {         // kept but compressed: L → L/factor
      const f = Math.max(1, c.transition.speed || 2);
      saved += L * (1 - 1 / f);
    } else {                                 // a normal cut removes its span
      saved += L;
      if (c.transition && c.transition.on && SWIPE_VISUALS.includes(c.transition.visual)) slides++;
    }
  });
  const final = Math.max(0, window - saved - slides * TRANS_DUR) / readSpeed();
  $("#al-length-info").textContent = `final length: ${final.toFixed(1)}s`;
}
// ---- Scrubber zoom -------------------------------------------------------
// The bar can collapse to a sub-window of the clip (AL.view) so cut placement
// isn't cramped when you only care about a slice of a long video. All position
// math maps source-seconds <-> a 0..1 fraction of the *visible* window.
function viewRange() {
  const dur = AL.duration || 0;
  if (AL.view && AL.view.end > AL.view.start)
    return { s: Math.max(0, AL.view.start), e: Math.min(dur || AL.view.end, AL.view.end) };
  return { s: 0, e: dur };
}
function tToPct(t) {
  const { s, e } = viewRange(); const span = (e - s) || 1;
  return Math.max(0, Math.min(100, ((t - s) / span) * 100));
}
function fracToT(frac) {
  const { s, e } = viewRange();
  return s + Math.max(0, Math.min(1, frac)) * (e - s);
}
// Collapse the bar to the trim window (+ a little padding) when it's a small
// slice of a longer clip; a full-length trim or "Full clip" restores the view.
function applyAutoZoom() {
  const dur = AL.duration || 0;
  const { start, end } = readTrim();
  const e = end === null ? dur : end;
  const win = e - start;
  if (AL.zoomFull || !dur || win <= 0 || win >= dur * 0.6 || dur <= 20) {
    AL.view = null;                                    // show the whole clip
  } else {
    const pad = Math.min(win * 0.12, 4, (dur - win) / 2);
    AL.view = { start: Math.max(0, start - pad), end: Math.min(dur, e + pad) };
  }
  updateZoomBtn();
}
function updateZoomBtn() {
  const btn = $("#al-trim-zoom");
  if (!btn) return;
  const dur = AL.duration || 0;
  const { start, end } = readTrim();
  const e = end === null ? dur : end;
  const win = e - start;
  const meaningful = dur > 20 && win > 0 && win < dur * 0.6;
  btn.classList.toggle("hidden", !meaningful && !AL.view);
  btn.textContent = AL.view ? "⤢ Show full clip" : "⤢ Zoom to selection";
}

// paint the selected range + handles on the scrubber (percentages of the view)
function drawTrimBar() {
  const dur = AL.duration || 0;
  const bar = $("#al-trim-bar");
  if (!dur) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const { start, end } = readTrim();
  const e = end === null ? dur : end;
  const sPct = tToPct(start), ePct = tToPct(e);
  $("#al-trim-range").style.left = sPct + "%";
  $("#al-trim-range").style.width = Math.max(0, ePct - sPct) + "%";
  $("#al-trim-h-start").style.left = sPct + "%";
  $("#al-trim-h-end").style.left = ePct + "%";
  bar.classList.toggle("zoomed", !!AL.view);
  drawCutBands();
  drawEffectMarkers();
  drawMomentBand();
  drawPlayhead();
}
// red bands over each cut span on the scrubber
function drawCutBands() {
  const dur = AL.duration || 0;
  const bar = $("#al-trim-bar");
  $$(".trim-cut", bar).forEach(el => el.remove());
  if (!dur) return;
  AL.cuts.forEach(c => {
    const l = tToPct(c.start);
    const w = Math.max(0, tToPct(c.end) - l);
    const el = document.createElement("div");
    el.className = "trim-cut" + (isSpeedupTr(c.transition) ? " speedup" : "");
    if (isSpeedupTr(c.transition)) el.title = `Speed up ${(c.transition.speed || 2).toFixed(2)}×`;
    el.style.left = l + "%";
    el.style.width = w + "%";
    bar.appendChild(el);
  });
}
function drawPlayhead() {
  const dur = AL.duration || 0;
  if (dur) {
    const t = $("#al-video").currentTime || 0;
    $("#al-trim-playhead").style.left = tToPct(t) + "%";
  }
  drawPending();
}
// live band from an armed quick-cut start to the current playhead
function drawPending() {
  const el = $("#al-trim-pending");
  if (!el) return;
  const dur = AL.duration || 0;
  if (pendingCutStart === null || !dur) { el.classList.add("hidden"); return; }
  const t = $("#al-video").currentTime || 0;
  const a = Math.min(pendingCutStart, t), b = Math.max(pendingCutStart, t);
  const l = tToPct(a);
  el.style.left = l + "%";
  el.style.width = Math.max(0, tToPct(b) - l) + "%";
  el.classList.remove("hidden");
}

// drag the in/out handles + click-to-seek on the scrubber
let selPlaying = false;
function initTrimBar() {
  const bar = $("#al-trim-bar");
  const v = $("#al-video");
  let edge = null;
  const fracAt = (clientX) => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  };
  const onMove = (e) => {
    if (!edge) return;
    const dur = AL.duration || 0;
    const t = fracToT(fracAt(e.clientX));
    const { start, end } = readTrim();
    const e2 = end === null ? dur : end;
    if (edge === "start") $("#al-trim-start").value = Math.min(t, Math.max(0, e2 - 0.1)).toFixed(1);
    else                  $("#al-trim-end").value = Math.max(t, start + 0.1).toFixed(1);
    v.currentTime = t;
    selPlaying = false;
    updateTrimInfo();
  };
  const onUp = () => {
    if (!edge) return;
    edge = null;
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    applyAutoZoom(); drawTrimBar();
    refreshPreview();
  };
  $$(".trim-handle", bar).forEach(h => h.addEventListener("mousedown", (e) => {
    e.preventDefault(); e.stopPropagation();
    edge = h.dataset.edge;
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }));
  bar.addEventListener("click", (e) => {
    if (e.target.classList.contains("trim-handle")) return;
    selPlaying = false;
    v.currentTime = fracToT(fracAt(e.clientX));
  });

  // keep the playhead in sync + loop the selection when "Play selection" is active
  v.addEventListener("timeupdate", () => {
    drawPlayhead();
    if (selPlaying) {
      const { start, end } = readTrim();
      const e = end === null ? (AL.duration || 0) : end;
      if (v.currentTime >= e - 0.03) v.currentTime = start;
    }
  });
  v.addEventListener("pause", () => { selPlaying = false; });
}
function playSelection() {
  const v = $("#al-video");
  const { start } = readTrim();
  v.currentTime = start;
  selPlaying = true;
  v.play().catch(() => {});
}

// ------------------------------------------------------------------ cuts
// Each cut is a {start,end} span (source seconds) removed from the render, plus
// an optional transition {on,sound,visual} played where the footage rejoins.
const TRANS_DUR = 0.5; // seconds a swipe transition overlaps (matches backend TRANSITION_DUR)
const SWIPE_VISUALS = ["swipeleft", "swiperight", "slideleft", "slideright"];
// a new cut inherits the FX studio's current selection
function newTransition() {
  const f = AL.fx;
  return { on: f.autoApply, sound: f.sound, visual: f.visual, volume: f.volume, speed: f.speed ?? 2.0 };
}
// "speed up" is a special Effect: the cut's span is kept and played faster
// (per-region ramp) instead of being removed. sound/vol don't apply to it.
function isSpeedupTr(tr) { return !!(tr && tr.on && tr.visual === "speedup"); }

// add a cut spanning [start,end] (source seconds); clamps + orders the bounds
function pushCut(start, end) {
  const dur = AL.duration || 0;
  let s = Math.max(0, Math.min(start, dur || start));
  let e = Math.max(0, Math.min(end, dur || end));
  if (e < s) { const t = s; s = e; e = t; }
  if (e - s < 0.1) e = Math.min(s + 0.1, dur || s + 0.1);
  AL.cuts.push({ start: +s.toFixed(1), end: +e.toFixed(1), transition: newTransition() });
  renderCuts();
}
function addCut() {
  const t = $("#al-video").currentTime || 0;
  pushCut(t, t + 2); // default 2s span at the playhead
}
function removeCut(i) {
  AL.cuts.splice(i, 1);
  renderCuts();
}

// --- "quick cut": arm the start at the playhead, then set the end at the playhead
let pendingCutStart = null;
function markCutStart() {
  pendingCutStart = Math.max(0, Math.min($("#al-video").currentTime || 0, AL.duration || 0));
  updateCutQuickUI();
  drawPlayhead();
}
function markCutEnd() {
  if (pendingCutStart === null) return;
  pushCut(pendingCutStart, $("#al-video").currentTime || 0);
  pendingCutStart = null;
  updateCutQuickUI();
  drawPlayhead();
}
function updateCutQuickUI() {
  const armed = pendingCutStart !== null;
  const startBtn = $("#al-cut-start");
  if (startBtn) startBtn.classList.toggle("armed", armed);
  const endBtn = $("#al-cut-end");
  if (endBtn) endBtn.disabled = !armed;
  const info = $("#al-cut-quick-info");
  if (info) info.textContent = armed
    ? `cut start @ ${pendingCutStart.toFixed(1)}s — scrub to the end, then “Cut end”`
    : "";
}

// build <option>s for the per-cut sound select, grouped by category
function soundOptionsHtml(selected) {
  const sounds = AL.fxData.sounds;
  let html = `<option value="none"${selected === "none" ? " selected" : ""}>None</option>`;
  if (!sounds.length) return html + `<option value="whoosh"${selected === "whoosh" ? " selected" : ""}>Whoosh</option>`;
  const cats = [...new Set(sounds.map(s => s.category))];
  cats.forEach(cat => {
    html += `<optgroup label="${cat}">`;
    sounds.filter(s => s.category === cat).forEach(s => {
      html += `<option value="${s.key}"${s.key === selected ? " selected" : ""}>${s.label}</option>`;
    });
    html += `</optgroup>`;
  });
  return html;
}
function visualOptionsHtml(selected) {
  const vis = AL.fxData.visuals.length ? AL.fxData.visuals
    : [{ key: "none", label: "None" }, { key: "swipeleft", label: "Swipe Left" }, { key: "swiperight", label: "Swipe Right" }];
  // normalize legacy names to the current keys
  const sel = ({ slideleft: "swipeleft", slideright: "swiperight" })[selected] || selected;
  return vis.map(v => `<option value="${v.key}"${v.key === sel ? " selected" : ""}>${v.label}</option>`).join("");
}
function previewSound(key) {
  if (!key || key === "none") return;
  const s = AL.fxData.sounds.find(x => x.key === key);
  if (!s || !s.preview) return;
  const a = new Audio(s.preview);
  a.volume = Math.min(1, (AL.fx.volume || 1) / 2 + 0.3);
  a.play().catch(() => {});
}

function renderCuts() {
  const list = $("#al-cut-list");
  AL.cuts.sort((a, b) => a.start - b.start);
  if (!AL.cuts.length) { list.innerHTML = `<span class="hint">No cuts — the full window renders.</span>`; }
  else {
    list.innerHTML = "";
    AL.cuts.forEach((c, i) => {
      if (!c.transition) c.transition = newTransition();
      const tr = c.transition;
      const item = document.createElement("div");
      item.className = "cut-item";
      item.innerHTML = `
        <div class="cut-row">
          <span class="cut-ix">${i + 1}</span>
          <label>from<input class="cut-s" type="number" min="0" step="0.1" value="${c.start}" /></label>
          <label>to<input class="cut-e" type="number" min="0" step="0.1" value="${c.end}" /></label>
          <label class="cut-fx-tg" title="Add a transition where the footage rejoins">
            <input class="cut-fx-on" type="checkbox" ${tr.on ? "checked" : ""} /> transition
          </label>
          <button class="ghost cut-seek" type="button" title="Jump to cut start">▶</button>
          <button class="ghost cut-del" type="button" title="Remove cut">✕</button>
        </div>
        <div class="cut-fx ${tr.on ? "" : "hidden"}">
          <label>Effect
            <select class="cut-fx-visual">${visualOptionsHtml(tr.visual)}</select>
          </label>
          <span class="cut-fx-join">
            <label>Sound
              <select class="cut-fx-sound">${soundOptionsHtml(tr.sound)}</select>
            </label>
            <button class="ghost cut-fx-play" type="button" title="Audition this sound">▶</button>
            <label>Vol
              <input class="cut-fx-vol" type="range" min="0" max="200" value="${Math.round((tr.volume ?? 1) * 100)}" />
            </label>
          </span>
          <label class="cut-fx-speed">Speed
            <input class="cut-fx-speedval" type="range" min="1.25" max="8" step="0.25" value="${tr.speed ?? 2}" />
            <output class="cut-fx-speed-out">${(tr.speed ?? 2).toFixed(2)}×</output>
          </label>
          <span class="hint cut-fx-hint"></span>
        </div>`;

      const clamp = () => {
        const dur = AL.duration || 0;
        let s = parseFloat(item.querySelector(".cut-s").value);
        let e = parseFloat(item.querySelector(".cut-e").value);
        if (!isFinite(s) || s < 0) s = 0;
        if (dur) s = Math.min(s, dur);
        if (!isFinite(e) || e <= s) e = Math.min(s + 0.1, dur || s + 0.1);
        if (dur) e = Math.min(e, dur);
        c.start = +s.toFixed(1); c.end = +e.toFixed(1);
        item.querySelector(".cut-s").value = c.start;
        item.querySelector(".cut-e").value = c.end;
        updateLengthInfo(); drawCutBands();
      };
      item.querySelector(".cut-s").addEventListener("change", clamp);
      item.querySelector(".cut-e").addEventListener("change", clamp);
      item.querySelector(".cut-seek").onclick = () => { $("#al-video").currentTime = c.start; };
      item.querySelector(".cut-del").onclick = () => removeCut(i);
      item.querySelector(".cut-fx-on").addEventListener("change", (e) => {
        tr.on = e.target.checked;
        item.querySelector(".cut-fx").classList.toggle("hidden", !tr.on);
        updateLengthInfo();
      });
      item.querySelector(".cut-fx-sound").addEventListener("change", (e) => { tr.sound = e.target.value; });
      item.querySelector(".cut-fx-play").onclick = () => previewSound(item.querySelector(".cut-fx-sound").value);
      item.querySelector(".cut-fx-visual").addEventListener("change", (e) => {
        tr.visual = e.target.value; syncCutFxMode(item, tr); updateLengthInfo(); drawCutBands();
      });
      item.querySelector(".cut-fx-vol").addEventListener("input", (e) => { tr.volume = (+e.target.value) / 100; });
      item.querySelector(".cut-fx-speedval").addEventListener("input", (e) => {
        tr.speed = +e.target.value;
        item.querySelector(".cut-fx-speed-out").textContent = tr.speed.toFixed(2) + "×";
        updateLengthInfo();
      });
      syncCutFxMode(item, tr);
      list.appendChild(item);
    });
  }
  updateLengthInfo();
  drawCutBands();
}

// Toggle a cut's transition panel between join mode (sound/vol) and speed-up
// mode (speed slider) based on the chosen Effect.
function syncCutFxMode(item, tr) {
  const speedup = tr.visual === "speedup";
  const join = item.querySelector(".cut-fx-join");
  const speed = item.querySelector(".cut-fx-speed");
  const hint = item.querySelector(".cut-fx-hint");
  if (join) join.classList.toggle("hidden", speedup);
  if (speed) speed.classList.toggle("hidden", !speedup);
  if (hint) hint.textContent = speedup
    ? "Keeps this span but plays it faster (kept, not removed)."
    : "Plays where the clip rejoins after the cut.";
}

function selectClip(c, onReady) {
  if (!onReady && AL.editingSlot !== null) { AL.editingSlot = null; updateSlotEditorUI(); }
  selectedClip = c.path || c.name;              // identity is folder/name
  AL.effects = [];                              // B-roll markers are per-clip
  AL.moment = { on: false, start: 0, end: 0 };  // moment window is per-clip
  if (typeof momentControlsSync === "function") momentControlsSync();
  renderBrollList();
  if (AL.editingSlot === null) loadLibrary();   // don't disturb formatter state while editing a slot
  $("#al-empty").classList.add("hidden");
  $("#al-tool").classList.remove("hidden");
  $("#al-name").textContent = c.name || (c.path || "").split("/").pop();
  $("#al-output").classList.add("hidden");
  $("#al-preview").src = "";
  $("#al-status").textContent = "";

  const v = $("#al-video");
  v.src = c.url || `/storage/clips/${c.path || c.name}`;
  v.onloadedmetadata = () => {
    v.currentTime = Math.min(0.5, (v.duration || 1) * 0.1); // show a frame
    AL.natW = v.videoWidth; AL.natH = v.videoHeight;
    AL.duration = v.duration || 0;
    // trim defaults to the full clip
    $("#al-trim-start").value = 0;
    $("#al-trim-start").max = AL.duration ? AL.duration.toFixed(1) : "";
    $("#al-trim-end").value = AL.duration ? AL.duration.toFixed(1) : "";
    $("#al-trim-end").max = AL.duration ? AL.duration.toFixed(1) : "";
    AL.cuts = [];
    pendingCutStart = null;
    AL.view = null; AL.zoomFull = false;       // reset scrubber zoom for the new clip
    updateCutQuickUI();
    renderCuts();
    updateTrimInfo();
    applyAutoZoom(); drawTrimBar();
    // facecam default: top-left, 28% width
    const fw = Math.round(AL.natW * 0.28);
    setRect("facecam", { x: 0, y: 0, w: fw, h: Math.round(fw * 9 / 16) });
    // mousecam default: bottom-right, 18% width
    const mw = Math.round(AL.natW * 0.18);
    const mh = Math.round(mw * 3 / 4);
    setRect("mousecam", { x: AL.natW - mw, y: AL.natH - mh, w: mw, h: mh });
    AL.mcPos = mcCornerToPos($("#al-mc-pos").value || "bottom-right");   // seed output placement
    syncBoxVisibility();
    if (typeof onReady === "function") onReady();
  };
}

// map source px <-> displayed px
function scale() {
  const v = $("#al-video");
  return v.clientWidth / (AL.natW || v.videoWidth || 1);
}
function setRect(name, r) {
  const rect = {
    x: Math.round(r.x), y: Math.round(r.y),
    w: Math.round(r.w), h: Math.round(r.h),
  };
  AL.rect[name] = rect;
  const p = BOXES[name].coord;
  $("#" + p + "x").value = rect.x; $("#" + p + "y").value = rect.y;
  $("#" + p + "w").value = rect.w; $("#" + p + "h").value = rect.h;
  drawBox(name);
  if (name === "mousecam") { clampMcPos(); drawMcGhost(); }   // aspect changed → resize ghost
}
function readCoords(name) {
  const p = BOXES[name].coord;
  setRect(name, {
    x: +$("#" + p + "x").value, y: +$("#" + p + "y").value,
    w: +$("#" + p + "w").value, h: +$("#" + p + "h").value,
  });
}
function drawBox(name) {
  const r = AL.rect[name];
  if (!r) return;
  const s = scale();
  const b = $("#" + BOXES[name].elId);
  b.style.left = r.x * s + "px";
  b.style.top = r.y * s + "px";
  b.style.width = r.w * s + "px";
  b.style.height = r.h * s + "px";
}
function drawAllBoxes() { Object.keys(BOXES).forEach(drawBox); drawMcGhost(); }

// drag + resize for one named box
function initBoxDrag(name) {
  const box = $("#" + BOXES[name].elId);
  let mode = null, start = null;

  const onDown = (e, m) => {
    e.preventDefault(); e.stopPropagation();
    mode = m;
    start = { mx: e.clientX, my: e.clientY, ...AL.rect[name] };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };
  const onMove = (e) => {
    if (!mode) return;
    const s = scale();
    const dx = (e.clientX - start.mx) / s;
    const dy = (e.clientY - start.my) / s;
    let { x, y, w, h } = start;
    if (mode === "move") { x += dx; y += dy; }
    else { w += dx; h += dy; }
    w = Math.max(40, w); h = Math.max(40, h);
    x = Math.max(0, Math.min(x, AL.natW - w));
    y = Math.max(0, Math.min(y, AL.natH - h));
    w = Math.min(w, AL.natW - x); h = Math.min(h, AL.natH - y);
    setRect(name, { x, y, w, h });
  };
  const onUp = () => {
    mode = null;
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    refreshPreview(); // auto-update preview when a box is released
  };

  box.addEventListener("mousedown", (e) => onDown(e, "move"));
  $(".handle", box).addEventListener("mousedown", (e) => onDown(e, "resize"));
}

// ---- Mousecam OUTPUT placement: a draggable ghost on the 9:16 preview ----
// AL.mcPos = {x,y} is the overlay's top-left as a fraction of the output (1080x1920);
// the preview frame is exactly 9:16 so % maps 1:1 to those fractions.
function mcGhostFrac() {
  const scale = (+$("#al-mc-scale").value) / 100;      // overlay width = fraction of output W
  const mc = AL.rect.mousecam;
  const aspect = (mc && mc.w) ? (mc.h / mc.w) : 0.6;   // source crop aspect (fallback)
  return { wFrac: scale, hFrac: scale * aspect * (1080 / 1920) };
}
function mcCornerToPos(corner) {
  const { wFrac, hFrac } = mcGhostFrac();
  const mx = 0.03, my = 0.03 * (1080 / 1920);          // mirror backend margin (W*0.03)
  const right = 1 - wFrac - mx, bottom = 1 - hFrac - my;
  return ({
    "top-left": { x: mx, y: my },
    "top-right": { x: right, y: my },
    "bottom-left": { x: mx, y: bottom },
    "bottom-right": { x: right, y: bottom },
  })[corner] || { x: right, y: bottom };
}
function ensureMcPos() { if (!AL.mcPos) AL.mcPos = mcCornerToPos($("#al-mc-pos").value || "bottom-right"); }
function clampMcPos() {
  if (!AL.mcPos) return;
  const { wFrac, hFrac } = mcGhostFrac();
  AL.mcPos.x = Math.max(0, Math.min(AL.mcPos.x, 1 - wFrac));
  AL.mcPos.y = Math.max(0, Math.min(AL.mcPos.y, 1 - hFrac));
}
function drawMcGhost() {
  const g = $("#al-mc-ghost");
  const on = $("#al-mc-on").checked && AL.mcPos;
  g.classList.toggle("hidden", !on);
  if (!on) return;
  const { wFrac, hFrac } = mcGhostFrac();
  g.style.left = (AL.mcPos.x * 100) + "%";
  g.style.top = (AL.mcPos.y * 100) + "%";
  g.style.width = (wFrac * 100) + "%";
  g.style.height = (hFrac * 100) + "%";
}
function initMcGhostDrag() {
  const g = $("#al-mc-ghost");
  let start = null;
  const down = e => {
    e.preventDefault(); e.stopPropagation();
    ensureMcPos();
    const f = g.parentElement.getBoundingClientRect();
    start = { mx: e.clientX, my: e.clientY, x: AL.mcPos.x, y: AL.mcPos.y, fw: f.width, fh: f.height };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const move = e => {
    if (!start) return;
    AL.mcPos.x = start.x + (e.clientX - start.mx) / start.fw;
    AL.mcPos.y = start.y + (e.clientY - start.my) / start.fh;
    clampMcPos(); drawMcGhost();
  };
  const up = () => {
    if (!start) return;
    start = null;
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    refreshPreview();
  };
  g.addEventListener("mousedown", down);
}

// show/hide boxes + control groups based on toggles
function syncBoxVisibility() {
  const fcOn = $("#al-fc-on").checked;
  const mcOn = $("#al-mc-on").checked;
  $("#al-box").classList.toggle("hidden", !fcOn);
  $("#al-box-mc").classList.toggle("hidden", !mcOn);
  $("#al-detect").classList.toggle("hidden", !fcOn);
  $(".coords:not(.mc-coords)").classList.toggle("hidden", !fcOn);
  $("#ctrl-bar").classList.toggle("hidden", !$("#al-edges-on").checked);
  $("#ctrl-fc-h").classList.toggle("hidden", !(fcOn && $("#al-fc-lock").checked));
  $("#mc-controls").classList.toggle("hidden", !mcOn);
  $("#mc-coords").classList.toggle("hidden", !mcOn);
  if (mcOn) ensureMcPos();
  drawAllBoxes();
}

// The facecam pane height (% of the content block) the AUTO fit would produce for
// the current crop — mirrors autolayout._build_filter's autofit branch. Used to
// seed the "Lock facecam height" slider so locking doesn't jump the look and you
// can read one clip's value off to copy onto the others.
function autoFcHeightPct() {
  const fc = AL.rect.facecam;
  if (!fc || !fc.w || !fc.h) return 35;
  const W = 1080, H = 1920;
  const bar = $("#al-edges-on").checked ? (+$("#al-bar").value) / 100 : 0;
  const contentH = H * (1 - 2 * bar) || 2;
  let topH = W * fc.h / fc.w;                                   // aspect-derived
  topH = Math.max(contentH * 0.15, Math.min(topH, contentH * 0.62));
  return Math.round(topH / contentH * 100);
}

// assemble the full layout options payload
function buildOpts() {
  const { start, end } = readTrim();
  return {
    clip: selectedClip,
    trim_start: start,
    trim_end: end,
    facecam_enabled: $("#al-fc-on").checked,
    rect: AL.rect.facecam,
    facecam_autofit: true,           // top pane sizes to the facecam's aspect (no bars)…
    facecam_height: $("#al-fc-lock").checked ? (+$("#al-fc-h").value) / 100 : null,  // …unless locked
    gameplay_fill: $("#al-edges-on").checked ? "blur" : "crop",
    blur_bar: (+$("#al-bar").value) / 100,
    mousecam_enabled: $("#al-mc-on").checked,
    mousecam: AL.rect.mousecam,
    mousecam_pos: $("#al-mc-pos").value,
    mousecam_x: AL.mcPos ? AL.mcPos.x : null,   // dragged output position (top-left fractions)
    mousecam_y: AL.mcPos ? AL.mcPos.y : null,
    mousecam_scale: (+$("#al-mc-scale").value) / 100,
    speed: readSpeed(),
    cuts: AL.cuts.map(c => ({
      start: c.start,
      end: c.end,
      transition: (c.transition && c.transition.on)
        ? { sound: c.transition.sound, visual: c.transition.visual,
            volume: c.transition.volume ?? 1, speed: c.transition.speed ?? 2 }
        : null,
    })),
    audio_fade_out: $("#al-fade-on").checked,
    audio_fade_dur: +$("#al-fade-dur").value,
    effects: (AL.effects || []).map(e => ({ type: e.type, t: e.t, dur: e.dur, sound: e.sound, volume: e.volume, scale: e.scale, x: e.x, y: e.y, style: e.style, vd_accent: e.vd_accent, params: e.params })),
  };
}

async function detectFacecam() {
  const btn = $("#al-detect");
  btn.disabled = true; btn.textContent = "Detecting…";
  showPreviewSpin(true);
  try {
    const res = await api("/api/autolayout/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip: selectedClip }),
    });
    AL.natW = res.video.width; AL.natH = res.video.height;
    setRect("facecam", res.rect);
    const st = $("#al-status");
    if (res.detected) { st.textContent = `Detected (${res.corner})`; st.className = "det-status ok"; }
    else { st.textContent = "No face found — position the box manually"; st.className = "det-status warn"; }
    await refreshPreview();
  } catch (e) { toast("Detect failed: " + e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Auto-detect facecam"; showPreviewSpin(false); }
}

let previewTimer;
async function refreshPreview() {
  if (!selectedClip) return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    showPreviewSpin(true);
    try {
      const res = await api("/api/autolayout/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildOpts()),
      });
      $("#al-preview").src = res.preview + "?t=" + Date.now();
    } catch (e) { toast("Preview failed: " + e.message, true); }
    finally { showPreviewSpin(false); }
  }, 150);
}
function showPreviewSpin(on) { $("#al-preview-spin").classList.toggle("hidden", !on); }

async function render() {
  const btn = $("#al-render");
  btn.disabled = true;
  const status = $("#al-render-status");
  status.textContent = "Queued…";
  renderProgress($("#al-progress"), null, "Queued…");
  try {
    const { job } = await api("/api/autolayout/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildOpts()),
    });
    pollJob(job, status, btn);
  } catch (e) { status.textContent = ""; endRenderProgress($("#al-progress"), false); toast("Render failed: " + e.message, true); btn.disabled = false; }
}

// ---- Animated render/build progress bar ---------------------------------------
// pct is 0..100 for a determinate fill, or null/undefined for an indeterminate
// shimmer (used while a job reports no numeric progress yet).
function renderProgress(el, pct, label) {
  if (!el) return;
  el.classList.remove("hidden");
  const fill = el.querySelector(".rp-fill");
  const lab = el.querySelector(".rp-label");
  if (typeof pct === "number" && pct >= 0) {
    el.classList.remove("indeterminate");
    if (fill) fill.style.width = Math.max(3, Math.min(100, pct)) + "%";
    el.setAttribute("aria-valuenow", Math.round(pct));
  } else {
    el.classList.add("indeterminate");
    if (fill) fill.style.width = "";
    el.removeAttribute("aria-valuenow");
  }
  if (lab) lab.textContent = label || "";
}
function endRenderProgress(el, ok, label) {
  if (!el) return;
  if (ok) {
    el.classList.remove("indeterminate");
    const fill = el.querySelector(".rp-fill");
    if (fill) fill.style.width = "100%";
    const lab = el.querySelector(".rp-label");
    if (lab) lab.textContent = label || "Done ✓";
    setTimeout(() => el.classList.add("hidden"), 1400);
  } else {
    el.classList.add("hidden");
  }
}

async function pollJob(job, status, btn) {
  const prog = $("#al-progress");
  try {
    const j = await api("/api/jobs/" + job);
    if (j.status === "running" || j.status === "queued") {
      status.textContent = "Rendering…";
      renderProgress(prog, (typeof j.progress === "number" ? j.progress : null),
                     j.stage || "Rendering…");
      setTimeout(() => pollJob(job, status, btn), 1200);
    } else if (j.status === "done") {
      status.textContent = "Done ✓";
      btn.disabled = false;
      endRenderProgress(prog, true);
      $("#al-output").classList.remove("hidden");
      $("#al-out-video").src = j.output;
      $("#al-download").href = j.output;
    } else {
      status.textContent = ""; btn.disabled = false;
      endRenderProgress(prog, false);
      toast("Render error: " + (j.error || "unknown"), true);
    }
  } catch (e) { status.textContent = ""; btn.disabled = false; endRenderProgress(prog, false); toast(e.message, true); }
}

// ============================================================ Experimental search
// AI-parsed, multi-source, vision-verified clip search (backend/search2.py).
// One natural-language box -> a background job that parses intent, fans out to
// YouTube + Twitch (+ Reddit), then watches the top clips to verify clippability.
// The experimental engine is CONTEXT-DRIVEN so the exact same code powers both the
// Search tab's "🧪 Experimental" sub-tab (prefix "x") and the Formatter rank finder's
// "🧪 Deep" pane (prefix "rx"). An instance = a set of DOM elements (all `#{p}-…`) + a
// destination `onGet(r, btn, start, end)` callback (library download vs. rank assign).
// Adding an experimental feature here lights it up in BOTH surfaces automatically —
// keep it that way (see CLAUDE.md "Search feature parity").
let xMeta = null, xMetaLoaded = false;
const XCTX = {};   // prefix -> ctx

const xEl = (ctx, s) => document.getElementById(ctx.p + "-" + s);

// Destination for the Search-tab instance: grab into the Library.
const xGetToLibrary = (r, btn, start, end) => downloadClip(r.url, btn, start, end);

async function xLoadMeta() {
  if (xMetaLoaded) return;
  xMetaLoaded = true;
  try { xMeta = await api("/api/search2/meta"); }
  catch { xMeta = { ai_available: false, twitch_available: false, reddit_available: false,
                    agents: [], maps: [], weapons: [], plays: [] }; }
}

// Wire one experimental-search instance. Idempotent per prefix (cached in XCTX).
async function xInit(p, onGet) {
  if (XCTX[p]) return XCTX[p];
  const ctx = XCTX[p] = { p, onGet, results: [],
    filter: { agents: [], maps: [], weapons: [], plays: [], creator: "" } };
  await xLoadMeta();
  xRenderCaps(ctx);
  xRenderChips(ctx);
  // Reddit checkbox only usable when creds are configured
  const rc = xEl(ctx, "src-reddit");
  if (rc && !xMeta.reddit_available) { rc.checked = false; rc.disabled = true;
    rc.closest("label").title = "Add secrets/reddit.json creds to enable Reddit"; }
  const run = xEl(ctx, "run");
  if (run) run.onclick = () => xRun(ctx);
  const q = xEl(ctx, "query");
  if (q) q.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) xRun(ctx);
  });
  return ctx;
}

function xRenderCaps(ctx) {
  const box = xEl(ctx, "caps");
  if (!box) return;
  const cap = (ok, on, off) => `<span class="x-cap ${ok ? "on" : "off"}">${ok ? "✓" : "✕"} ${ok ? on : off}</span>`;
  box.innerHTML =
    cap(xMeta.ai_available, "AI parse + vision", "AI offline (rule-based)") +
    cap(xMeta.twitch_available, "Twitch clips", "Twitch off") +
    cap(xMeta.reddit_available, "Reddit", "Reddit off (no creds)");
}

// clickable vocab chips that toggle into this instance's explicit filter
function xRenderChips(ctx) {
  const box = xEl(ctx, "chips");
  if (!box) return;
  const groups = [["plays", xMeta.plays], ["agents", xMeta.agents],
                  ["maps", xMeta.maps], ["weapons", xMeta.weapons]];
  box.innerHTML = groups.map(([key, vals]) => (vals && vals.length) ? `
    <div class="x-chip-row"><span class="mini-lbl">${key}</span>
      ${vals.map(v => `<button type="button" class="x-vchip" data-k="${key}" data-v="${esc(v)}">${esc(v)}</button>`).join("")}
    </div>` : "").join("");
  box.querySelectorAll(".x-vchip").forEach(b => b.onclick = () => {
    const k = b.dataset.k, v = b.dataset.v, arr = ctx.filter[k];
    const i = arr.indexOf(v);
    if (i >= 0) arr.splice(i, 1); else arr.push(v);
    b.classList.toggle("on", i < 0);
  });
}

function xBuildPayload(ctx) {
  const val = (s, d) => { const e = xEl(ctx, s); return e ? e.value : d; };
  const chk = (s) => { const e = xEl(ctx, s); return !!(e && e.checked && !e.disabled); };
  const srcs = [];
  if (chk("src-youtube")) srcs.push("youtube");
  if (chk("src-twitch")) srcs.push("twitch");
  if (chk("src-reddit")) srcs.push("reddit");
  if (!srcs.length && !xEl(ctx, "src-youtube")) srcs.push("youtube", "twitch");  // compact panel omits toggles
  const facecam = xEl(ctx, "facecam");
  const vision = xEl(ctx, "vision");
  const filters = {
    agents: ctx.filter.agents, maps: ctx.filter.maps,
    weapons: ctx.filter.weapons, plays: ctx.filter.plays,
    creator: (val("creator", "") || "").trim(),
    recency_days: +val("recency", 0) || 0,
    duration_max: +val("durmax", 0) || 0,
    want_facecam: (facecam && facecam.checked) || undefined,
  };
  const opts = {
    sources: srcs,
    use_vision: vision ? vision.checked : true,
    vision_count: +val("vcount", 8) || 8,
    limit: 20,
  };
  return { query: (val("query", "") || "").trim(), filters, opts };
}

async function xRun(ctx) {
  const payload = xBuildPayload(ctx);
  if (!payload.query && !payload.filters.creator &&
      !["agents", "maps", "weapons", "plays"].some(k => payload.filters[k].length))
    return toast("Describe what you're looking for", true);
  const btn = xEl(ctx, "run");
  btn.disabled = true; btn.textContent = "Searching…";
  const res = xEl(ctx, "results"); if (res) res.innerHTML = "";
  const intent = xEl(ctx, "intent"); if (intent) intent.classList.add("hidden");
  xProgress(ctx, 0, "starting…");
  const prog = xEl(ctx, "progress"); if (prog) prog.classList.remove("hidden");
  try {
    const { job_id } = await api("/api/search2", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    xPoll(ctx, job_id, btn);
  } catch (e) {
    if (prog) prog.classList.add("hidden");
    btn.disabled = false; btn.textContent = "Deep Search";
    toast("Search failed: " + e.message, true);
  }
}

function xProgress(ctx, pct, msg) {
  const p = xEl(ctx, "progress");
  if (!p) return;
  p.querySelector("i").style.width = `${Math.max(3, pct)}%`;
  p.querySelector(".x-stage").textContent = msg || "";
}

async function xPoll(ctx, job, btn) {
  const prog = xEl(ctx, "progress");
  try {
    const j = await api("/api/jobs/" + job);
    if (j.status === "running" || j.status === "queued") {
      xProgress(ctx, j.progress || 0, j.stage || "working…");
      setTimeout(() => xPoll(ctx, job, btn), 900);
      return;
    }
    btn.disabled = false; btn.textContent = "Deep Search";
    if (prog) prog.classList.add("hidden");
    if (j.status === "done") {
      ctx.results = j.results || [];
      xShowIntent(ctx, j.intent, j.meta);
      xRender(ctx);
    } else {
      toast("Search error: " + (j.error || "unknown"), true);
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = "Deep Search";
    if (prog) prog.classList.add("hidden");
    toast(e.message, true);
  }
}

function xShowIntent(ctx, intent, meta) {
  const box = xEl(ctx, "intent");
  if (!box || !intent) return;
  const parts = [];
  if (intent.creator) parts.push(`<b>${esc(intent.creator)}</b>`);
  for (const k of ["plays", "agents", "maps", "weapons", "orgs"])
    (intent[k] || []).forEach(v => parts.push(esc(v)));
  if (intent.recency_days) parts.push(`last ${intent.recency_days}d`);
  if (intent.want_facecam) parts.push("facecam");
  const src = (meta && meta.sources || []).join(" + ");
  const vv = meta && meta.vision_verified ? ` · watched ${meta.vision_verified} clips` : "";
  const how = meta && meta.ai_parse ? "AI-parsed" : "rule-parsed";
  box.innerHTML =
    `<span class="x-intent-lbl">${how}:</span> ${parts.join(" · ") || "—"} ` +
    `<span class="x-intent-meta">(${meta ? meta.pool : 0} found across ${src}${vv})</span>`;
  box.classList.remove("hidden");
}

function xRender(ctx) {
  const box = xEl(ctx, "results");
  if (!box) return;
  if (!ctx.results.length) { box.innerHTML = `<div class="hint">No clips matched. Try broadening the query or enabling more sources.</div>`; return; }
  box.innerHTML = "";
  ctx.results.forEach(r => box.appendChild(xCard(ctx, r)));
}

function xCard(ctx, r) {
  const v = r.vision || {};
  const dur = fmtDur(r.duration), views = fmtViews(r.view_count);
  const src = r.source === "twitch" ? "Twitch" : r.source === "reddit" ? "Reddit" : "YT";
  const el = document.createElement("div");
  el.className = "result x-result";
  // verdict badges from the vision pass
  const badges = [];
  if (v.analyzed) {
    if (v.clippable) badges.push(`<span class="x-badge good">✓ clippable</span>`);
    else badges.push(`<span class="x-badge bad">✕ not clean</span>`);
    if (v.facecam) badges.push(`<span class="x-badge fc">🙂 facecam</span>`);
    if (v.edited) badges.push(`<span class="x-badge warn">⚠ edited</span>`);
    if (v.center_clutter) badges.push(`<span class="x-badge warn">⚠ overlay</span>`);
    const am = [v.agent, v.map].filter(Boolean).join(" · ");
    if (am) badges.push(`<span class="x-badge meta">${esc(am)}</span>`);
  } else if (r.tier) {
    badges.push(`<span class="x-badge tier">${r.tier}</span>`);
  }
  el.innerHTML = `
    <img src="${r.thumbnail || ""}" onerror="this.style.visibility='hidden'"/>
    <div class="meta">
      <div class="title">${esc(r.title || "(untitled)")}</div>
      <div class="sub">
        <span class="src src-${r.source}">${src}</span>
        ${dur ? `<span class="dur dur-${r.tier || "medium"}">${dur}</span>` : ""}
        ${r.channel ? `<span class="ch">${esc(r.channel)}</span>` : ""}
        ${views ? `<span class="vc">${views}${r.via === "reddit" ? " up" : " views"}</span>` : ""}
      </div>
      <div class="x-badges">${badges.join("")}</div>
      ${r.reason ? `<div class="x-reason">${esc(r.reason)}</div>` : ""}
    </div>
    <div class="res-actions">
      <button class="res-preview ghost" type="button" title="Preview this clip">▶</button>
      <button class="res-range-tg ghost" type="button" title="Download only a section">✂</button>
      <button class="res-get">Get</button>
    </div>
    <div class="res-range hidden">
      <span class="res-range-lbl">Section</span>
      <label>from <input class="res-from" type="text" placeholder="0:00" /></label>
      <label>to <input class="res-to" type="text" placeholder="${dur || "end"}" /></label>
      <span class="hint">blank = whole video</span>
    </div>`;
  const getBtn = el.querySelector(".res-get");
  const rangeRow = el.querySelector(".res-range");
  el.querySelector(".res-preview").onclick = ev => previewRemoteClip(r.url, r.title, ev.currentTarget);
  el.querySelector(".res-range-tg").onclick = () => rangeRow.classList.toggle("hidden");
  getBtn.onclick = () => {
    let start = null, end = null;
    if (!rangeRow.classList.contains("hidden")) {
      const from = parseTime(el.querySelector(".res-from").value);
      const to = parseTime(el.querySelector(".res-to").value);
      if (from != null || to != null) {
        start = from ?? 0; end = to ?? r.duration ?? null;
        if (end != null && end <= start) return toast("Section end must be after its start", true);
      }
    }
    ctx.onGet(r, getBtn, start, end);
  };
  return el;
}

// Preview a not-yet-downloaded remote clip (YouTube/Twitch/etc.) in the shared player.
// Resolves a progressive stream URL via the Nb1 resolver — same path as pkgPreviewClip.
async function previewRemoteClip(url, title, btn) {
  if (btn) btn.disabled = true;
  toast("Resolving preview…");
  try {
    const r = await api("/api/nb1/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, max_height: 720 }) });
    openPlayer(r.url, title || r.title);
  } catch (e) { toast("Can't preview this source — grab it first. (" + e.message + ")", true); }
  finally { if (btn) btn.disabled = false; }
}

// ------------------------------------------------------------------ FX studio
async function initFx() {
  try { AL.fxData = await api("/api/fx"); } catch { AL.fxData = { sounds: [], visuals: [], effects: [] }; }
  renderFxSoundGrid();
  renderFxSwipeGrid();
  renderBrollTray();

  // mini-tab switching (Sound / Swipe)
  $$(".fx-mtab").forEach(btn => btn.onclick = () => {
    $$(".fx-mtab").forEach(b => b.classList.toggle("active", b === btn));
    $$(".fx-pane").forEach(p => p.classList.toggle("hidden", p.dataset.fxpane !== btn.dataset.fxtab));
  });
  $("#fx-vol").addEventListener("input", e => {
    AL.fx.volume = (+e.target.value) / 100;
    $("#fx-vol-val").textContent = e.target.value + "%";
  });
  $("#fx-default-on").addEventListener("change", e => { AL.fx.autoApply = e.target.checked; });
  $("#fx-demo-play").onclick = () => playSwipeDemo(AL.fx.visual);
}
function renderFxSoundGrid() {
  const grid = $("#fx-sound-grid");
  const sounds = AL.fxData.sounds;
  grid.innerHTML = "";
  if (!sounds.length) { grid.innerHTML = `<span class="hint">No sounds found.</span>`; return; }
  [...new Set(sounds.map(s => s.category))].forEach(cat => {
    const lbl = document.createElement("div"); lbl.className = "fx-cat"; lbl.textContent = cat;
    grid.appendChild(lbl);
    sounds.filter(s => s.category === cat).forEach(s => {
      const chip = document.createElement("div");
      chip.className = "fx-chip" + (s.key === AL.fx.sound ? " active" : "");
      chip.innerHTML = `<span class="play">▶</span><span>${s.label}</span>`;
      chip.querySelector(".play").onclick = (e) => { e.stopPropagation(); previewSound(s.key); };
      chip.onclick = () => {
        AL.fx.sound = s.key;
        $$("#fx-sound-grid .fx-chip").forEach(c => c.classList.remove("active"));
        chip.classList.add("active");
      };
      grid.appendChild(chip);
    });
  });
}
function renderFxSwipeGrid() {
  const grid = $("#fx-swipe-grid");
  const vis = AL.fxData.visuals.length ? AL.fxData.visuals
    : [{ key: "none", label: "None" }, { key: "swipeleft", label: "Swipe Left" }, { key: "swiperight", label: "Swipe Right" }];
  grid.innerHTML = "";
  vis.forEach(v => {
    const chip = document.createElement("div");
    chip.className = "fx-chip" + (v.key === AL.fx.visual ? " active" : "");
    chip.innerHTML = `<span>${v.label}</span>`;
    chip.onclick = () => {
      AL.fx.visual = v.key;
      $$("#fx-swipe-grid .fx-chip").forEach(c => c.classList.remove("active"));
      chip.classList.add("active");
      playSwipeDemo(v.key);
    };
    grid.appendChild(chip);
  });
}
// animate the little A/B demo with an ease-out curve matching the backend push
function playSwipeDemo(direction) {
  const demo = $("#fx-demo"); if (!demo) return;
  const a = demo.querySelector(".a"), b = demo.querySelector(".b");
  const set = (el, x, anim) => {
    el.style.transition = anim ? "transform .55s cubic-bezier(.215,.61,.355,1)" : "none";
    el.style.transform = `translateX(${x}%)`;
  };
  if (direction === "none" || !SWIPE_VISUALS.includes(direction)) {
    set(a, 0, false); set(b, 100, false);
    requestAnimationFrame(() => { set(b, 0, false); setTimeout(() => set(b, 100, false), 350); });
    return;
  }
  const right = direction === "swiperight";
  set(a, 0, false); set(b, right ? -100 : 100, false);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    set(a, right ? 100 : -100, true); set(b, 0, true);
    setTimeout(() => { set(a, 0, false); set(b, right ? -100 : 100, false); }, 1100);
  }));
}

// ------------------------------------------------------------------ Video Formatter (separate tool)
let fmtMetaLoaded = false;
async function initFormatter() {
  try { AL.fmtMeta = await api("/api/formatter/meta"); } catch { AL.fmtMeta = { templates: [], styles: [], fonts: [], joins: [], sounds: [] }; }
  fmtMetaLoaded = true;
  // per-style drag offsets: server defaults (the design's dragged spots) unless
  // a saved project already restored them
  if (!AL.vce.off) {
    AL.vce.off = {};
    (AL.fmtMeta.styles || []).forEach(s => { AL.vce.off[s.key] = Object.assign({}, s.offsets); });
  }
  ["pill", "minimal", "editorial"].forEach(k => {
    if (!AL.vce.off[k]) AL.vce.off[k] = { tx: 0, ty: 0, lx: 0, ly: 0, ts: 1, ls: 1 };
  });
  // picker catalog for the widget / team searches (lazy but kicked off now)
  fetch(AL.fmtMeta.picker || "/assets/catalog.json")
    .then(r => r.json()).then(d => { AL.vcePicker = d; })
    .catch(() => { AL.vcePicker = { players: [], weapons: [], orgs: [] }; });

  if (!AL.fmt.ranks.length) {
    AL.fmt.ranks = Array.from({ length: 5 }, newRank);
    seedPlayPositions();
  }

  $("#fmt-order").addEventListener("change", e => {
    AL.fmt.order = e.target.value;
    if (AL.fmt.order !== "custom") seedPlayPositions();   // asc/desc derive the play numbers
    renderRankSlots();
  });
  $("#fmt-add-rank").onclick = () => {
    if (AL.fmt.ranks.length < 10) {
      AL.fmt.ranks.push(newRank()); seedPlayPositions();
      renderRankSlots(); vceRanksChanged();
    }
  };
  $("#fmt-build").onclick = buildRanking;
  $("#proj-save").onclick = saveProject;
  $("#proj-load").onclick = loadProject;
  $("#proj-delete").onclick = deleteProject;
  loadProjectList();
  initVceEditor();

  // per-rank clip finder modal
  $("#rs-close").onclick = closeRankSearch;
  $$(".rs-mode", $("#rank-search")).forEach(b => b.onclick = () => rsSetMode(b.dataset.mode));
  $("#rs-search").onclick = doRankSearch;
  $("#rs-query").addEventListener("keydown", e => { if (e.key === "Enter") doRankSearch(); });
  $("#rs-creator").addEventListener("keydown", e => { if (e.key === "Enter") doRankSearch(); });
  $("#rs-file").onchange = e => uploadToRank(e.target.files[0]);
  $("#rank-search").addEventListener("click", e => { if (e.target.id === "rank-search") closeRankSearch(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#rank-search").classList.contains("hidden")) closeRankSearch();
  });
  // If we're already sitting on the Formatter tab when meta finishes loading
  // (e.g. deep-linked / refreshed on #formatter), render it now — loadFormatter's
  // earlier call bailed out because fmtMetaLoaded was still false.
  const fp = $('[data-niche="formatter"]');
  if (fp && !fp.classList.contains("hidden")) loadFormatter();
}

// ============================================================================
// "Valorant Clip Ranking" editor + live 1080×1920 canvas.
// Ported from the Claude Design "Shorts Ranking Templates" component: the DOM
// builders, easing and timing constants mirror backend/animate.py exactly, so
// the canvas ▶ Play reveal is a faithful (instant) preview of the baked render.
// ============================================================================
const VCE_T = { ANIM: .55, ROW_STEP: .14, TITLE_TO_ROWS: .22, PART_STEP: .10, NEW_ITEM_LEAD: .15, SUB_LEAD: .10, SLIDE_PX: 1920 * .045 };
const VCE_ICON_COLORS = ["#ffffff", "#ff4655", "#2ea6ff", "#f5b942", "#59e0c5", "#a259ff"];
// accent swatch presets per style (from the design's props panel)
const VCE_ACCENT_SWATCHES = {
  pill:      ["#2ea6ff", "#f5b942", "#59e0c5", "#ff6f91"],
  minimal:   ["#f5b942", "#59e0c5", "#2ea6ff", "#ff6f91"],
  editorial: ["#ff5a5f", "#f5b942", "#2ea6ff", "#8b7cff"],
};
const VCE_ACCENT_DEFAULT = { pill: "#2ea6ff", minimal: "#F5B942", editorial: "#FF5A5F" };
const VCE_BONUS_SEED = {
  sticker: "just wait for the second one",
  marker: "You won't believe #1",
  tag: "wait for it...",
  spark: "just wait for it",
};
let vceParts = null, vceRaf = null, vceSfxTimer = null, vceHideTimer = null, vceBonusSfxTimer = null;
let vceClipTimers = [], vceHandles = [], vceIconImg = null, vceIconEl = null;
let vceClipEl = null, vceActiveClipIdx = null, vceScale = 1;

function vceLabels() { return AL.fmt.ranks.map(r => r.label || ""); }
function vceOff() { return AL.vce.off[AL.vce.style] || (AL.vce.off[AL.vce.style] = { tx: 0, ty: 0, lx: 0, ly: 0, ts: 1, ls: 1 }); }
// active-style accent colour + the set of accented (lowercased) title words
function vceAccent() {
  const a = AL.vce.accent || (AL.vce.accent = {});
  return a[AL.vce.style] || (a[AL.vce.style] = VCE_ACCENT_DEFAULT[AL.vce.style] || "#2ea6ff");
}
function vceAccentSet() { return new Set((AL.vce.accentWords || []).map(w => w.toLowerCase())); }
function vceToggleAccentWord(word) {
  const w = (word || "").toLowerCase(); if (!w) return;
  const list = AL.vce.accentWords || (AL.vce.accentWords = []);
  const i = list.indexOf(w);
  if (i >= 0) list.splice(i, 1); else list.push(w);
  vceRebuild();
}
// Ensure the per-phase reveal store exists + is fully populated (defaults filled).
function vceNormalizeAnim(v) {
  if (!v.anim || typeof v.anim !== "object") v.anim = {};
  ["intro", "new_item"].forEach(ph => {
    v.anim[ph] = Object.assign({ motion: "pop", sound: "pop", volume: 1.0, speed: 1.0 }, v.anim[ph] || {});
  });
  return v.anim;
}
// The reveal Motion/Sound/Volume/Speed for the phase the Reveal toggle selects.
function vceCfg() { return vceNormalizeAnim(AL.vce)[AL.vce.mode] || AL.vce.anim.intro; }
function vceMk(tag, css, text) { const e = document.createElement(tag); if (css) e.style.cssText = css; if (text != null) e.textContent = text; return e; }
function vceClamp01(p) { return p < 0 ? 0 : p > 1 ? 1 : p; }
function vceEaseOutCubic(p) { return 1 - Math.pow(1 - p, 3); }
function vceEaseOutBack(p) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2); }
function vceXform(motion, p) {
  p = vceClamp01(p);
  const a = vceEaseOutCubic(Math.min(1, p * 1.35));
  if (motion === "slide") return { s: 1, dy: (1 - vceEaseOutCubic(p)) * VCE_T.SLIDE_PX, a };
  return { s: 0.60 + 0.40 * vceEaseOutBack(p), dy: 0, a };
}
function vceSetEl(el, s, dy, a) { el.style.opacity = a; el.style.transform = `translateY(${dy}px) scale(${s})`; }
function vceShowEl(el) { el.style.opacity = 1; el.style.transform = "none"; }
function vceHideEl(el) { el.style.opacity = 0; el.style.transform = "none"; }
function vceHexToRgba(hex, a) {
  let h = (hex || "#000000").replace("#", "");
  if (h.length === 3) h = h.split("").map(c => c + c).join("");
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
function vceSetOut(id, val) { const o = $("#" + id); if (o) o.textContent = val; }
// Streamer reaction catalog (from /api/formatter/meta). Keyed lookup mirrors
// formatter.STREAMER_BY_KEY so the canvas + build agree.
function vceStreamers() { return (AL.fmtMeta && AL.fmtMeta.streamers) || []; }
function vceStreamerByKey(k) { return vceStreamers().find(s => s.key === k) || null; }
// per-player twitch handles (players have no twitch tag in the catalog yet, so
// autofill the handful we know; everyone else starts blank and can be typed in)
const VCE_PLAYER_HANDLES = { tenz: "@TenZ", shahzam: "@ShahZaM" };
// legacy clips stored the reaction as a streamer key; migrate to a react object
function vceMigrateClipReact(c) {
  if (!c || c.react || !c.streamer) return;
  const s = vceStreamerByKey(c.streamer);
  c.react = s ? { src: s.src, name: s.name, key: s.key, handle: s.handle } : null;
  delete c.streamer;
}
// unique streamer keys pinned across every rank's clip info (for "Streamers
// React"). Only streamers (react.key set) — player reactions have no key.
function vceStreamerKeys() {
  const seen = [];
  (AL.vce.clips || []).forEach(c => {
    vceMigrateClipReact(c);
    const k = c && c.react && c.react.key;
    if (k && !seen.includes(k)) seen.push(k);
  });
  return seen;
}

// ---- wiring -----------------------------------------------------------------
function initVceEditor() {
  const v = AL.vce, panel = $("#vce-panel");
  if (!panel) return;

  // sound catalog (needs /api/fx)
  const fillSounds = () => { $("#vce-sound").innerHTML = soundOptionsHtml(vceCfg().sound || "pop"); fillBonusSounds(); };
  if (AL.fxData.sounds && AL.fxData.sounds.length) fillSounds();
  else api("/api/fx").then(d => { AL.fxData = d; fillSounds(); }).catch(fillSounds);

  // segmented controls + popover picks (event delegation, like the design)
  panel.addEventListener("click", e => {
    const sw = e.target.closest("[data-icon-color]");
    if (sw) { v.icon.color = sw.dataset.iconColor; $("#vce-icon-color").value = v.icon.color; vceApplyIcon(); vceSyncSwatches(); return; }
    const asw = e.target.closest("[data-accent-color]");
    if (asw) { v.accent[v.style] = asw.dataset.accentColor; $("#vce-accent-color").value = asw.dataset.accentColor; vceRebuild(); vceSyncAccentSwatches(); return; }
    const seg = e.target.closest("[data-seg] > button");
    if (seg) {
      const k = seg.parentElement.dataset.seg, val = seg.dataset.v;
      if (k === "style") { v.style = val; vceRebuild(); vceSyncPosControls(); vceSyncColorControls(); }
      else if (k === "mode") { v.mode = val; vceSyncRevealControls(); vcePlay(); }
      else if (k === "motion") { vceCfg().motion = val; vceSyncControls(); vcePlay(); }
      else if (k === "titleLines") { v.titleLines = (val === "1") ? 1 : 2; vceRebuild(); }
      else if (k === "bonusStyle") {
        v.bonus.style = val;
        v.bonus.text = VCE_BONUS_SEED[val];
        $("#vce-bonus-text").value = v.bonus.text;
        vceRebuild();
      }
      vceSyncControls(); return;
    }
    const wt = e.target.closest("[data-wtoggle]");
    if (wt) { vceToggleWidgetPop(+wt.dataset.wtoggle); return; }
    const wp = e.target.closest("[data-wpick]");
    if (wp) {
      const i = +wp.dataset.wpick;
      vceAssignWidget(i, wp.dataset.clear ? null :
        { src: wp.dataset.src, name: wp.dataset.name, kind: wp.dataset.kind });
      return;
    }
    const cg = e.target.closest("[data-cliptoggle]");
    if (cg) { vceToggleClipPop(+cg.dataset.cliptoggle); return; }
    const cclr = e.target.closest("[data-clipclear]");
    if (cclr) { vceClearClip(+cclr.dataset.clipclear); return; }
    const cknd = e.target.closest("[data-clipkind]");
    if (cknd) { vceSetClipKind(cknd.dataset.clipkind); return; }
    const crx = e.target.closest("[data-clipreact]");
    if (crx) {
      const i = +crx.dataset.clipreact;
      if ((AL.vce.clips[i] || {}).react) vceSetClipReact(i, null);
      else { const s = $(`#vce-panel [data-clipreactsearch="${i}"]`); if (s) s.focus(); }
      return;
    }
    const crp = e.target.closest("[data-clipreactpick]");
    if (crp) {
      vceSetClipReact(+crp.dataset.clipreactpick, {
        src: crp.dataset.src, name: crp.dataset.name,
        key: crp.dataset.key || null, handle: crp.dataset.handle || "",
      });
      return;
    }
    const ct = e.target.closest("[data-clipteam]");
    if (ct) { const s = panel.querySelector(`[data-clipsearch="${ct.dataset.clipteam}"]`); if (s) s.focus(); return; }
    const cp = e.target.closest("[data-clippick]");
    if (cp) { vceSetClipTeam(cp.dataset.clippick, { src: cp.dataset.src, name: cp.dataset.name }); return; }
  });
  panel.addEventListener("input", e => {
    if (e.target.dataset.wsearch != null) { vceWidgetResults(+e.target.dataset.wsearch, e.target.value); return; }
    if (e.target.dataset.clipsearch != null) { vceClipSearch(e.target.dataset.clipsearch, e.target.value); return; }
    if (e.target.dataset.clipcap != null) { vceSetClipCaption(+e.target.dataset.clipcap, e.target.value); return; }
    if (e.target.dataset.clipreactsearch != null) { vceClipReactSearch(+e.target.dataset.clipreactsearch, e.target.value); return; }
    if (e.target.dataset.cliphandle != null) { vceSetClipHandle(+e.target.dataset.cliphandle, e.target.value); return; }
    if (e.target.classList.contains("vce-li-in")) {
      const i = +e.target.dataset.i;
      AL.fmt.ranks[i].label = e.target.value;
      const slotIn = $$("#fmt-ranks .rank-label-in")[i];
      if (slotIn && slotIn.value !== e.target.value) slotIn.value = e.target.value;
      vceSyncTarget(); vceRebuild(); return;
    }
  });

  $("#vce-title").value = v.title;
  $("#vce-title").addEventListener("input", e => { v.title = e.target.value; vceRebuild(); });
  $("#vce-target").addEventListener("change", e => { v.target = parseInt(e.target.value, 10) || 0; vcePlay(); });
  $("#vce-sound").addEventListener("change", e => { vceCfg().sound = e.target.value; });
  $("#vce-volume").addEventListener("input", e => { vceCfg().volume = (+e.target.value) / 100; vceSetOut("vce-volume-out", e.target.value + "%"); });
  $("#vce-speed").addEventListener("input", e => { vceCfg().speed = +e.target.value; vceSetOut("vce-speed-out", (+e.target.value).toFixed(2) + "×"); });

  // bonus
  $("#vce-bonus-on").addEventListener("change", e => { v.bonus.on = e.target.checked; vceSyncControls(); vceRebuild(); });
  $("#vce-bonus-text").value = v.bonus.text;
  $("#vce-bonus-text").addEventListener("input", e => { v.bonus.text = e.target.value; vceRebuild(); });
  $("#vce-bonus-size").addEventListener("input", e => { v.bonus.size = +e.target.value; vceSetOut("vce-bonus-size-out", Math.round(v.bonus.size * 100) + "%"); vceRebuild(); });
  $("#vce-bonus-hide").addEventListener("change", e => { v.bonus.hideOn = e.target.checked; vceSyncControls(); vcePlay(); });
  $("#vce-bonus-secs").addEventListener("input", e => {
    v.bonus.hideSecs = Math.max(0.5, +e.target.value || 5);
    vceSetOut("vce-bonus-secs-out", (+e.target.value).toFixed(1) + "s"); vcePlay();
  });
  // bonus delayed entrance (intro only): after N seconds, own speed + SFX
  $("#vce-bonus-delay-on").addEventListener("change", e => { v.bonus.delayOn = e.target.checked; vceSyncControls(); vcePlay(); });
  $("#vce-bonus-delay").addEventListener("input", e => {
    v.bonus.delay = Math.max(0, +e.target.value || 0);
    vceSetOut("vce-bonus-delay-out", v.bonus.delay.toFixed(1) + "s"); vcePlay();
  });
  $("#vce-bonus-speed").addEventListener("input", e => {
    v.bonus.speed = Math.max(0.25, +e.target.value || 1);
    vceSetOut("vce-bonus-speed-out", v.bonus.speed.toFixed(2) + "×"); vcePlay();
  });
  fillBonusSounds();
  $("#vce-bonus-sound").addEventListener("change", e => { v.bonus.sound = e.target.value; });
  $("#vce-bonus-sound-play").onclick = () => auditionCombinedSfx(v.bonus.sound, v.bonus.volume);
  $("#vce-bonus-vol").addEventListener("input", e => {
    v.bonus.volume = (+e.target.value || 0) / 100;
    vceSetOut("vce-bonus-vol-out", Math.round(v.bonus.volume * 100) + "%");
  });

  // valorant subtitle
  $("#vce-sub-on").addEventListener("change", e => { v.subtitle.on = e.target.checked; vceSyncControls(); vceRebuild(); });
  $("#vce-sub-text").value = v.subtitle.text;
  $("#vce-sub-text").addEventListener("input", e => { v.subtitle.text = e.target.value; vceRebuild(); });
  $("#vce-sub-after").value = v.subtitle.after || "";
  $("#vce-sub-after").addEventListener("input", e => { v.subtitle.after = e.target.value; vceRebuild(); });
  $("#vce-sub-size").addEventListener("input", e => { v.subtitle.size = +e.target.value; vceSetOut("vce-sub-size-out", Math.round(v.subtitle.size * 100) + "%"); vceRebuild(); });
  $("#vce-sub-streamers").addEventListener("change", e => { v.subtitle.streamers = e.target.checked; vceRebuild(); });

  // icon backdrop
  $("#vce-icon-on").addEventListener("change", e => { v.icon.on = e.target.checked; vceSyncControls(); vceApplyIcon(); });
  $("#vce-icon-color").addEventListener("input", e => { v.icon.color = e.target.value; vceApplyIcon(); vceSyncSwatches(); });
  $("#vce-icon-opacity").addEventListener("input", e => { v.icon.opacity = +e.target.value; vceApplyIcon(); vceSetOut("vce-icon-opacity-out", e.target.value + "%"); });
  $("#vce-icon-size").addEventListener("input", e => { v.icon.size = +e.target.value; vceApplyIcon(); vceSetOut("vce-icon-size-out", e.target.value); });
  const swBox = $("#vce-icon-swatches");
  VCE_ICON_COLORS.forEach(c => {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.iconColor = c; b.style.background = c;
    swBox.appendChild(b);
  });

  // title accent colour + pill colour
  $("#vce-accent-color").addEventListener("input", e => { v.accent[v.style] = e.target.value; vceRebuild(); vceSyncAccentSwatches(); });
  $("#vce-pill-color").addEventListener("input", e => { v.pillFill = e.target.value; vceRebuild(); });
  $("#vce-medals-on").addEventListener("change", e => { v.medalsOn = e.target.checked; vceRebuild(); });
  $("#vce-medal-text-on").addEventListener("change", e => { v.medalText = e.target.checked; vceRebuild(); });
  vceBuildAccentSwatches();
  vceIconImg = new Image();
  vceIconImg.onload = () => vceApplyIcon();
  vceIconImg.src = AL.fmtMeta.vct_icon || "/assets/vct-icon.png";

  // clip-info shared style
  const cs = v.clipStyle;
  $("#vce-clip-bg").addEventListener("input", e => { cs.bg = e.target.value; vceRenderClipBox(vceActiveClipIdx); });
  $("#vce-clip-accent").addEventListener("input", e => { cs.accent = e.target.value; vceRenderClipBox(vceActiveClipIdx); });
  $("#vce-clip-text").addEventListener("input", e => { cs.text = e.target.value; vceRenderClipBox(vceActiveClipIdx); });
  $("#vce-clip-size").addEventListener("input", e => { cs.size = +e.target.value; vceSetOut("vce-clip-size-out", Math.round(cs.size * 100) + "%"); vceRenderClipBox(vceActiveClipIdx); });
  $("#vce-clip-textsize").addEventListener("input", e => { cs.textSize = +e.target.value; vceSetOut("vce-clip-textsize-out", Math.round(cs.textSize * 100) + "%"); vceRenderClipBox(vceActiveClipIdx); });
  $("#vce-clip-opacity").addEventListener("input", e => { cs.opacity = (+e.target.value) / 100; vceSetOut("vce-clip-opacity-out", (100 - +e.target.value) + "%"); vceRenderClipBox(vceActiveClipIdx); });

  // position / size sliders (drag offsets, in design px @1080×1920)
  const POSMAP = { titleX: "tx", titleY: "ty", listX: "lx", listY: "ly", titleS: "ts", listS: "ls" };
  Object.keys(POSMAP).forEach(c => {
    $("#vce-" + c).addEventListener("input", e => {
      vceOff()[POSMAP[c]] = +e.target.value;
      vceApplyOffsets();
      vceSetOut("vce-" + c + "-out", c.endsWith("S") ? Math.round(+e.target.value * 100) + "%" : Math.round(+e.target.value) + "px");
    });
  });
  $("#vce-resize").addEventListener("change", e => { v.resize = e.target.checked; vceSetResizeVisible(e.target.checked); });

  // stage toggles + play
  $("#vce-play").onclick = vcePlay;
  $("#vce-phone-tg").addEventListener("change", e => { v.phone = e.target.checked; vceSetPhone(v.phone); });
  $("#vce-overclip").addEventListener("change", e => { v.overClip = e.target.checked; refreshStageBg(true); });
  $("#anim-bake").checked = v.bake;
  $("#anim-bake").addEventListener("change", e => { v.bake = e.target.checked; });
  $("#anim-preview-btn").onclick = runAnimPreview;

  window.addEventListener("resize", vceFit);
  vceApplyVceToControls();
  vceBuildListInputs();
  vceSyncTarget();
  vceRebuild();
  vceFit();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { vceFit(); vceRebuild(); });
}

// push AL.vce values into the panel controls (used at init + project restore)
function vceApplyVceToControls() {
  const v = AL.vce;
  $("#vce-title").value = v.title;
  $("#vce-bonus-on").checked = v.bonus.on;
  $("#vce-bonus-text").value = v.bonus.text;
  $("#vce-bonus-size").value = v.bonus.size ?? 1;
  vceSetOut("vce-bonus-size-out", Math.round((v.bonus.size ?? 1) * 100) + "%");
  $("#vce-bonus-hide").checked = v.bonus.hideOn;
  $("#vce-bonus-secs").value = v.bonus.hideSecs;
  vceSetOut("vce-bonus-secs-out", (+v.bonus.hideSecs).toFixed(1) + "s");
  $("#vce-bonus-delay-on").checked = !!v.bonus.delayOn;
  $("#vce-bonus-delay").value = v.bonus.delay ?? 1;
  vceSetOut("vce-bonus-delay-out", (+(v.bonus.delay ?? 1)).toFixed(1) + "s");
  $("#vce-bonus-speed").value = v.bonus.speed ?? 1;
  vceSetOut("vce-bonus-speed-out", (+(v.bonus.speed ?? 1)).toFixed(2) + "×");
  $("#vce-bonus-vol").value = Math.round((v.bonus.volume ?? 1) * 100);
  vceSetOut("vce-bonus-vol-out", Math.round((v.bonus.volume ?? 1) * 100) + "%");
  fillBonusSounds();
  $("#vce-sub-on").checked = v.subtitle.on;
  $("#vce-sub-text").value = v.subtitle.text;
  $("#vce-sub-after").value = v.subtitle.after || "";
  $("#vce-sub-size").value = v.subtitle.size;
  vceSetOut("vce-sub-size-out", Math.round(v.subtitle.size * 100) + "%");
  $("#vce-sub-streamers").checked = v.subtitle.streamers;
  $("#vce-icon-on").checked = v.icon.on;
  $("#vce-icon-color").value = v.icon.color;
  $("#vce-icon-opacity").value = v.icon.opacity;
  vceSetOut("vce-icon-opacity-out", v.icon.opacity + "%");
  $("#vce-icon-size").value = v.icon.size;
  vceSetOut("vce-icon-size-out", String(v.icon.size));
  $("#vce-clip-bg").value = v.clipStyle.bg;
  $("#vce-clip-accent").value = v.clipStyle.accent;
  $("#vce-clip-text").value = v.clipStyle.text;
  $("#vce-clip-size").value = v.clipStyle.size;
  vceSetOut("vce-clip-size-out", Math.round(v.clipStyle.size * 100) + "%");
  $("#vce-clip-textsize").value = v.clipStyle.textSize;
  vceSetOut("vce-clip-textsize-out", Math.round(v.clipStyle.textSize * 100) + "%");
  $("#vce-clip-opacity").value = Math.round(v.clipStyle.opacity * 100);
  vceSetOut("vce-clip-opacity-out", (100 - Math.round(v.clipStyle.opacity * 100)) + "%");
  $("#vce-resize").checked = v.resize;
  const cfg = vceCfg();
  $("#vce-volume").value = Math.round(cfg.volume * 100);
  vceSetOut("vce-volume-out", Math.round(cfg.volume * 100) + "%");
  $("#vce-speed").value = cfg.speed;
  vceSetOut("vce-speed-out", (+cfg.speed).toFixed(2) + "×");
  $("#vce-overclip").checked = v.overClip;
  $("#vce-phone-tg").checked = !!v.phone;
  vceSetPhone(!!v.phone);
  const snd = $("#vce-sound"); if (snd && snd.options.length) snd.value = cfg.sound;
  vceSyncControls();
  vceSyncSwatches();
  vceSyncPosControls();
  vceSyncColorControls();
}

function vceSyncControls() {
  const v = AL.vce;
  $$("#vce-panel [data-seg]").forEach(seg => {
    const key = seg.dataset.seg;
    const cur = key === "style" ? v.style : key === "mode" ? v.mode : key === "motion" ? vceCfg().motion
      : key === "bonusStyle" ? v.bonus.style : key === "titleLines" ? String(v.titleLines === 1 ? 1 : 2) : v.bonus.pos;
    seg.querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.v === cur));
  });
  $("#vce-target-sec").classList.toggle("hidden", v.mode !== "new_item");
  $("#vce-bonus-fields").classList.toggle("hidden", !v.bonus.on);
  $("#vce-sub-fields").classList.toggle("hidden", !v.subtitle.on);
  $("#vce-icon-fields").classList.toggle("hidden", !v.icon.on);
  $("#vce-bonus-hide-row").style.display = v.bonus.hideOn ? "flex" : "none";
  $("#vce-bonus-delay-fields").classList.toggle("hidden", !v.bonus.delayOn);
  // reflect which reveal phase the Motion/Sound/Volume/Speed controls edit
  const phase = v.mode === "new_item" ? "New item" : "Intro";
  $$("#vce-panel .vce-phase-tag").forEach(el => { el.textContent = "· " + phase; });
}
// Reload the Motion/Sound/Volume/Speed controls for the currently-selected
// reveal phase (called when the Reveal toggle flips between Intro / New item).
function vceSyncRevealControls() {
  const cfg = vceCfg();
  const snd = $("#vce-sound"); if (snd && snd.options.length) snd.value = cfg.sound;
  $("#vce-volume").value = Math.round(cfg.volume * 100);
  vceSetOut("vce-volume-out", Math.round(cfg.volume * 100) + "%");
  $("#vce-speed").value = cfg.speed;
  vceSetOut("vce-speed-out", (+cfg.speed).toFixed(2) + "×");
  vceSyncControls();   // Motion active button + phase tags
}
function vceSyncSwatches() {
  $$("#vce-icon-swatches button").forEach(b =>
    b.classList.toggle("active", b.dataset.iconColor === AL.vce.icon.color));
}
// accent swatches are style-specific — rebuild them when the style changes
function vceBuildAccentSwatches() {
  const box = $("#vce-accent-swatches"); if (!box) return;
  box.innerHTML = "";
  (VCE_ACCENT_SWATCHES[AL.vce.style] || []).forEach(c => {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.accentColor = c; b.style.background = c;
    box.appendChild(b);
  });
  vceSyncAccentSwatches();
}
function vceSyncAccentSwatches() {
  const cur = (vceAccent() || "").toLowerCase();
  $$("#vce-accent-swatches button").forEach(b =>
    b.classList.toggle("active", (b.dataset.accentColor || "").toLowerCase() === cur));
}
// reflect the accent colour picker, pill picker (+ visibility) for the active style
function vceSyncColorControls() {
  $("#vce-accent-color").value = vceAccent();
  $("#vce-pill-color").value = AL.vce.pillFill || "#a9c9ec";
  $("#vce-pill-row").style.display = AL.vce.style === "pill" ? "" : "none";
  // Medal ranks: Blue Pill only (numbers + label text, independent)
  const isPill = AL.vce.style === "pill";
  $("#vce-medals-on").checked = !!AL.vce.medalsOn;
  $("#vce-medal-text-on").checked = !!AL.vce.medalText;
  $("#vce-medal-row").style.display = isPill ? "" : "none";
  $("#vce-medal-text-row").style.display = isPill ? "" : "none";
  vceBuildAccentSwatches();
}
function vceSyncPosControls() {
  const o = vceOff(), M = { titleX: "tx", titleY: "ty", listX: "lx", listY: "ly", titleS: "ts", listS: "ls" };
  Object.keys(M).forEach(c => {
    const inp = $("#vce-" + c); if (!inp) return;
    inp.value = o[M[c]];
    vceSetOut("vce-" + c + "-out", c.endsWith("S") ? Math.round(o[M[c]] * 100) + "%" : Math.round(o[M[c]]) + "px");
  });
}
function vceSyncTarget() {
  const sel = $("#vce-target"); if (!sel) return;
  if (AL.vce.target >= AL.fmt.ranks.length) AL.vce.target = 0;
  sel.innerHTML = AL.fmt.ranks.map((r, i) =>
    `<option value="${i}"${i === AL.vce.target ? " selected" : ""}>${i + 1}. ${(r.label || "(no label)").replace(/"/g, "&quot;")}</option>`
  ).join("");
}
// rank count changed (add/remove): resize per-rank arrays + rebuild everything
function vceRanksChanged() {
  const n = AL.fmt.ranks.length;
  AL.vce.widgets.length = n;
  AL.vce.clips.length = n;
  vceBuildListInputs();
  vceSyncTarget();
  vceRebuild();
  refreshStageBg();
}

// ---- per-rank list inputs (label + ◈ widget + ▦ clip info) -------------------
function vceBuildListInputs() {
  const box = $("#vce-list-inputs"); if (!box) return;
  box.innerHTML = "";
  AL.fmt.ranks.forEach((r, i) => {
    const wrap = document.createElement("div");
    const w = AL.vce.widgets[i];
    const cOn = vceClipHasContent(AL.vce.clips[i]);
    wrap.innerHTML = `
      <div class="vce-li-row">
        <span class="vce-li-num">${i + 1}.</span>
        <input class="vce-li-in" type="text" data-i="${i}" value="${(r.label || "").replace(/"/g, "&quot;")}" placeholder="Rank ${i + 1} label" />
        <button type="button" class="vce-li-btn${w ? " set" : ""}" data-wtoggle="${i}" title="Add player / weapon widget">${w ? `<img src="${w.src}">` : "◈"}</button>
        <button type="button" class="vce-li-btn${cOn ? " set" : ""}" data-cliptoggle="${i}" title="Add clip info for this item">▦</button>
      </div>
      <div class="vce-pop hidden" data-wp="${i}">
        <input type="text" class="vce-pop-search" data-wsearch="${i}" placeholder="/plrs cned   ·   /weapon vandal" />
        <div class="vce-pop-grid" data-wres="${i}"></div>
      </div>
      <div class="vce-pop hidden" data-cp="${i}">
        <div class="vce-seg vce-clipkind" style="margin-bottom:10px">
          <button type="button" data-clipkind="${i}-competition">Competition Clip</button>
          <button type="button" data-clipkind="${i}-ranked">Ranked Clip</button>
        </div>
        <div data-clipcompete="${i}">
          <div class="vce-pop-teamrow">
            <button type="button" class="vce-li-btn" data-clipteam="${i}-a">＋</button>
            <input type="text" data-clipsearch="${i}-a" placeholder="Team A — e.g. sentinels" />
          </div>
          <div class="vce-pop-grid orgs hidden" data-clipres="${i}-a"></div>
          <div class="vce-pop-teamrow">
            <button type="button" class="vce-li-btn" data-clipteam="${i}-b">＋</button>
            <input type="text" data-clipsearch="${i}-b" placeholder="Team B — e.g. paper rex" />
          </div>
          <div class="vce-pop-grid orgs hidden" data-clipres="${i}-b"></div>
        </div>
        <input type="text" class="vce-pop-cap" data-clipcap="${i}" placeholder="Caption — e.g. Tenz 1v5" />
        <div class="vce-pop-note hidden" data-clipcapnote="${i}">Caption is required for a Ranked Clip.</div>
        <div class="vce-pop-sublab">Reaction — player or streamer <span class="dim">· head appears above the card</span></div>
        <div class="vce-pop-teamrow">
          <button type="button" class="vce-str-btn" data-clipreact="${i}" title="Pick person">＋</button>
          <input type="text" data-clipreactsearch="${i}" placeholder="/plrs tenz   ·   or a streamer name" />
        </div>
        <div class="vce-pop-grid react hidden" data-clipreactres="${i}"></div>
        <input type="text" class="vce-pop-cap vce-pop-handle hidden" data-cliphandle="${i}" placeholder="@twitch handle" />
        <div class="vce-pop-hint hidden" data-cliphandlenote="${i}">Optional — leave blank to hide the handle.</div>
        <button type="button" class="vce-pop-remove" data-clipclear="${i}">✕ Remove clip info</button>
      </div>`;
    box.appendChild(wrap);
    vceFillClipFields(i);
  });
}
function vceUpdateWidgetBtn(i) {
  const b = $(`#vce-panel [data-wtoggle="${i}"]`); if (!b) return;
  const w = AL.vce.widgets[i];
  b.innerHTML = w ? `<img src="${w.src}">` : "◈";
  b.classList.toggle("set", !!w);
}
function vceToggleWidgetPop(i) {
  const pop = $(`#vce-panel [data-wp="${i}"]`); if (!pop) return;
  const open = pop.classList.contains("hidden");
  $$("#vce-panel [data-wp], #vce-panel [data-cp]").forEach(p => p.classList.add("hidden"));
  if (open) {
    pop.classList.remove("hidden");
    const s = pop.querySelector("[data-wsearch]");
    vceWidgetResults(i, s ? s.value : "");
    if (s) setTimeout(() => s.focus(), 0);
  }
}
function vceWidgetResults(i, raw) {
  const res = $(`#vce-panel [data-wres="${i}"]`); if (!res) return;
  const D = AL.vcePicker || { players: [], weapons: [] };
  let q = (raw || "").trim().toLowerCase(), pool;
  const P = (D.players || []).map(x => ({ ...x, kind: "player" }));
  const W = (D.weapons || []).map(x => ({ ...x, kind: "weapon" }));
  if (q.startsWith("/plrs")) { pool = P; q = q.slice(5).trim(); }
  else if (q.startsWith("/weapon")) { pool = W; q = q.slice(7).trim(); }
  else if (q.startsWith("/")) { pool = P.concat(W); q = q.replace(/^\/\S*/, "").trim(); }
  else pool = P.concat(W);
  const terms = q.split(/\s+/).filter(Boolean);
  const list = pool.filter(x => terms.every(t => x.q.includes(t))).slice(0, 48);
  res.innerHTML = "";
  const clr = document.createElement("button");
  clr.type = "button"; clr.className = "vce-pop-clear";
  clr.dataset.wpick = i; clr.dataset.clear = "1";
  clr.textContent = AL.vce.widgets[i] ? "✕ Remove widget" : "No widget";
  res.appendChild(clr);
  list.forEach(x => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.wpick = i; b.dataset.src = x.src; b.dataset.name = x.name; b.dataset.kind = x.kind;
    b.title = x.name + (x.sub ? " — " + x.sub : "");
    b.innerHTML = `<img src="${x.src}" loading="lazy"><span>${x.name}</span>`;
    res.appendChild(b);
  });
  if (!list.length) {
    const none = document.createElement("div");
    none.className = "vce-pop-none";
    none.textContent = "No matches. Try /plrs or /weapon.";
    res.appendChild(none);
  }
}
function vceAssignWidget(i, data) {
  AL.vce.widgets[i] = data;
  vceUpdateWidgetBtn(i);
  const s = $(`#vce-panel [data-wsearch="${i}"]`);
  vceWidgetResults(i, s ? s.value : "");
  vceRebuild();
}

// ---- clip info (per rank) ----------------------------------------------------
function vceClipHasContent(c) {
  if (!c) return false;
  const hasReact = !!(c.react || c.streamer);
  if (c.kind === "ranked") return !!((c.caption || "").trim() || hasReact);
  return !!(c.a || c.b || (c.caption || "").trim() || hasReact);
}
function vceFirstClipIdx() { const i = AL.vce.clips.findIndex(vceClipHasContent); return i < 0 ? null : i; }
function vceUpdateClipBtn(i) {
  const b = $(`#vce-panel [data-cliptoggle="${i}"]`); if (!b) return;
  b.classList.toggle("set", vceClipHasContent(AL.vce.clips[i]));
}
function vceFillClipFields(i) {
  vceMigrateClipReact(AL.vce.clips[i]);
  const c = AL.vce.clips[i];
  const cap = $(`#vce-panel [data-clipcap="${i}"]`); if (cap) cap.value = c ? (c.caption || "") : "";
  ["a", "b"].forEach(slot => {
    const b = $(`#vce-panel [data-clipteam="${i}-${slot}"]`); if (!b) return;
    const t = c && c[slot];
    b.innerHTML = t ? `<img src="${t.src}">` : "＋";
    b.classList.toggle("set", !!t);
  });
  vceUpdateClipReactButton(i);
  vceUpdateClipKindButtons(i);
  vceUpdateClipModeFields(i);
}
// ---- clip kind: Competition vs Ranked ----
function vceSetClipKind(key) {
  const dash = key.indexOf("-");
  const i = +key.slice(0, dash), kind = key.slice(dash + 1);
  if (!AL.vce.clips[i]) AL.vce.clips[i] = { a: null, b: null, caption: "" };
  AL.vce.clips[i].kind = kind;
  vceUpdateClipKindButtons(i);
  vceUpdateClipModeFields(i);
  vceUpdateClipBtn(i);
  vceActiveClipIdx = i;
  vceRenderClipBox(i, true);
}
function vceUpdateClipKindButtons(i) {
  const kind = (AL.vce.clips[i] || {}).kind || "competition";
  $$(`#vce-panel [data-clipkind]`).forEach(b => {
    const p = b.dataset.clipkind.split("-");
    if (+p[0] !== i) return;
    b.classList.toggle("active", p[1] === kind);
  });
}
function vceUpdateClipModeFields(i) {
  const ranked = ((AL.vce.clips[i] || {}).kind || "competition") === "ranked";
  const comp = $(`#vce-panel [data-clipcompete="${i}"]`);
  const note = $(`#vce-panel [data-clipcapnote="${i}"]`);
  if (comp) comp.classList.toggle("hidden", ranked);
  if (note) note.classList.toggle("hidden", !ranked);
}
// ---- reaction: player OR streamer head above the card (+ optional @handle) ----
function vceUpdateClipReactButton(i) {
  const rc = (AL.vce.clips[i] || {}).react;
  const b = $(`#vce-panel [data-clipreact="${i}"]`);
  if (b) {
    b.innerHTML = rc ? `<img src="${rc.src}">` : "＋";
    b.classList.toggle("set", !!rc);
    b.title = rc ? "Remove reaction" : "Pick person";
  }
  const hin = $(`#vce-panel [data-cliphandle="${i}"]`);
  const hnote = $(`#vce-panel [data-cliphandlenote="${i}"]`);
  if (hin) {
    hin.classList.toggle("hidden", !rc);
    hin.value = rc ? (rc.handle || "") : "";
    hin.placeholder = rc && rc.key ? "@twitch handle" : "@twitch handle (optional)";
  }
  // only players (no streamer key) get the "leave blank to hide" hint
  if (hnote) hnote.classList.toggle("hidden", !(rc && !rc.key));
}
function vceClipReactSearch(i, raw) {
  const res = $(`#vce-panel [data-clipreactres="${i}"]`); if (!res) return;
  let q = (raw || "").trim().toLowerCase();
  if (!q) { res.classList.add("hidden"); res.innerHTML = ""; return; }
  const D = AL.vcePicker || { players: [] };
  const P = (D.players || []).map(x => ({ src: x.src, name: x.name, q: x.q, key: null, handle: VCE_PLAYER_HANDLES[(x.name || "").toLowerCase()] || "" }));
  const ST = vceStreamers().map(s => ({ src: s.src, name: s.name, q: (s.name + " " + s.key).toLowerCase(), key: s.key, handle: s.handle }));
  let pool;
  if (q.startsWith("/plrs")) { pool = P; q = q.slice(5).trim(); }
  else if (q.startsWith("/")) { pool = ST.concat(P); q = q.replace(/^\/\S*/, "").trim(); }
  else pool = ST.concat(P);
  const terms = q.split(/\s+/).filter(Boolean);
  const list = pool.filter(x => terms.every(t => x.q.includes(t))).slice(0, 40);
  res.innerHTML = "";
  list.forEach(x => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.clipreactpick = i; b.dataset.src = x.src; b.dataset.name = x.name; b.title = x.name;
    if (x.key) b.dataset.key = x.key;
    if (x.handle) b.dataset.handle = x.handle;
    b.innerHTML = `<img src="${x.src}" loading="lazy"><span>${x.name}</span>`;
    res.appendChild(b);
  });
  if (!list.length) res.innerHTML = `<div class="vce-pop-none">No matches. Try /plrs or a streamer.</div>`;
  res.classList.remove("hidden");
}
function vceSetClipReact(i, data) {
  if (!AL.vce.clips[i]) AL.vce.clips[i] = { a: null, b: null, caption: "" };
  AL.vce.clips[i].react = data;
  vceUpdateClipReactButton(i);
  vceUpdateClipBtn(i);
  const res = $(`#vce-panel [data-clipreactres="${i}"]`); if (res) { res.classList.add("hidden"); res.innerHTML = ""; }
  const s = $(`#vce-panel [data-clipreactsearch="${i}"]`); if (s) s.value = "";
  vceActiveClipIdx = i;
  vceRenderClipBox(i, true);
  // the streamers-react subtitle depends on the pinned set → rebuild it
  if (AL.vce.subtitle.on && AL.vce.subtitle.streamers) vceRebuild();
}
function vceSetClipHandle(i, v) {
  const c = AL.vce.clips[i]; if (!c || !c.react) return;
  c.react.handle = v;
  vceActiveClipIdx = i;
  vceRenderClipBox(i);
}
function vceToggleClipPop(i) {
  const pop = $(`#vce-panel [data-cp="${i}"]`); if (!pop) return;
  const open = pop.classList.contains("hidden");
  $$("#vce-panel [data-wp], #vce-panel [data-cp]").forEach(p => p.classList.add("hidden"));
  if (open) {
    if (!AL.vce.clips[i]) AL.vce.clips[i] = { a: null, b: null, caption: "" };
    pop.classList.remove("hidden");
    vceFillClipFields(i);
    vceActiveClipIdx = i;
    vceRenderClipBox(i);
  }
}
function vceClipSearch(key, raw) {
  const [i, slot] = key.split("-");
  const res = $(`#vce-panel [data-clipres="${i}-${slot}"]`); if (!res) return;
  const q = (raw || "").trim().toLowerCase();
  if (!q) { res.classList.add("hidden"); res.innerHTML = ""; return; }
  const terms = q.split(/\s+/).filter(Boolean);
  const list = ((AL.vcePicker || {}).orgs || []).filter(o => terms.every(t => o.q.includes(t))).slice(0, 30);
  res.innerHTML = "";
  list.forEach(o => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.clippick = `${i}-${slot}`; b.dataset.src = o.src; b.dataset.name = o.name; b.title = o.name;
    b.innerHTML = `<img src="${o.src}" loading="lazy">`;
    res.appendChild(b);
  });
  if (!list.length) res.innerHTML = `<div class="vce-pop-none">No teams match.</div>`;
  res.classList.remove("hidden");
}
function vceSetClipTeam(key, org) {
  const [i, slot] = key.split("-");
  const idx = +i;
  if (!AL.vce.clips[idx]) AL.vce.clips[idx] = { a: null, b: null, caption: "" };
  AL.vce.clips[idx][slot] = org;
  vceFillClipFields(idx);
  vceUpdateClipBtn(idx);
  const res = $(`#vce-panel [data-clipres="${i}-${slot}"]`); if (res) { res.classList.add("hidden"); res.innerHTML = ""; }
  const s = $(`#vce-panel [data-clipsearch="${i}-${slot}"]`); if (s) s.value = "";
  vceActiveClipIdx = idx;
  vceRenderClipBox(idx);
}
function vceSetClipCaption(i, val) {
  if (!AL.vce.clips[i]) AL.vce.clips[i] = { a: null, b: null, caption: "" };
  AL.vce.clips[i].caption = val;
  vceUpdateClipBtn(i);
  vceActiveClipIdx = i;
  vceRenderClipBox(i);
}
function vceClearClip(i) {
  AL.vce.clips[i] = null;
  vceUpdateClipBtn(i); vceFillClipFields(i);
  if (vceActiveClipIdx === i) vceActiveClipIdx = vceFirstClipIdx();
  vceRenderClipBox(vceActiveClipIdx);
}
function vceRenderClipBox(idx, animate) {
  if (vceClipEl && vceClipEl.parentNode) vceClipEl.parentNode.removeChild(vceClipEl);
  vceClipEl = null;
  const c = (idx != null) ? AL.vce.clips[idx] : null;
  const stage = $("#vce-stage");
  if (!vceClipHasContent(c) || !stage) return;
  const isRanked = c.kind === "ranked";
  const S = AL.vce.clipStyle, k = S.size || 1, r = n => Math.round(n * k), tk = S.textSize || 1, tt = n => Math.round(n * k * tk);
  const wrap = vceMk("div", `position:absolute;right:46px;bottom:78px;z-index:5;display:flex;flex-direction:column;align-items:flex-end`);
  const box = vceMk("div", `position:relative;z-index:2;display:flex;flex-direction:column;gap:${r(15)}px;padding:${r(22)}px ${r(26)}px;border-radius:${r(24)}px;background:${vceHexToRgba(S.bg, S.opacity)};border:1px solid ${vceHexToRgba(S.text, 0.16)};box-shadow:0 18px 50px rgba(0,0,0,.5);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);max-width:720px`);
  // VD channel mark (replaces the old dot) — ring, D and "Now Playing" all track the accent
  const head = vceMk("div", `display:flex;align-items:center;gap:${r(12)}px`);
  const vd = vceMk("div", `position:relative;width:${tt(51)}px;height:${tt(51)}px;border-radius:50%;border:${Math.max(2, Math.round(tt(3)))}px solid ${S.accent};display:flex;align-items:center;justify-content:center;flex:none;background:rgba(0,0,0,.28);box-shadow:0 0 12px ${vceHexToRgba(S.accent, .45)}`);
  const letters = vceMk("div", `font-family:'Poppins',sans-serif;font-weight:900;font-style:italic;font-size:${tt(22.8)}px;line-height:1;letter-spacing:-1px;transform:translate(${-tt(2)}px, ${-tt(0.5)}px)`);
  letters.append(vceMk("span", "color:#fff", "V"), vceMk("span", `color:${S.accent}`, "D"));
  vd.append(letters);
  head.append(vd, vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${tt(29)}px;letter-spacing:2px;text-transform:uppercase;color:${S.accent}`, isRanked ? "Best Clips" : "Now Playing"));
  const logo = t => {
    if (!t) return vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${tt(40)}px;color:${vceHexToRgba(S.text, .5)}`, "?");
    const im = vceMk("img", `height:${r(92)}px;width:auto;max-width:${r(190)}px;object-fit:contain;filter:drop-shadow(0 3px 10px rgba(0,0,0,.5))`);
    im.src = t.src; return im;
  };
  box.append(head);
  if (!isRanked && (c.a || c.b)) {
    const mid = vceMk("div", `display:flex;align-items:center;gap:${r(20)}px`);
    mid.append(logo(c.a), vceMk("span", `font-family:'Anton',sans-serif;font-size:${tt(36)}px;color:${vceHexToRgba(S.text, .75)}`, "vs"), logo(c.b));
    box.append(mid);
  }
  if ((c.caption || "").trim()) box.append(vceMk("div", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${tt(38)}px;line-height:1.05;color:${S.text}`, c.caption));
  wrap.append(box);

  // reaction: player/streamer portrait pops over the card; @handle chip below
  // it (only when a handle is set — players may have none)
  const rc = c.react || null;
  let portImg = null;
  if (rc) {
    const portWrap = vceMk("div", `position:absolute;left:50%;top:0;transform:translate(-50%,-58%);pointer-events:none;z-index:1`);
    portImg = vceMk("img", `display:block;height:${r(172)}px;width:auto;transform-origin:bottom center;clip-path:inset(0 0 42% 0);filter:drop-shadow(0 6px 18px rgba(0,0,0,.55))`);
    portImg.src = rc.src; portWrap.append(portImg); wrap.append(portWrap);
    const handle = (rc.handle || "").trim();
    if (handle) {
      const hl = vceMk("div", `align-self:center;display:flex;align-items:center;gap:${r(11)}px;margin-top:${r(15)}px;padding:${r(9)}px ${r(20)}px;border-radius:999px;background:rgba(12,15,21,.62);border:1px solid ${vceHexToRgba(S.text, 0.14)};backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:0 10px 26px rgba(0,0,0,.4)`);
      const glyph = vceMk("img", `width:${tt(34)}px;height:${tt(34)}px;object-fit:contain;flex:none`);
      glyph.src = "/assets/twitch.png";
      hl.append(glyph, vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${tt(29)}px;letter-spacing:.5px;color:${S.text}`, handle));
      wrap.append(hl);
    }
  }
  stage.appendChild(wrap);
  vceClipEl = wrap;
  if (portImg) portImg.style.animation = animate
    ? "vceClipPop .62s cubic-bezier(.34,1.56,.64,1) both, vceIconIdle 2.8s ease-in-out .72s infinite"
    : "vceIconIdle 2.8s ease-in-out infinite";
  if (animate) {
    box.style.transition = "none"; box.style.opacity = "0"; box.style.transform = "translateY(28px)";
    requestAnimationFrame(() => { box.style.transition = "opacity .5s ease, transform .5s cubic-bezier(.2,.8,.2,1)"; box.style.opacity = "1"; box.style.transform = "none"; });
  }
}

// ---- icon backdrop -----------------------------------------------------------
const vceTintCache = {};
function vceTintIcon(color) {
  if (!vceIconImg || !vceIconImg.complete || !vceIconImg.naturalWidth) return null;
  if (vceTintCache[color]) return vceTintCache[color];
  const w = vceIconImg.naturalWidth, h = vceIconImg.naturalHeight;
  const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
  const ctx = cv.getContext("2d");
  ctx.drawImage(vceIconImg, 0, 0);
  ctx.globalCompositeOperation = "source-in";
  ctx.fillStyle = color; ctx.fillRect(0, 0, w, h);
  return (vceTintCache[color] = cv.toDataURL("image/png"));
}
function vceMountIcon() {
  if (!vceParts || !vceParts.listEl) return;
  // Sit the backdrop BEHIND the list items: centre it on the list's own box
  // (design mountIcon — 50%/50% of listEl), so it moves + scales with the list
  // and hugs the ranked rows rather than drifting to the frame centre. The
  // render mirrors this in formatter._list_content_box.
  const el = document.createElement("div");
  el.style.cssText = "position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);pointer-events:none";
  vceParts.listEl.insertBefore(el, vceParts.listEl.firstChild);
  vceIconEl = el;
  vceApplyIcon();
}
function vceApplyIcon() {
  const el = vceIconEl, S = AL.vce.icon;
  if (!el) return;
  el.style.display = S.on ? "block" : "none";
  if (!S.on) return;
  el.style.width = S.size + "px";
  el.style.height = S.size + "px";
  el.style.opacity = S.opacity / 100;
  el.style.backgroundRepeat = "no-repeat";
  el.style.backgroundPosition = "center";
  el.style.backgroundSize = "contain";
  const url = vceTintIcon(S.color);
  if (url) el.style.backgroundImage = `url('${url}')`;
}

// ---- valorant subtitle --------------------------------------------------------
// "VALORANT" + red mark under the title; "Streamers React" pulls in every
// streamer pinned across the ranks. Mirrors formatter._subtitle_sprite.
function vceBuildSubtitle() {
  const v = AL.vce, sub = v.subtitle;
  if (!sub.on || !vceParts || !vceParts.titleWrap) return;
  const txt = (sub.text || "").trim(); if (!txt) return;
  const z = sub.size || 0.70;
  const line = vceMk("div", `position:relative;z-index:3;display:flex;align-items:center;flex-wrap:wrap;gap:${Math.round(18 * z)}px;margin-top:24px;transform-origin:center;` +
    (v.style === "editorial" ? "justify-content:flex-start" : "justify-content:center"));
  const word = vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${Math.round(48 * z)}px;letter-spacing:${(6 * z).toFixed(1)}px;text-transform:uppercase;color:#fff;text-shadow:0 3px 18px rgba(0,0,0,.55)`, txt);
  const glyph = vceMk("img", `height:${Math.round(46 * z)}px;width:auto;object-fit:contain;flex:none`);
  glyph.src = "/assets/valorant.png";
  line.append(word, glyph);
  const wordStyle = `font-family:'Poppins',sans-serif;font-weight:800;font-size:${Math.round(48 * z)}px;letter-spacing:${(6 * z).toFixed(1)}px;text-transform:uppercase;color:#fff;text-shadow:0 3px 18px rgba(0,0,0,.55)`;
  if ((sub.after || "").trim()) line.append(vceMk("span", wordStyle, sub.after));
  if (sub.streamers) {
    const keys = vceStreamerKeys();
    if (keys.length) {
      const bar = () => vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${Math.round(48 * z)}px;color:rgba(255,255,255,.4);margin:0 ${Math.round(2 * z)}px`, "|");
      const react = vceMk("span", `font-family:'Poppins',sans-serif;font-weight:800;font-size:${Math.round(48 * z)}px;letter-spacing:${(3 * z).toFixed(1)}px;text-transform:uppercase;color:#fff;text-shadow:0 3px 18px rgba(0,0,0,.55)`, "Streamers React");
      line.append(bar(), react, bar());
      keys.forEach(kk => {
        const smm = vceStreamerByKey(kk); if (!smm) return;
        const s = Math.round(105 * z);
        const av = vceMk("img", `width:${s}px;height:${s}px;border-radius:50%;object-fit:cover;object-position:center top;background:#0f141d;border:${Math.max(2, Math.round(3 * z))}px solid #fff;box-shadow:0 3px 12px rgba(0,0,0,.5);flex:none`);
        av.src = smm.src; line.append(av);
      });
    }
  }
  vceParts.titleWrap.appendChild(line);
  vceParts.subtitleLine = line;
}

// Combined SFX list (assets/sfx) — shared by the SFX B-roll + bonus entrance.
function combinedSfx() {
  const sfxFx = (AL.fxData.effects || []).find(e => e.key === "sfx");
  return (sfxFx && sfxFx.sounds) || [];
}
function fillBonusSounds() {
  const sel = $("#vce-bonus-sound");
  if (!sel) return;
  const cur = AL.vce.bonus.sound || "none";
  const opts = [`<option value="none"${cur === "none" ? " selected" : ""}>None</option>`];
  combinedSfx().forEach(s => opts.push(`<option value="${esc(s.key)}"${s.key === cur ? " selected" : ""}>${esc(s.label)}</option>`));
  sel.innerHTML = opts.join("");
  sel.value = cur;
}
function auditionCombinedSfx(key, volume) {
  if (!key || key === "none") return;
  const s = combinedSfx().find(x => x.key === key);
  if (s && s.preview) { const a = new Audio(s.preview); a.volume = Math.max(0, Math.min(1, volume ?? 1)); a.play().catch(() => {}); }
}

// ---- bonus title --------------------------------------------------------------
function vceBuildBonus() {
  const v = AL.vce;
  if (!v.bonus.on || !vceParts || !vceParts.titleWrap) return;
  const txt = (v.bonus.text || "").trim(); if (!txt) return;
  const el = document.createElement("div");
  el.style.transformOrigin = "center";
  if (v.bonus.style === "sticker") {
    el.style.cssText += "display:inline-block;font-family:'Poppins',sans-serif;font-weight:800;font-size:46px;color:#04121f;background:#f5b942;padding:14px 30px;border-radius:16px;transform:rotate(-4deg);box-shadow:0 8px 26px rgba(0,0,0,.4);letter-spacing:.5px";
    el.textContent = txt;
  } else if (v.bonus.style === "marker") {
    el.style.cssText += "display:inline-block;font-family:'Anton',sans-serif;font-size:52px;color:#fff;text-transform:uppercase;letter-spacing:2px;padding:6px 4px;background:linear-gradient(transparent 62%, rgba(255,70,85,.85) 62%);text-shadow:0 2px 14px rgba(0,0,0,.5)";
    el.textContent = txt;
  } else if (v.bonus.style === "tag") {
    el.style.cssText += "display:inline-flex;align-items:center;gap:12px;font-family:'BebasNeue',sans-serif;font-size:46px;color:#fff;text-transform:uppercase;letter-spacing:3px;background:rgba(0,0,0,.5);border:2px solid rgba(255,255,255,.7);padding:8px 24px;border-radius:999px";
    el.textContent = "► " + txt;
  } else {   // spark
    el.style.cssText += "position:relative;display:inline-block;font-family:'MontserratBlack','Arial Black',sans-serif;color:#fff;-webkit-text-stroke:9px #16233f;paint-order:stroke fill;font-size:50px;line-height:1.12;letter-spacing:.5px;padding:6px 34px";
    const label = document.createElement("span");
    label.style.cssText = "position:relative;z-index:1";
    label.textContent = txt;
    el.appendChild(label);
    const spk = "position:absolute;-webkit-text-stroke:0;color:#ffe27a;filter:drop-shadow(0 0 8px rgba(255,222,120,.85));pointer-events:none;animation:vceSparkleFloat 2.2s ease-in-out infinite";
    [
      { t: "-26px", l: "-6px", r: "", s: 44, d: "0s" },
      { t: "-12px", l: "", r: "-2px", s: 30, d: ".7s" },
      { t: "34px", l: "18px", r: "", s: 26, d: "1.3s" },
    ].forEach(p => {
      const s = document.createElement("span");
      s.textContent = "✦";
      s.style.cssText = spk + `;font-size:${p.s}px;top:${p.t};animation-delay:${p.d};` + (p.r ? `right:${p.r}` : `left:${p.l}`);
      el.appendChild(s);
    });
  }
  // Size multiplier — scale the built call-out (the reveal animation drives the
  // wrapper `line`, so scaling `el` here won't fight it; mirrors
  // formatter._bonus_sprite folding the size into its `u`). Anchor the scale to
  // the edge nearest the title so growing it expands *away* from the title, like
  // the render (which pins the bonus a fixed gap off the title edge).
  const bz = v.bonus.size ?? 1;
  const above = v.bonus.pos === "above";
  el.style.transformOrigin = above ? "center bottom" : "center top";
  el.style.transform = (v.bonus.style === "sticker" ? "rotate(-4deg) " : "") + `scale(${bz})`;
  // Float the bonus above/below the title WITHOUT reflowing it — the render
  // (formatter._bonus_position) keeps the title anchored at its y and places the
  // bonus a fixed gap off its edge. Position absolutely against the title box and
  // centre via text-align (not transform, which the reveal animation overwrites).
  const gap = Math.round(26 * bz);
  const line = document.createElement("div");
  line.style.cssText = "position:absolute;left:0;right:0;z-index:3;white-space:nowrap;transform-origin:center;" +
    (v.style === "editorial" ? "text-align:left;" : "text-align:center;") +
    (above ? "bottom:100%;margin-bottom:" + gap + "px;" : "top:100%;margin-top:" + gap + "px;");
  line.appendChild(el);
  vceParts.titleUnit.appendChild(line);
  vceParts.bonusEl = el;
  vceParts.bonusLine = line;
}

// ---- stage scaling / bg -------------------------------------------------------
// iPhone screen height in the stage's 1080-wide units. The clip is shown
// contain-by-width (fills width, top-aligned), so a 9:16 clip occupies the top
// 1920 and the phone's bottom chrome extends to VPH_SCREEN_H. Traced from a real
// iPhone capture (1170×2532): 2532 × (1080/1170) ≈ 2337.
const VPH_SCREEN_H = 2337;
function vceFit() {
  const holder = $("#vce-holder"), stage = $("#vce-stage"), box = $("#vce-box");
  if (!holder || !stage || !box) return;
  const w = holder.getBoundingClientRect().width;
  if (w < 60) return;                          // panel hidden — refit on tab open
  const phoneOn = !!(AL.vce && AL.vce.phone);
  // in phone mode the mockup is taller (2337 vs 1920) — cap scale so it still fits
  const cap = phoneOn ? 0.44 : 0.52;
  const scale = Math.min((w - 16) / 1080, cap);
  vceScale = scale;
  stage.style.transform = `scale(${scale})`;
  const phone = $("#vce-phone");
  if (phone) phone.style.transform = `scale(${scale})`;
  box.style.width = (1080 * scale) + "px";
  box.style.height = ((phoneOn ? VPH_SCREEN_H : 1920) * scale) + "px";
}

// Toggle the iPhone mockup on/off (builds it lazily the first time).
function vceSetPhone(on) {
  const phone = $("#vce-phone"), box = $("#vce-box"), holder = $("#vce-holder");
  if (!phone) return;
  if (on && !phone.dataset.built) { vceBuildPhone(phone); phone.dataset.built = "1"; }
  phone.classList.toggle("hidden", !on);
  if (box) box.classList.toggle("phone-on", on);
  if (holder) holder.classList.toggle("phone-on", on);
  vceFit();
}

// Build the iPhone (Dynamic Island) + YouTube Shorts UI overlay. All coordinates
// are in the stage's 1080-wide clip units (the layer is scaled by vceFit). The
// clip is shown contain-by-width: the 9:16 video fills 0..1920, the phone's
// bottom chrome runs 1920..VPH_SCREEN_H. Element positions are traced from a real
// iPhone capture so text placed clear of these lands clear on-device.
function vceBuildPhone(host) {
  const S = {                                   // compact inline SVGs (white)
    back: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M15 4l-8 8 8 8" fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    search: `<svg viewBox="0 0 24 24" width="100%" height="100%"><circle cx="10.5" cy="10.5" r="7" fill="none" stroke="#fff" stroke-width="2.1"/><path d="M20 20l-4.5-4.5" stroke="#fff" stroke-width="2.3" stroke-linecap="round"/></svg>`,
    menu: `<svg viewBox="0 0 24 24" width="100%" height="100%"><circle cx="12" cy="4.5" r="1.9" fill="#fff"/><circle cx="12" cy="12" r="1.9" fill="#fff"/><circle cx="12" cy="19.5" r="1.9" fill="#fff"/></svg>`,
    like: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M2 9.5h3.4V21H2zM7 21V9.8l4.1-6.6c1.2 0 2.1.9 2.1 2.1v3.4h5.4c1.3 0 2.2 1.1 1.9 2.3l-1.5 6.6c-.2 1-1.1 1.4-2 1.4z" fill="#fff"/></svg>`,
    comment: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M3 5.5C3 4.7 3.7 4 4.5 4h15c.8 0 1.5.7 1.5 1.5v9c0 .8-.7 1.5-1.5 1.5H9l-4.5 4v-4H4.5C3.7 16 3 15.3 3 14.5z" fill="#fff"/></svg>`,
    share: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M13 3l8 7-8 7v-3.6C7.5 13.4 4.6 15 2.5 18 3 12.5 6.5 9.2 13 8.6z" fill="#fff"/></svg>`,
    remix: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M4 8a6 6 0 0 1 10-2m2-2v4h-4M20 16a6 6 0 0 1-10 2m-2 2v-4h4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    home: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M4 11l8-6 8 6v8h-5v-5H9v5H4z" fill="none" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/></svg>`,
    shorts: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M9 3.8l7 3.6c2.3 1.2 2.3 4 0 5.1l-1.4.7 1.4.7c2.3 1.1 2.3 4 0 5.1l-7 3.6c-2.6 1.3-5.5-.6-5.5-3.4V7.2C3.5 4.4 6.4 2.5 9 3.8z" fill="none" stroke="#fff" stroke-width="1.6"/><path d="M10 9.5l4.5 2.5-4.5 2.5z" fill="#fff"/></svg>`,
    plus: `<svg viewBox="0 0 24 24" width="100%" height="100%"><rect x="2.5" y="4.5" width="19" height="15" rx="4" fill="none" stroke="#fff" stroke-width="1.7"/><path d="M12 8.5v7M8.5 12h7" stroke="#fff" stroke-width="1.9" stroke-linecap="round"/></svg>`,
    subs: `<svg viewBox="0 0 24 24" width="100%" height="100%"><path d="M4 8h16M6 4h12M4 12h16v7H4z" fill="none" stroke="#fff" stroke-width="1.7" stroke-linejoin="round"/><path d="M11 14.5l3 1.8-3 1.8z" fill="#fff"/></svg>`,
  };
  const railItem = (icon, label) =>
    `<div class="vph-rail-item"><div class="vph-rail-ic">${icon}</div><div class="vph-rail-lb">${label}</div></div>`;
  const tab = (icon, label, active, dot) =>
    `<div class="vph-tab${active ? " on" : ""}"><div class="vph-tab-ic">${icon}${dot ? '<i class="vph-tab-dot"></i>' : ""}</div><div class="vph-tab-lb">${label}</div></div>`;

  host.innerHTML = `
    <div class="vph-scrim-top"></div>
    <div class="vph-scrim-bottom"></div>

    <!-- status bar + Dynamic Island -->
    <div class="vph-island"></div>
    <div class="vph-time">9:41</div>
    <div class="vph-status">
      <svg viewBox="0 0 22 16" width="34" height="24"><rect x="0" y="9" width="3.5" height="6" rx="1" fill="#fff"/><rect x="5" y="6" width="3.5" height="9" rx="1" fill="#fff"/><rect x="10" y="3" width="3.5" height="12" rx="1" fill="#fff"/><rect x="15" y="0" width="3.5" height="15" rx="1" fill="#fff" opacity=".4"/></svg>
      <svg viewBox="0 0 22 16" width="32" height="24"><path d="M11 4c3 0 5.6 1.2 7.5 3M11 4C8 4 5.4 5.2 3.5 7M11 9c1.4 0 2.7.6 3.7 1.5M11 9c-1.4 0-2.7.6-3.7 1.5" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/><circle cx="11" cy="13.5" r="1.4" fill="#fff"/></svg>
      <div class="vph-batt"><span></span></div>
    </div>

    <!-- top controls -->
    <div class="vph-top-ic" style="left:44px">${S.back}</div>
    <div class="vph-top-ic" style="left:840px">${S.search}</div>
    <div class="vph-top-ic" style="left:978px">${S.menu}</div>

    <!-- right action rail -->
    <div class="vph-rail">
      ${railItem(S.like, "12K")}
      ${railItem(S.comment, "340")}
      ${railItem(S.share, "Share")}
      ${railItem(S.remix, "Remix")}
    </div>

    <!-- bottom-left channel + caption -->
    <div class="vph-meta">
      <div class="vph-chrow">
        <div class="vph-avatar">▶</div>
        <div class="vph-handle">@yourchannel</div>
        <div class="vph-sub">Subscribe</div>
      </div>
      <div class="vph-caption">Your caption goes here 🔥 #valorant #shorts #tenz</div>
      <div class="vph-sound">🎵 original sound · yourchannel</div>
    </div>

    <!-- video scrubber (bottom of the video) -->
    <div class="vph-scrub"><i></i></div>

    <!-- bottom chrome: opaque area below the 9:16 video -->
    <div class="vph-below">
      <div class="vph-tabbar">
        ${tab(S.home, "Home", false)}
        ${tab(S.shorts, "Shorts", true)}
        ${tab(S.plus, "", false)}
        ${tab(S.subs, "Subscriptions", false, true)}
        ${tab('<div class="vph-you"></div>', "You", false)}
      </div>
      <div class="vph-home-ind"></div>
    </div>

    <!-- phone bezel: rounded black edge drawn over the square video corners -->
    <div class="vph-bezel"></div>`;
}
let stageBgTimer = null;
function refreshStageBg(force) {
  const bg = $("#vce-bg"), scrim = $("#vce-scrim"), stage = $("#vce-stage");
  if (!bg) return;
  const first = AL.fmt.ranks.find(r => r.clip);
  const on = AL.vce.overClip && first;
  bg.style.display = on ? "block" : "none";
  scrim.style.display = on ? "block" : "none";
  stage.style.background = on ? "#000" : "linear-gradient(#1e2430,#0b0d12)";
  if (!on) return;
  const key = first.clip + "|" + JSON.stringify(first.layout || {});
  if (!force && bg.dataset.key === key) return;
  clearTimeout(stageBgTimer);
  stageBgTimer = setTimeout(async () => {
    try {
      const res = await api("/api/formatter/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          template: "valorant_clip_ranking", bare: true, items: [],
          clip: first.clip, layout: first.layout || { clip: first.clip },
        }),
      });
      bg.src = res.preview + "?t=" + Date.now();
      bg.dataset.key = key;
    } catch { /* stage bg is best-effort */ }
  }, 250);
}

// ---- template builders (per style) — port of the design's buildPill/… ---------
function vceRebuild() {
  const host = $("#vce-host"); if (!host) return;
  host.innerHTML = "";
  vceIconEl = null;
  const b = { pill: vceBuildPill, minimal: vceBuildMinimal, editorial: vceBuildEditorial }[AL.vce.style] || vceBuildPill;
  vceParts = b(host);
  vceBuildBonus();
  vceBuildSubtitle();
  vceMountIcon();
  vceApplyRowWidgets();
  vceApplyOffsets();
  vceWireDrag();
  vceRenderClipBox(vceActiveClipIdx != null ? vceActiveClipIdx : (vceActiveClipIdx = vceFirstClipIdx()));
}

// #1 / #2 / #3 → gold / silver / bronze (pill style only, when Medal ranks is on)
const VCE_MEDAL_COLORS = ["#FFD24A", "#8A97A8", "#D08A4E"];
function vceBuildPill(host) {
  const STROKE = "#16233f", accent = vceAccent(), accentSet = vceAccentSet();
  const wStyle = (i, col) => `display:inline-block;transform-origin:center;font-family:'MontserratBlack','Arial Black',sans-serif;color:${col};-webkit-text-stroke:16px ${STROKE};paint-order:stroke fill;font-size:75px;line-height:1.12;cursor:pointer${i ? ";margin-left:.28em" : ""}`;
  const rStyle = extra => `display:inline-block;transform-origin:center;font-family:'MontserratBlack','Arial Black',sans-serif;color:#fff;-webkit-text-stroke:12px ${STROKE};paint-order:stroke fill;font-size:56px;line-height:1${extra || ""}`;
  const wrap = vceMk("div", "position:absolute;top:576px;left:0;right:0;text-align:center;transform-origin:center top;display:flex;flex-direction:column;align-items:center");
  const titleUnit = vceMk("div", "position:relative;display:inline-block;transform-origin:center");
  const pill = vceMk("div", `position:absolute;inset:0;border-radius:999px;background:${vceHexToRgba(AL.vce.pillFill || "#a9c9ec", 0.922)};transform-origin:center`);
  const words = vceMk("div", "position:relative;z-index:1;padding:26px 37px;white-space:normal;max-width:920px;text-align:center");
  const titleParts = (AL.vce.title || "").split(/\s+/).filter(Boolean).map((w, i) => { const s = vceMk("span", wStyle(i, accentSet.has(w.toLowerCase()) ? accent : "#fff"), w); s.dataset.word = w; words.appendChild(s); return s; });
  titleUnit.append(pill, words); wrap.append(titleUnit); host.append(wrap);
  const list = vceMk("div", "position:absolute;left:54px;top:763px;transform-origin:left top");
  const rows = vceLabels().map((label, i) => {
    const row = vceMk("div", "position:relative;height:115px;white-space:nowrap;transform-origin:center");
    const num = vceMk("span", rStyle(), (i + 1) + ".");
    if (AL.vce.medalsOn && i < 3) num.style.color = VCE_MEDAL_COLORS[i];
    const lab = vceMk("span", rStyle(";margin-left:.30em"), label);
    if (AL.vce.medalText && i < 3) lab.style.color = VCE_MEDAL_COLORS[i];
    row.append(num, lab); list.append(row); return { unit: row, num, label: lab, hasLabel: !!label.trim() };
  });
  host.append(list);
  return { titleUnit, titleParts, pill, pillWords: words, rows, titleWrap: wrap, listEl: list };
}

function vceBuildMinimal(host) {
  const accent = vceAccent(), accentSet = vceAccentSet();
  const shadow = "text-shadow:0 3px 22px rgba(0,0,0,.55)";
  const wrap = vceMk("div", "position:absolute;top:300px;left:0;right:0;text-align:center;transform-origin:center top;display:flex;flex-direction:column;align-items:center");
  const titleUnit = vceMk("div", "position:relative;display:inline-block;transform-origin:center;white-space:normal;max-width:920px");
  const titleParts = (AL.vce.title || "").split(/\s+/).filter(Boolean).map((w, i) => {
    const col = accentSet.has(w.toLowerCase()) ? accent : "#fff";
    const s = vceMk("span", `display:inline-block;transform-origin:center;font-family:'Poppins',sans-serif;font-weight:800;color:${col};font-size:82px;line-height:1.08;text-transform:uppercase;letter-spacing:1px;cursor:pointer;${shadow}${i ? ";margin-left:.32em" : ""}`, w);
    s.dataset.word = w; titleUnit.appendChild(s); return s;
  });
  wrap.append(titleUnit); host.append(wrap);
  const list = vceMk("div", "position:absolute;left:96px;right:96px;top:620px;transform-origin:left top");
  const rows = vceLabels().map((label, i) => {
    const row = vceMk("div", "position:relative;display:flex;align-items:baseline;height:150px;transform-origin:left center");
    const num = vceMk("span", `display:inline-block;transform-origin:center;font-family:'Poppins',sans-serif;font-weight:900;color:${accent};font-size:62px;line-height:1;min-width:96px`, (i + 1) + ".");
    const lab = vceMk("span", `display:inline-block;transform-origin:left center;font-family:'Poppins',sans-serif;font-weight:800;color:#fff;font-size:58px;line-height:1;margin-left:12px;${shadow}`, label);
    row.append(num, lab); list.append(row); return { unit: row, num, label: lab, hasLabel: !!label.trim() };
  });
  host.append(list);
  return { titleUnit, titleParts, pill: null, rows, titleWrap: wrap, listEl: list };
}

function vceBuildEditorial(host) {
  const accent = vceAccent(), accentSet = vceAccentSet();
  const shadow = "text-shadow:0 3px 22px rgba(0,0,0,.55)";
  const wrap = vceMk("div", "position:absolute;top:270px;left:80px;right:80px;transform-origin:left top;display:flex;flex-direction:column;align-items:flex-start");
  const titleUnit = vceMk("div", "position:relative;display:inline-block;transform-origin:left center;white-space:normal");
  const titleParts = (AL.vce.title || "").split(/\s+/).filter(Boolean).map((w, i) => {
    const col = accentSet.has(w.toLowerCase()) ? accent : "#fff";
    const s = vceMk("span", `display:inline-block;transform-origin:center;font-family:'Anton',sans-serif;color:${col};font-size:70px;line-height:1.06;text-transform:uppercase;letter-spacing:2px;cursor:pointer;${shadow}${i ? ";margin-left:.30em" : ""}`, w);
    s.dataset.word = w; titleUnit.appendChild(s); return s;
  });
  wrap.append(titleUnit); host.append(wrap);
  const list = vceMk("div", "position:absolute;left:80px;right:80px;top:600px;transform-origin:left top");
  const rows = vceLabels().map((label, i) => {
    const row = vceMk("div", "position:relative;display:flex;align-items:center;height:210px;transform-origin:left center");
    const num = vceMk("span", `display:inline-block;transform-origin:center;font-family:'Anton',sans-serif;color:${accent};font-size:150px;line-height:.8;min-width:150px`, String(i + 1));
    const lab = vceMk("span", `display:inline-block;transform-origin:left center;font-family:'BebasNeue',sans-serif;color:#fff;font-size:74px;line-height:1;letter-spacing:1px;text-transform:uppercase;margin-left:28px;${shadow}`, label);
    row.append(num, lab); list.append(row); return { unit: row, num, label: lab, hasLabel: !!label.trim() };
  });
  host.append(list);
  return { titleUnit, titleParts, pill: null, rows, titleWrap: wrap, listEl: list };
}

function vceApplyRowWidgets() {
  if (!vceParts) return;
  const base = { pill: 92, minimal: 84, editorial: 116 }[AL.vce.style] || 84;
  vceParts.rows.forEach((r, i) => {
    const w = AL.vce.widgets[i]; if (!w || !r.hasLabel) return;
    const h = w.kind === "weapon" ? Math.round(base * 0.6) : base;
    const img = document.createElement("img");
    img.src = w.src;
    img.style.cssText = `display:inline-block;height:${h}px;width:auto;max-width:340px;vertical-align:middle;margin-left:.4em;filter:drop-shadow(0 3px 12px rgba(0,0,0,.55));animation:vceIconIdle 2.6s ease-in-out infinite;animation-delay:${(i * 0.25).toFixed(2)}s;transform-origin:center`;
    r.label.appendChild(img);
  });
}

// ---- drag / resize on the canvas ---------------------------------------------
function vceApplyOffsets() {
  if (!vceParts) return;
  const o = vceOff();
  const single = (AL.vce.titleLines === 1);
  const textBox = vceParts.pillWords || vceParts.titleUnit;   // element that owns the wrapping
  let titleScale = o.ts || 1;
  if (single && textBox) {
    // Force one line and auto-shrink the block to fit the title's on-screen width
    // budget — mirrors formatter._fit_title(max_lines=1), which shrinks the font
    // until the single line fits (ts only sets the starting size).
    textBox.style.whiteSpace = "nowrap";
    textBox.style.maxWidth = "none";
    const cs = getComputedStyle(textBox);
    const pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    const naturalW = Math.max(1, textBox.scrollWidth - pad);   // text width at the base font
    const budget = vceParts.pill ? 918 : 983;                  // backend base_max_w (pill vs plain)
    titleScale = Math.min(o.ts || 1, budget / naturalW);
  } else if (vceParts.pillWords) {
    // Pill wraps by rendered width: shrinking the title widens its text allowance
    // so a longer title stays on fewer lines (mirrors formatter._fit_title).
    vceParts.pillWords.style.whiteSpace = "normal";
    vceParts.pillWords.style.maxWidth = Math.round(920 / (o.ts || 1)) + "px";
  }
  if (vceParts.titleWrap) vceParts.titleWrap.style.transform = `translate(${o.tx}px, ${o.ty}px) scale(${titleScale})`;
  if (vceParts.listEl) vceParts.listEl.style.transform = `translate(${o.lx}px, ${o.ly}px) scale(${o.ls})`;
}
function vceWireDrag() {
  if (!vceParts) return;
  vceHandles = [];
  vceAddMove(vceParts.titleUnit, "title", t => { if (t && t.dataset && t.dataset.word) vceToggleAccentWord(t.dataset.word); });
  vceAddMove(vceParts.listEl, "list");
  vceAddHandle(vceParts.titleUnit, "title"); vceAddHandle(vceParts.listEl, "list");
  vceSetResizeVisible(AL.vce.resize);
}
function vceSetResizeVisible(on) {
  vceHandles.forEach(h => { h.style.display = on ? "block" : "none"; });
}
function vceAddMove(el, kind, onTap) {
  if (!el) return;
  el.style.cursor = "move"; el.style.touchAction = "none";
  el.addEventListener("pointerdown", e => {
    if (e.target && e.target.dataset && e.target.dataset.handle) return;
    e.preventDefault();
    const o = vceOff();
    const xK = kind === "title" ? "tx" : "lx", yK = kind === "title" ? "ty" : "ly";
    const sx = e.clientX, sy = e.clientY, ox = o[xK], oy = o[yK], sc = vceScale || 1;
    const downTarget = e.target;
    let moved = false;
    try { el.setPointerCapture(e.pointerId); } catch (x) {}
    const mv = ev => {
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 4) return;   // tap vs drag
      moved = true;
      o[xK] = ox + (ev.clientX - sx) / sc; o[yK] = oy + (ev.clientY - sy) / sc; vceApplyOffsets(); vceSyncPosControls();
    };
    const up = () => {
      el.removeEventListener("pointermove", mv); el.removeEventListener("pointerup", up);
      if (!moved && onTap) onTap(downTarget);
    };
    el.addEventListener("pointermove", mv); el.addEventListener("pointerup", up);
  });
}
function vceAddHandle(el, kind) {
  if (!el) return;
  const h = document.createElement("div");
  h.dataset.handle = kind;
  h.style.cssText = "position:absolute;width:38px;height:38px;right:-19px;bottom:-19px;border-radius:50%;background:#2ea6ff;border:3px solid #04121f;box-shadow:0 2px 10px rgba(0,0,0,.55);cursor:nwse-resize;z-index:30;touch-action:none";
  el.appendChild(h);
  vceHandles.push(h);
  h.addEventListener("pointerdown", e => {
    e.preventDefault(); e.stopPropagation();
    const o = vceOff();
    const sK = kind === "title" ? "ts" : "ls";
    const rect = el.getBoundingClientRect();
    const center = kind === "title" && AL.vce.style !== "editorial";
    const ax = center ? rect.left + rect.width / 2 : rect.left, ay = rect.top;
    const sd = Math.hypot(e.clientX - ax, e.clientY - ay) || 1, ss = o[sK];
    try { h.setPointerCapture(e.pointerId); } catch (x) {}
    const mv = ev => {
      let val = ss * Math.hypot(ev.clientX - ax, ev.clientY - ay) / sd;
      val = Math.max(0.4, Math.min(2.5, val));
      o[sK] = Math.round(val * 100) / 100;
      vceApplyOffsets(); vceSyncPosControls();
    };
    const up = () => { h.removeEventListener("pointermove", mv); h.removeEventListener("pointerup", up); };
    h.addEventListener("pointermove", mv); h.addEventListener("pointerup", up);
  });
}

// ---- reveal playback (canvas) --------------------------------------------------
function vcePlan() {
  const { mode, target } = AL.vce;
  const motion = vceCfg().motion;              // Motion is per reveal phase
  const P = vceParts;
  const tweens = [], statics = [], hidden = [];
  if (mode === "new_item") {
    statics.push(P.titleUnit);
  } else if (motion === "stagger") {
    if (P.pill) tweens.push({ el: P.pill, start: 0, motion: "pop" });
    const base = P.pill ? 1 : 0;
    P.titleParts.forEach((w, i) => tweens.push({ el: w, start: (i + base) * VCE_T.PART_STEP, motion: "pop" }));
  } else {
    tweens.push({ el: P.titleUnit, start: 0, motion });
  }
  // The VALORANT subtitle pops in with the title (a hair behind it), matching the
  // backend (formatter.animation_layers gives it the title's reveal, start 0.10).
  if (P.subtitleLine) {
    if (mode === "new_item") statics.push(P.subtitleLine);
    else tweens.push({ el: P.subtitleLine, start: VCE_T.SUB_LEAD, motion: motion === "stagger" ? "pop" : motion });
  }
  P.rows.forEach((r, i) => {
    const start = VCE_T.TITLE_TO_ROWS + i * VCE_T.ROW_STEP;
    if (mode === "new_item") {
      statics.push(r.num);
      if (i === target && r.hasLabel) tweens.push({ el: r.label, start: VCE_T.NEW_ITEM_LEAD, motion });
      else if (i === target) statics.push(r.label);
      else hidden.push(r.label);
    } else if (motion === "stagger") {
      tweens.push({ el: r.num, start, motion: "pop" });
      if (r.hasLabel) tweens.push({ el: r.label, start: start + VCE_T.PART_STEP, motion: "pop" });
    } else {
      tweens.push({ el: r.unit, start, motion });
    }
  });
  const minStart = tweens.reduce((m, t) => Math.min(m, t.start), Infinity);
  return { tweens, statics, hidden, minStart: isFinite(minStart) ? minStart : 0 };
}

function vcePlay() {
  if (!vceParts) return;
  cancelAnimationFrame(vceRaf); clearTimeout(vceSfxTimer); clearTimeout(vceHideTimer); clearTimeout(vceBonusSfxTimer);
  vceClipTimers.forEach(clearTimeout); vceClipTimers = [];
  const v = AL.vce, p = vcePlan(), P = vceParts, cfg = vceCfg();

  // per-item clip cues — swap the corner card as each ranked item reveals
  const cues = [];
  AL.vce.clips.forEach((c, i) => {
    if (!vceClipHasContent(c)) return;
    if (v.mode === "new_item") { if (i === v.target) cues.push({ i, t: VCE_T.NEW_ITEM_LEAD }); }
    else cues.push({ i, t: VCE_T.TITLE_TO_ROWS + i * VCE_T.ROW_STEP });
  });
  vceRenderClipBox(null);
  cues.sort((a, b) => a.t - b.t).forEach(cue => {
    vceClipTimers.push(setTimeout(() => { vceActiveClipIdx = cue.i; vceRenderClipBox(cue.i, true); }, (cue.t / cfg.speed) * 1000));
  });

  const bonusLine = (v.bonus.on && P.bonusLine) ? P.bonusLine : null;
  // Delayed entrance is intro-only (matches formatter.animation_layers): the
  // bonus enters `delay` real-seconds in, on its own speed, with its own SFX.
  const bonusDelayed = bonusLine && v.bonus.delayOn && v.mode !== "new_item";
  if (bonusLine) {
    bonusLine.style.transition = "";
    if (bonusDelayed) p.tweens.push({ el: bonusLine, realStart: Math.max(0, v.bonus.delay ?? 1), speed: Math.max(0.25, v.bonus.speed ?? 1), motion: "pop" });
    else p.tweens.push({ el: bonusLine, start: VCE_T.TITLE_TO_ROWS * 0.8, motion: "pop" });
  }

  const all = [P.titleUnit, P.pill, bonusLine, P.subtitleLine, ...P.titleParts, ...P.rows.flatMap(r => [r.unit, r.num, r.label])].filter(Boolean);
  all.forEach(el => { el.style.opacity = ""; el.style.transform = ""; });
  p.statics.forEach(vceShowEl);
  p.hidden.forEach(vceHideEl);
  p.tweens.forEach(t => vceHideEl(t.el));

  // end of motion in real seconds (delayed tweens run on their own clock)
  const maxEndReal = p.tweens.reduce((m, t) => Math.max(m,
    t.realStart != null ? t.realStart + VCE_T.ANIM / (t.speed || 1) : (t.start + VCE_T.ANIM) / cfg.speed), 0);

  if (bonusLine && v.bonus.hideOn) {
    const hideDelay = (bonusDelayed ? Math.max(0, v.bonus.delay ?? 1) : 0) + Math.max(0.3, v.bonus.hideSecs);
    vceHideTimer = setTimeout(() => {
      bonusLine.style.transition = "opacity .5s ease, transform .5s ease";
      bonusLine.style.opacity = "0";
      bonusLine.style.transform = "translateY(-22px) scale(.9)";
    }, hideDelay * 1000);
  }

  if (cfg.sound && cfg.sound !== "none") {
    const s = (AL.fxData.sounds || []).find(x => x.key === cfg.sound);
    if (s && s.preview) {
      vceSfxTimer = setTimeout(() => {
        const a = new Audio(s.preview);
        a.volume = Math.max(0, Math.min(1, cfg.volume));
        a.play().catch(() => {});
      }, (p.minStart / cfg.speed) * 1000);
    }
  }
  // delayed bonus entrance SFX (its own one-shot, at the entrance moment)
  if (bonusDelayed && v.bonus.sound && v.bonus.sound !== "none") {
    vceBonusSfxTimer = setTimeout(() => auditionCombinedSfx(v.bonus.sound, v.bonus.volume), Math.max(0, v.bonus.delay ?? 1) * 1000);
  }

  const btn = $("#vce-play"); if (btn) btn.disabled = true;
  let t0 = null;
  const frame = ts => {
    if (t0 === null) t0 = ts;
    const elapsed = (ts - t0) / 1000;
    const animT = elapsed * cfg.speed;
    p.tweens.forEach(t => {
      const prog = t.realStart != null
        ? (elapsed - t.realStart) * (t.speed || 1) / VCE_T.ANIM
        : (animT - t.start) / VCE_T.ANIM;
      const { s, dy, a } = vceXform(t.motion, prog);
      vceSetEl(t.el, s, dy, a);
    });
    if (elapsed < maxEndReal) { vceRaf = requestAnimationFrame(frame); }
    else { p.tweens.forEach(t => vceShowEl(t.el)); if (btn) btn.disabled = false; }
  };
  vceRaf = requestAnimationFrame(frame);
}

// ---- server-rendered mp4 preview of the reveal ---------------------------------
async function runAnimPreview() {
  const btn = $("#anim-preview-btn"); btn.disabled = true;
  const status = $("#anim-status"); status.textContent = "Rendering…";
  const v = AL.vce;
  const labels = vceLabels();
  let items, highlight;
  if (v.mode === "new_item") {
    items = labels.map((l, i) => (i === v.target ? l : ""));
    highlight = v.target;
  } else {
    items = labels;
    highlight = -1;
  }
  const first = AL.fmt.ranks.find(r => r.clip);
  const cfg = vceCfg();
  const body = {
    template: "valorant_clip_ranking", template_opts: vceTemplateOpts(),
    items, highlight, mode: v.mode, motion: cfg.motion, speed: cfg.speed || 1.0,
    sound: cfg.sound, volume: cfg.volume ?? 1.0,
    clip: first ? first.clip : null,
    layout: first ? (first.layout || { clip: first.clip }) : null,
  };
  try {
    const res = await api("/api/formatter/animate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const vid = $("#anim-video");
    $("#anim-video-wrap").classList.remove("hidden");
    vid.src = res.preview + "?t=" + Date.now();
    vid.load();
    vid.play().catch(() => {});   // button click is a user gesture → sound allowed
    status.textContent = "";
  } catch (e) { status.textContent = ""; toast("Preview failed: " + e.message, true); }
  finally { btn.disabled = false; }
}
function newRank() { return { label: "", clip: "", layout: null, join: { visual: "cut", sound: "none", volume: 1.0 }, playPos: 1 }; }

// derive per-rank play numbers from the asc/desc order (custom keeps user edits)
function seedPlayPositions() {
  const n = AL.fmt.ranks.length;
  AL.fmt.ranks.forEach((r, i) => { r.playPos = AL.fmt.order === "asc" ? i + 1 : n - i; });
}

// called when the Formatter tab is opened
function loadFormatter() {
  if (!fmtMetaLoaded) return;               // initFormatter still fetching
  renderRankSlots();
  vceBuildListInputs();
  vceSyncTarget();
  vceRebuild();
  vceFit();                                 // the stage was display:none until now
  refreshStageBg();
}

// options for the join <select> (built from /api/formatter/meta)
function joinOptionsHtml(selected) {
  const joins = AL.fmtMeta.joins.length ? AL.fmtMeta.joins : [{ key: "cut", label: "Hard cut" }];
  return joins.map(j => `<option value="${j.key}"${j.key === selected ? " selected" : ""}>${j.label}</option>`).join("");
}
// The full template_opts payload for the backend (build / preview / animate).
// Mirrors formatter._merge's override sections; per-rank arrays are sized to
// the current rank count.
function vceTemplateOpts() {
  const v = AL.vce, n = AL.fmt.ranks.length;
  const off = vceOff();
  const accent = vceAccent();
  return {
    style: v.style,
    title: { text: v.title, accent, accent_words: (v.accentWords || []).slice(), pill_fill: v.pillFill || "#a9c9ec", max_lines: v.titleLines === 1 ? 1 : 2 },
    list: { accent, medals: v.style === "pill" && !!v.medalsOn, medal_text: v.style === "pill" && !!v.medalText },   // #1/2/3 gold·silver·bronze on numbers / label text (Blue Pill only)
    offsets: { tx: off.tx, ty: off.ty, lx: off.lx, ly: off.ly, ts: off.ts, ls: off.ls },
    widgets: Array.from({ length: n }, (_, i) => v.widgets[i] || null),
    clip_infos: Array.from({ length: n }, (_, i) => (vceClipHasContent(v.clips[i]) ? v.clips[i] : null)),
    clip_style: {
      bg: v.clipStyle.bg, accent: v.clipStyle.accent, text: v.clipStyle.text,
      size: v.clipStyle.size, text_size: v.clipStyle.textSize, opacity: v.clipStyle.opacity,
    },
    bonus: {
      on: v.bonus.on, text: v.bonus.text, style: v.bonus.style, pos: "above",
      size: v.bonus.size ?? 1, hide_on: v.bonus.hideOn, hide_secs: v.bonus.hideSecs,
      delay_on: !!v.bonus.delayOn, delay: v.bonus.delay ?? 1.0,
      speed: v.bonus.speed ?? 1.0, sound: v.bonus.sound || "none", volume: v.bonus.volume ?? 1.0,
    },
    subtitle: {
      on: v.subtitle.on, text: v.subtitle.text, after: v.subtitle.after || "",
      size: v.subtitle.size,
      streamers: v.subtitle.streamers, streamer_keys: vceStreamerKeys(),
    },
    icon: { on: v.icon.on, color: v.icon.color, opacity: v.icon.opacity, size: v.icon.size },
  };
}

// Custom play sequence -> play_order indices into the FILLED (sent) ranks array.
// Requires every rank's play # to be a unique 1..N value; returns null if not.
function customPlayOrder() {
  const n = AL.fmt.ranks.length;
  const seen = new Set();
  for (const r of AL.fmt.ranks) {
    const pos = parseInt(r.playPos, 10);
    if (!(pos >= 1 && pos <= n) || seen.has(pos)) return null;
    seen.add(pos);
  }
  const inPlayOrder = [...AL.fmt.ranks].sort((a, b) => a.playPos - b.playPos).filter(r => r.clip);
  const filled = AL.fmt.ranks.filter(r => r.clip);
  return inPlayOrder.map(r => filled.indexOf(r));
}

// Switch two ranks' positions, carrying each entry's full content — label, clip,
// its edits (layout) and its widget + clip-info card — but leaving the badge number,
// play number and "join to next" fixed to the slot (those describe the position).
function swapRanks(i, j) {
  const ranks = AL.fmt.ranks;
  if (i < 0 || j < 0 || i >= ranks.length || j >= ranks.length || i === j) return;
  const a = ranks[i], b = ranks[j];
  ["label", "clip", "layout"].forEach(k => { const t = a[k]; a[k] = b[k]; b[k] = t; });
  [AL.vce.widgets, AL.vce.clips].forEach(arr => { const t = arr[i]; arr[i] = arr[j]; arr[j] = t; });
  // follow the clip if the single-clip editor is currently open on one of them
  if (AL.editingSlot === i) AL.editingSlot = j;
  else if (AL.editingSlot === j) AL.editingSlot = i;
  renderRankSlots();
  vceRanksChanged();
}

function renderRankSlots() {
  const box = $("#fmt-ranks");
  box.innerHTML = "";
  const n = AL.fmt.ranks.length;
  // Joins live at the seam between two *played* segments, so a rank's "join to
  // next" describes the transition AFTER it plays — the rank that plays LAST has
  // no following segment and hides the control. The backend keys the join off the
  // play order (ranks[order[pos]].join), so anchor the UI to play order too, not
  // the slot/badge index — otherwise under `desc` (the default countdown) the
  // hidden control lands on the wrong rank and the into-next-clip transition is
  // silently unreachable (defaults to a hard cut). Sort by playPos, which
  // seedPlayPositions keeps in sync for asc/desc/custom.
  const playSeq = AL.fmt.ranks.map((_, i) => i).sort((a, b) =>
    (AL.fmt.ranks[a].playPos || a + 1) - (AL.fmt.ranks[b].playPos || b + 1));
  const playAt = {};                       // rank index -> 0-based play position
  playSeq.forEach((ri, p) => { playAt[ri] = p; });
  const lastPlayed = playSeq[playSeq.length - 1];
  AL.fmt.ranks.forEach((r, i) => {
    const slot = document.createElement("div");
    slot.className = "rank-slot" + (AL.editingSlot === i ? " editing" : "");
    const isLast = i === lastPlayed;
    // the rank that plays right after this one (for a play-order-aware label)
    const nextRi = playSeq[playAt[i] + 1];
    const nextName = nextRi == null ? "" :
      `#${nextRi + 1}${AL.fmt.ranks[nextRi].label ? " " + AL.fmt.ranks[nextRi].label.trim() : ""}`;
    slot.innerHTML = `
      <div class="rank-top">
        <span class="rank-badge">${i + 1}</span>
        <input class="rank-label-in" type="text" placeholder="Rank ${i + 1} label (e.g. Smoove Neon)" value="${(r.label || "").replace(/"/g, "&quot;")}" />
        <label class="rank-play" title="Order this rank plays in">▶<input class="rank-play-in" type="number" min="1" max="${n}" value="${r.playPos || (i + 1)}" ${AL.fmt.order === "custom" ? "" : "disabled"} /></label>
        <button class="ghost rank-up" type="button" title="Swap with the rank above" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="ghost rank-down" type="button" title="Swap with the rank below" ${isLast ? "disabled" : ""}>↓</button>
        <button class="ghost rank-del" type="button" title="Remove rank">✕</button>
      </div>
      <div class="rank-mid">
        <button class="rank-clip-btn ghost" type="button" title="Pick a clip from your library">${rankClipLabel(r.clip)}</button>
        <button class="ghost rank-find" type="button" title="Search or upload a clip for this rank">🔍 Find</button>
        <button class="ghost rank-edit" type="button" ${r.clip ? "" : "disabled"}>✎ Open editor</button>
        <button class="ghost rank-preview" type="button" ${r.clip ? "" : "disabled"} title="Render just this rank's segment (fast — no full build)">🎬 Preview</button>
        <span class="rank-status ${r.layout && r.layout._edited ? "set" : ""}">${r.clip ? (r.layout && r.layout._edited ? "edited ✓" : "not edited") : "no clip"}</span>
      </div>
      ${isLast ? "" : `
      <div class="rank-bottom">
        <div class="join-arrow" title="Transition played when this clip finishes, into the clip that plays next">→ transition into ${esc(nextName)}</div>
        <label>Transition
          <select class="rank-join">${joinOptionsHtml(r.join.visual)}</select>
        </label>
        <label>Sound
          <select class="rank-join-snd">${soundOptionsHtml(r.join.sound)}</select>
        </label>
        <button class="ghost rank-join-play" type="button" title="Audition sound">▶</button>
      </div>`}`;
    slot.querySelector(".rank-label-in").addEventListener("input", e => {
      r.label = e.target.value;
      // the editor's list input is the same label — keep it (and the canvas) in sync
      const li = $(`#vce-list-inputs .vce-li-in[data-i="${i}"]`);
      if (li && li.value !== e.target.value) li.value = e.target.value;
      vceSyncTarget(); vceRebuild();
    });
    slot.querySelector(".rank-play-in").addEventListener("change", e => {
      r.playPos = parseInt(e.target.value, 10) || 1;
      renderRankSlots();   // play order changed -> re-anchor which rank hides its join
    });
    slot.querySelector(".rank-up").onclick = () => swapRanks(i, i - 1);
    slot.querySelector(".rank-down").onclick = () => swapRanks(i, i + 1);
    slot.querySelector(".rank-del").onclick = () => {
      if (AL.fmt.ranks.length <= 1) return;
      AL.fmt.ranks.splice(i, 1);
      AL.vce.widgets.splice(i, 1); AL.vce.clips.splice(i, 1);
      seedPlayPositions(); renderRankSlots(); vceRanksChanged();
    };
    slot.querySelector(".rank-clip-btn").onclick = () =>
      openClipPicker(c => assignClipFromLibrary(i, c), `Pick a clip for rank #${i + 1}`);
    slot.querySelector(".rank-find").onclick = () => openRankSearch(i);
    slot.querySelector(".rank-edit").onclick = () => openSlotEditor(i);
    slot.querySelector(".rank-preview").onclick = e => buildOneRank(i, e.currentTarget);
    if (!isLast) {
      slot.querySelector(".rank-join").addEventListener("change", e => { r.join.visual = e.target.value; });
      slot.querySelector(".rank-join-snd").addEventListener("change", e => { r.join.sound = e.target.value; });
      slot.querySelector(".rank-join-play").onclick = () => previewSound(slot.querySelector(".rank-join-snd").value);
    }
    box.appendChild(slot);
  });
  vceSyncTarget();   // keep the "New item — which rank" picker in sync with the labels
  if (AL.fmtHook && AL.fmtHook.on) renderHookSrc();   // hook source-clip buttons follow the ranks
}

// ---- Saved projects (resume an edit + re-render) --------------------------
// The whole Formatter edit is captured as {vce, fmt} and persisted by name.
// Loading restores every control so you can re-render without rebuilding.
function fmtSnapshot() {
  // Capture the music bed too (minus the heavy catalog) so a saved edit
  // re-renders with the same track — it was silently dropped before.
  const m = AL.fmtMusic || {};
  const music = m.track ? {
    track: m.track, title: m.title, artist: m.artist, tier: m.tier,
    credit: m.credit, file: m.file, downloaded: !!m.downloaded,
    volume: m.volume, keep: m.keep, clipVol: m.clipVol, start: m.start, fade: m.fade,
  } : null;
  // Clip Hook + Subscribe CTA are plain config objects — persist them too (they
  // were dropped before, so a loaded project lost both widgets).
  const hook = AL.fmtHook ? Object.assign({}, AL.fmtHook) : null;
  const sub = AL.fmtSub ? Object.assign({}, AL.fmtSub) : null;
  return JSON.parse(JSON.stringify({ vce: AL.vce, fmt: AL.fmt, music, hook, sub }));
}

// Restore the saved music bed onto AL.fmtMusic + reflect it in the UI.
function restoreMusic(m) {
  if (!AL.fmtMusic) return;
  if (!m || !m.track) { clearMusic(); return; }
  Object.assign(AL.fmtMusic, {
    track: m.track, title: m.title, artist: m.artist, tier: m.tier,
    credit: m.credit, file: m.file, downloaded: m.downloaded !== false,
    volume: m.volume ?? 0.28, keep: m.keep !== false,
    clipVol: m.clipVol ?? 1.0, start: m.start ?? 0, fade: m.fade ?? 2,
  });
  const t = AL.fmtMusic;
  $("#fmt-music-sel").innerHTML = `<b>${t.title || ""}</b> — ${t.artist || ""} <span class="mtier mtier-${t.tier}">${t.tier || ""}</span>`;
  $("#fmt-music-clear").classList.remove("hidden");
  $("#fmt-music-ctrls").classList.remove("hidden");
  if (t.file) $("#fmt-music-audio").src = t.file; else $("#fmt-music-audio").removeAttribute("src");
  $("#fmt-music-note").textContent = t.credit
    ? "NCS track — a paste-ready credit line appears after you render."
    : (t.tier === "A" ? "Bundle-safe (CC-BY) — no credit needed."
      : "Pixabay — no credit needed; use in output only.");
  const vol = Math.round(t.volume * 100), cv = Math.round(t.clipVol * 100);
  $("#fmt-music-vol").value = vol; $("#fmt-music-vol-v").textContent = vol + "%";
  $("#fmt-music-clipvol").value = cv; $("#fmt-music-clipvol-v").textContent = cv + "%";
  $("#fmt-music-keep").checked = t.keep;
  $("#fmt-music-clipvol-wrap").style.opacity = t.keep ? "" : ".4";
  $("#fmt-music-start").value = t.start;
  $("#fmt-music-fade").value = t.fade;
}

async function loadProjectList(selectSlug) {
  let items = [];
  try { items = await api("/api/projects"); } catch { items = []; }
  const sel = $("#proj-list");
  sel.innerHTML = '<option value="">— saved edits —</option>' + items.map(p =>
    `<option value="${p.slug}"${p.slug === selectSlug ? " selected" : ""}>${(p.name || p.slug).replace(/"/g, "&quot;")}</option>`
  ).join("");
}

async function saveProject() {
  const name = ($("#proj-name").value || "").trim() || (AL.vce.title || "Untitled ranking");
  try {
    const res = await api("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, state: fmtSnapshot() }),
    });
    $("#proj-name").value = res.name;
    $("#proj-status").textContent = "Saved “" + res.name + "”";
    await loadProjectList(res.slug);
  } catch (e) { toast("Save failed: " + e.message, true); }
}

async function loadProject() {
  const slug = $("#proj-list").value;
  if (!slug) return toast("Pick a saved edit to load", true);
  try {
    const data = await api("/api/projects/" + encodeURIComponent(slug));
    restoreFmtState(data.state || {});
    $("#proj-name").value = data.name || "";
    $("#proj-status").textContent = "Loaded “" + (data.name || slug) + "” — re-render when ready";
  } catch (e) { toast("Load failed: " + e.message, true); }
}

async function deleteProject() {
  const slug = $("#proj-list").value;
  if (!slug) return toast("Pick a saved edit first", true);
  if (!confirm("Delete this saved edit? This can't be undone.")) return;
  try {
    await api("/api/projects/" + encodeURIComponent(slug), { method: "DELETE" });
    $("#proj-status").textContent = "Deleted";
    await loadProjectList();
  } catch (e) { toast("Delete failed: " + e.message, true); }
}

// Rebuild the Formatter UI from a saved snapshot. New snapshots are {vce, fmt};
// legacy {tpl, fmt, anim} ones map their basics (title, ranks, reveal settings)
// onto the new editor.
function restoreFmtState(state) {
  AL.fmt = Object.assign({ order: "desc", ranks: [] }, state.fmt || {});
  // Merge the saved snapshot onto the LIVE AL.vce object (and its nested objects)
  // IN PLACE — never reassign AL.vce. initVceEditor() captures `AL.vce` (and
  // `AL.vce.clipStyle`) in its control-handler closures once at startup; swapping the
  // object out from under them silently orphaned every control after a project load —
  // edits landed on the dead object while the renderer read the new one (mode toggle,
  // icon/bonus checkboxes, title, clip style, … all no-op'd). A same-identity merge
  // keeps every handler pointing at the object the renderer actually reads.
  const v = AL.vce;
  if (state.vce) {
    const src = state.vce;
    // nested objects the handlers read through `AL.vce` — merged in place so their
    // identity (notably clipStyle, which initVceEditor captures as `cs`) is preserved.
    const NESTED = ["clipStyle", "bonus", "subtitle", "icon", "accent"];
    Object.keys(src).forEach(k => { if (!NESTED.includes(k)) v[k] = src[k]; });
    NESTED.forEach(k => {
      if (!v[k] || typeof v[k] !== "object") v[k] = {};
      if (src[k] && typeof src[k] === "object") Object.assign(v[k], src[k]);
    });
    if (!Array.isArray(v.accentWords)) v.accentWords = [];
    // migrate a legacy single shared reveal set (flat motion/sound/volume/speed)
    // into per-phase anim, seeding both phases identically
    if (!src.anim &&
        (src.motion || src.sound || src.speed != null || src.volume != null)) {
      const legacy = { motion: src.motion || "pop", sound: src.sound || "pop",
                       volume: src.volume ?? 1.0, speed: src.speed ?? 1.0 };
      v.anim = { intro: Object.assign({}, legacy), new_item: Object.assign({}, legacy) };
    }
  } else if (state.tpl) {                     // legacy project
    AL.vce.title = state.tpl.title || AL.vce.title;
    const a = (state.anim || {}).intro || {};
    const cfg = vceNormalizeAnim(AL.vce);
    ["intro", "new_item"].forEach(ph => {
      if (a.motion) cfg[ph].motion = a.motion;
      if (a.sound) cfg[ph].sound = a.sound;
      if (a.speed) cfg[ph].speed = a.speed;
      if (a.volume != null) cfg[ph].volume = a.volume;
    });
    if (state.anim && state.anim.enabled != null) AL.vce.bake = !!state.anim.enabled;
  }
  vceNormalizeAnim(AL.vce);                    // ensure structure + defaults, drop nothing
  ["motion", "sound", "volume", "speed"].forEach(k => delete AL.vce[k]);  // no stale flat fields
  AL.vce.titleLines = AL.vce.titleLines === 1 ? 1 : 2;                     // default double for pre-toggle projects
  const n = AL.fmt.ranks.length;
  AL.vce.widgets = Array.from({ length: n }, (_, i) => (AL.vce.widgets || [])[i] || null);
  AL.vce.clips = Array.from({ length: n }, (_, i) => (AL.vce.clips || [])[i] || null);
  if (!AL.vce.off) AL.vce.off = {};
  (AL.fmtMeta.styles || []).forEach(s => {
    if (!AL.vce.off[s.key]) AL.vce.off[s.key] = Object.assign({}, s.offsets);
  });

  $("#fmt-order").value = AL.fmt.order || "desc";
  $("#anim-bake").checked = AL.vce.bake !== false;
  vceActiveClipIdx = null;
  renderRankSlots();
  vceApplyVceToControls();
  vceBuildListInputs();
  vceSyncTarget();
  restoreMusic(state.music);
  restoreHook(state.hook);
  restoreSub(state.sub);
  vceRebuild();
  refreshStageBg(true);
}

// Restore the saved Clip Hook onto AL.fmtHook + reflect it in the UI controls.
function restoreHook(h) {
  if (!AL.fmtHook) return;
  const d = { on: false, clip: null, moment: 0, length: 1.5, in_speed: 1, out_speed: 1,
              style: "glow", accent: "#2ea6ff", sound: "whoosh", volume: 1, freeze_bg: false };
  Object.assign(AL.fmtHook, d, h || {});
  const k = AL.fmtHook;
  const on = $("#fmt-hook-on"); if (on) on.checked = !!k.on;
  const ctrls = $("#fmt-hook-ctrls"); if (ctrls) ctrls.classList.toggle("hidden", !k.on);
  $$("#fmt-hook-style button").forEach(b => b.classList.toggle("active", b.dataset.v === k.style));
  $$("#fmt-hook-swatches button").forEach(b => b.classList.toggle("active", b.dataset.hex === k.accent));
  const set = (id, val) => { const e = $(id); if (e) e.value = val; };
  set("#fmt-hook-moment", k.moment);
  set("#fmt-hook-length", k.length); if ($("#fmt-hook-length-v")) $("#fmt-hook-length-v").textContent = (+k.length).toFixed(1) + "s";
  set("#fmt-hook-outspeed", k.out_speed); if ($("#fmt-hook-outspeed-v")) $("#fmt-hook-outspeed-v").textContent = (+k.out_speed).toFixed(1) + "×";
  set("#fmt-hook-sound", k.sound);
  set("#fmt-hook-vol", Math.round((k.volume ?? 1) * 100)); if ($("#fmt-hook-vol-v")) $("#fmt-hook-vol-v").textContent = Math.round((k.volume ?? 1) * 100) + "%";
  const fz = $("#fmt-hook-freeze"); if (fz) fz.checked = !!k.freeze_bg;
  if (k.on) renderHookSrc();
}

// Restore the saved Subscribe CTA onto AL.fmtSub + reflect it in the UI controls.
function restoreSub(s) {
  if (!AL.fmtSub) return;
  const d = { on: false, style: "classic", dur: 4.0, scale: 1.0, anim: 0.7, sound: "pop",
              accent: "#ff0033", matchTitle: false };
  Object.assign(AL.fmtSub, d, s || {});
  const k = AL.fmtSub;
  const on = $("#fmt-sub-on"); if (on) on.checked = !!k.on;
  const ctrls = $("#fmt-sub-ctrls"); if (ctrls) ctrls.classList.toggle("hidden", !k.on);
  $$("#fmt-sub-style button").forEach(b => b.classList.toggle("active", b.dataset.v === k.style));
  const set = (id, val) => { const e = $(id); if (e) e.value = val; };
  set("#fmt-sub-dur", k.dur);
  set("#fmt-sub-scale", k.scale); if ($("#fmt-sub-scale-v")) $("#fmt-sub-scale-v").textContent = Math.round(k.scale * 100) + "%";
  set("#fmt-sub-anim", k.anim); if ($("#fmt-sub-anim-v")) $("#fmt-sub-anim-v").textContent = (+k.anim).toFixed(2) + "s";
  set("#fmt-sub-sound", k.sound);
  set("#fmt-sub-color", k.accent || "#ff0033");
  const match = $("#fmt-sub-match"); if (match) match.checked = !!k.matchTitle;
  const colorRow = $("#fmt-sub-color-row"); if (colorRow) colorRow.classList.toggle("hidden", !!k.matchTitle);
}

function buildFmtPayload() {
  const v = AL.vce;
  // Intro plays on the first segment, New item on each later reveal — each phase
  // carries its own Motion/Sound/Volume/Speed (toggle-scoped in the editor).
  const A = vceNormalizeAnim(v);
  // per-rank arrays must line up with the FILLED ranks the backend receives
  const filledIdx = AL.fmt.ranks.map((r, i) => r.clip ? i : -1).filter(i => i >= 0);
  const tov = vceTemplateOpts();
  tov.widgets = filledIdx.map(i => tov.widgets[i] || null);
  tov.clip_infos = filledIdx.map(i => tov.clip_infos[i] || null);
  const payload = {
    template: "valorant_clip_ranking",
    template_opts: tov,
    order: AL.fmt.order,
    ranks: AL.fmt.ranks
      .filter(r => r.clip)
      .map(r => ({ label: r.label, layout: r.layout || { clip: r.clip }, join: r.join })),
    anim: { enabled: v.bake !== false, intro: Object.assign({}, A.intro), new_item: Object.assign({}, A.new_item) },
  };
  if (AL.fmt.order === "custom") {
    const po = customPlayOrder();
    if (po) payload.play_order = po;
  }
  const m = AL.fmtMusic;
  if (m && m.track && m.downloaded) {
    payload.music = {
      track: m.track, volume: m.volume, keep_clip_audio: m.keep,
      clip_volume: m.clipVol, start: m.start, fade_out: m.fade,
    };
  }
  const s = AL.fmtSub;
  if (s && s.on) {
    // "Match title accent" resolves to the current title accent at build time so it
    // always tracks the title; otherwise use the picked VD colour.
    const vdAccent = s.matchTitle ? vceAccent() : (s.accent || "#ff0033");
    payload.subscribe = { on: true, style: s.style, dur: s.dur, scale: s.scale,
                          anim: s.anim, sound: s.sound, volume: 1.0, x: 0.5, y: 0.5,
                          accent: vdAccent };
  }
  const hk = AL.fmtHook;
  if (hk && hk.on) {
    payload.hook = { on: true, clip: hk.clip || null, moment: hk.moment,
                     length: hk.length, in_speed: hk.in_speed, out_speed: hk.out_speed,
                     style: hk.style, accent: hk.accent, sound: hk.sound,
                     volume: hk.volume != null ? hk.volume : 1.0,
                     freeze_bg: !!hk.freeze_bg };
  }
  return payload;
}

// Render ONLY one rank's segment — a fast preview of that clip (with its
// progressive-reveal state) without building the whole Top-N. Result plays in
// the shared player modal.
async function buildOneRank(i, btn) {
  const r = AL.fmt.ranks[i];
  if (!r || !r.clip) return toast("Assign a clip first", true);
  if (AL.fmt.order === "custom" && !customPlayOrder())
    return toast("Give each rank a unique play # first", true);
  // map the slot index → index into the FILLED ranks the backend receives
  const filledIdx = AL.fmt.ranks.map((rk, idx) => (rk.clip ? idx : -1)).filter(x => x >= 0);
  const only = filledIdx.indexOf(i);
  if (only < 0) return;
  const payload = buildFmtPayload();
  payload.only = only;
  delete payload.music;                       // single-clip preview: skip the bed for speed
  const status = $("#fmt-status");
  if (btn) { btn.disabled = true; btn.textContent = "🎬 …"; }
  status.textContent = `Rendering rank ${i + 1}…`;
  try {
    const { job } = await api("/api/formatter/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await new Promise(resolve => {
      const tick = async () => {
        try {
          const s = await api("/api/jobs/" + job);
          if (s.status === "running" || s.status === "queued") { status.textContent = "Rendering…"; setTimeout(tick, 1000); }
          else resolve(s);
        } catch (e) { resolve({ status: "error", error: e.message }); }
      };
      tick();
    });
    if (j.status === "done") {
      status.textContent = "Preview ready ✓";
      openPlayer(j.output, `Rank ${i + 1} preview`);
    } else { status.textContent = ""; toast("Preview error: " + (j.error || "unknown"), true); }
  } catch (e) { status.textContent = ""; toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "🎬 Preview"; } }
}

async function buildRanking() {
  const filled = AL.fmt.ranks.filter(r => r.clip);
  if (!filled.length) return toast("Assign a clip to at least one rank", true);
  if (AL.fmt.order === "custom" && !customPlayOrder())
    return toast("Give each rank a unique play # (1…" + AL.fmt.ranks.length + ")", true);
  const btn = $("#fmt-build"); btn.disabled = true;
  const status = $("#fmt-status"); status.textContent = "Queued…";
  renderProgress($("#fmt-progress"), null, "Queued…");
  try {
    const { job } = await api("/api/formatter/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildFmtPayload()),
    });
    pollFmtJob(job, status, btn);
  } catch (e) { status.textContent = ""; endRenderProgress($("#fmt-progress"), false); btn.disabled = false; toast("Build failed: " + e.message, true); }
}
async function pollFmtJob(job, status, btn) {
  const prog = $("#fmt-progress");
  try {
    const j = await api("/api/jobs/" + job);
    if (j.status === "running" || j.status === "queued") {
      status.textContent = j.stage ? j.stage + "…" : "Building…";
      renderProgress(prog, (typeof j.progress === "number" ? j.progress : null),
                     j.stage || "Building…");
      setTimeout(() => pollFmtJob(job, status, btn), 1200);
    } else if (j.status === "done") {
      status.textContent = "Done ✓"; btn.disabled = false;
      endRenderProgress(prog, true);
      $("#fmt-output").classList.remove("hidden");
      $("#fmt-out-video").src = j.output; $("#fmt-download").href = j.output;
      const cr = $("#fmt-credit");
      if (j.credit) { $("#fmt-credit-text").textContent = j.credit; cr.classList.remove("hidden"); }
      else cr.classList.add("hidden");
    } else {
      status.textContent = ""; btn.disabled = false;
      endRenderProgress(prog, false);
      toast("Build error: " + (j.error || "unknown"), true);
    }
  } catch (e) { status.textContent = ""; btn.disabled = false; endRenderProgress(prog, false); toast(e.message, true); }
}

// ---- Per-rank clip finder (search + section download, or upload own) ----
let rankSearchIndex = null;
let rsResults = [];
function openRankSearch(i) {
  rankSearchIndex = i;
  $("#rs-rank").textContent = i + 1;
  $("#rs-results").innerHTML = `<div class="hint">Search a creator/query, or upload your own clip.</div>`;
  $("#rank-search").classList.remove("hidden");
  rsSetMode("basic");
  // prefill the query from the rank's label (handy — the label is usually the player)
  const r = AL.fmt.ranks[i];
  if (r && r.label && !$("#rs-creator").value) $("#rs-creator").value = r.label;
  setTimeout(() => $("#rs-query").focus(), 30);
}
// Switch the rank finder between the Basic searcher and the 🧪 Deep (experimental)
// engine. The deep engine is lazily wired the first time it's shown, with an
// assign-to-rank destination — the SAME code that powers the Search tab.
function rsSetMode(mode) {
  $$(".rs-mode", $("#rank-search")).forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
  $$(".rs-pane", $("#rank-search")).forEach(p => p.classList.toggle("hidden", p.dataset.mode !== mode));
  if (mode === "deep") {
    xInit("rx", downloadAndAssign);
    setTimeout(() => $("#rx-query").focus(), 30);
  }
}
function closeRankSearch() {
  $("#rank-search").classList.add("hidden");
  rankSearchIndex = null;
}
async function doRankSearch() {
  const creator = $("#rs-creator").value.trim();
  const query = $("#rs-query").value.trim();
  if (!creator && !query) return toast("Enter a creator or query", true);
  const box = $("#rs-results");
  box.innerHTML = `<div class="hint">Searching…</div>`;
  try {
    rsResults = await api("/api/clips/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ creator, query, limit: 16, include_twitch: $("#rs-twitch").checked }),
    });
    box.innerHTML = "";
    if (!rsResults.length) { box.innerHTML = `<div class="hint">No results.</div>`; return; }
    rsResults.forEach(r => box.appendChild(resultRowEl(r, downloadAndAssign)));
  } catch (e) { box.innerHTML = `<div class="hint">Error: ${e.message}</div>`; }
}
// Get a search result (optionally a section) and drop it onto the current rank.
async function downloadAndAssign(res, btn, start, end) {
  const i = rankSearchIndex;
  if (i === null) return;
  try {
    const dl = await downloadClip(res.url, btn, start, end, true);   // keep — it's for a project
    await assignClipToRank(i, dl.path || dl.name);
  } catch { /* toast already shown */ }
}
// Assign a library clip to a rank (keeps it, refreshes the picker), then either
// open the editor or close the finder.
// Compact label for a rank's clip button (thumbnail + name), or a prompt to pick.
function rankClipLabel(path) {
  if (!path) return `📁 Pick a clip`;
  const c = (AL.libClips || []).find(x => x.path === path);
  const name = path.split("/").pop();
  const thumb = c ? c.thumb : `/api/clips/thumb?path=${encodeURIComponent(path)}`;
  return `<img class="rank-clip-thumb" src="${esc(thumb)}" alt="" onerror="this.style.display='none'"><span class="rank-clip-name" title="${esc(name)}">${esc(name)}</span><span class="rank-clip-swap">swap</span>`;
}
// Assign a library clip to a rank (from the folder picker — no editor auto-open).
async function assignClipFromLibrary(i, c) {
  await keepClip(c.path, true);
  const r = AL.fmt.ranks[i];
  r.clip = c.path; r.layout = { clip: c.path };
  await loadLibrary();
  renderRankSlots(); refreshStageBg();
  toast(`Rank #${i + 1}: ${c.name}`);
}

async function assignClipToRank(i, name) {
  await keepClip(name, true);
  await loadLibrary();                        // refresh AL.libClips so the picker has it
  const r = AL.fmt.ranks[i];
  r.clip = name; r.layout = { clip: name };
  renderRankSlots(); refreshStageBg();
  const open = $("#rs-open").checked;
  closeRankSearch();
  toast(`Added to rank #${i + 1}`);
  if (open) openSlotEditor(i);
}
async function uploadToRank(file) {
  const i = rankSearchIndex;
  if (!file || i === null) return;
  const fd = new FormData();
  fd.append("file", file);
  toast("Uploading…");
  try {
    const res = await api("/api/clips/import", { method: "POST", body: fd });
    await assignClipToRank(i, res.path || res.name);
  } catch (e) { toast("Upload failed: " + e.message, true); }
}

// ---- Open the single-clip editor for a rank slot, then save back ----
function openSlotEditor(i) {
  const r = AL.fmt.ranks[i];
  if (!r.clip) return toast("Assign a clip first", true);
  AL.editingSlot = i;
  selectTab("search", AL.tabBtns["search"]);
  selectClip({ name: r.clip.split("/").pop(), path: r.clip, url: `/storage/clips/${r.clip}` }, () => {
    if (r.layout && r.layout._edited) applyLayoutToEditor(r.layout);
    updateSlotEditorUI();
    refreshPreview();
  });
}
function updateSlotEditorUI() {
  const on = AL.editingSlot !== null;
  $("#al-render").classList.toggle("hidden", on);
  $("#al-save-slot").classList.toggle("hidden", !on);
  $("#al-cancel-slot").classList.toggle("hidden", !on);
  const banner = $("#al-slot-banner");
  banner.classList.toggle("hidden", !on);
  if (on) banner.textContent = `Editing clip for rank #${AL.editingSlot + 1} — trim / lay out / cut, then “Save to rank”.`;
}
function saveSlotFromEditor() {
  if (AL.editingSlot === null) return;
  const i = AL.editingSlot;
  const opts = buildOpts();
  opts._edited = true;
  AL.fmt.ranks[i].layout = opts;
  AL.fmt.ranks[i].clip = opts.clip;
  AL.editingSlot = null;
  updateSlotEditorUI();
  selectTab("formatter", AL.tabBtns["formatter"]);
  renderRankSlots();
  refreshStageBg(true);          // the edited layout changes the composed frame
  toast(`Saved to rank #${i + 1}`);
}
function cancelSlotEditor() {
  AL.editingSlot = null;
  updateSlotEditorUI();
  selectTab("formatter", AL.tabBtns["formatter"]);
}
// switch a niche's sub-tab (Search panel; expandable to more niches later)
function selectSubTab(panel, sub) {
  $$(".subtab", panel).forEach(b => b.classList.toggle("active", b.dataset.sub === sub));
  $$(".subpanel", panel).forEach(p => p.classList.toggle("hidden", p.dataset.sub !== sub));
}
// push a saved buildOpts back onto the editor controls (best-effort for the main knobs)
function applyLayoutToEditor(o) {
  if (o.trim_start != null) $("#al-trim-start").value = (+o.trim_start).toFixed(1);
  if (o.trim_end != null) $("#al-trim-end").value = (+o.trim_end).toFixed(1);
  $("#al-fc-on").checked = o.facecam_enabled !== false;
  const fcLocked = o.facecam_height != null;
  $("#al-fc-lock").checked = fcLocked;
  if (fcLocked) {
    const p = Math.round(o.facecam_height * 100);
    $("#al-fc-h").value = p; $("#al-fc-h-val").textContent = p;
  }
  $("#al-edges-on").checked = o.gameplay_fill === "blur";
  if (o.blur_bar != null) {                       // restore edge-blur (also keeps the lock seed accurate)
    const b = Math.round(o.blur_bar * 100);
    $("#al-bar").value = b; $("#al-bar-val").textContent = b;
  }
  $("#al-mc-on").checked = !!o.mousecam_enabled;
  $("#al-fade-on").checked = !!o.audio_fade_out;
  if (o.speed) { $("#al-speed").value = o.speed; $("#al-speed-val").textContent = o.speed + "×"; }
  if (o.rect) setRect("facecam", o.rect);
  if (o.mousecam) setRect("mousecam", o.mousecam);
  if (o.mousecam_pos) $("#al-mc-pos").value = o.mousecam_pos;
  AL.mcPos = (o.mousecam_x != null && o.mousecam_y != null)
    ? { x: o.mousecam_x, y: o.mousecam_y }
    : mcCornerToPos(o.mousecam_pos || "bottom-right");
  AL.cuts = (o.cuts || []).map(c => ({
    start: c.start, end: c.end,
    transition: c.transition ? { on: true, ...c.transition } : newTransition(),
  }));
  // B-roll markers are saved in the layout — restore them so they reappear in the
  // editor (previously dropped on reopen, though the saved build still used them).
  AL.effects = (o.effects || []).map(e => ({ type: e.type, t: e.t, dur: e.dur, sound: e.sound, volume: e.volume, scale: e.scale, x: e.x, y: e.y, params: e.params }));
  syncBoxVisibility();
  updateTrimInfo();
  applyAutoZoom(); drawTrimBar();
  renderCuts();
  renderBrollList(); drawEffectMarkers();
}

// ------------------------------------------------------------------ emoji picker
// "Click to add emoji" for the rendered-text fields (title / bonus). All of these
// render as color emoji in the build now (Apple Color Emoji), so the palette is
// seeded with a hype/gaming set that's guaranteed to render.
const EMOJI_SET = [
  "👑","🤩","🔥","💀","😭","😳","🐐","🤯","⭐","💯","👀","😱","⚡","🥶","🗿","🎯",
  "🏆","🥇","✨","💥","😤","🫡","🙏","💪","👌","🤙","🥵","🤡","🧠","🚀","❤️","💙",
  "🖤","💚","💛","🔫","🎮","😮‍💨","🤤","😎","🥹","😬","🫠","👇","🆚","‼️","👏","🙌",
];
function insertAtCursor(input, text) {
  const s = input.selectionStart ?? input.value.length;
  const e = input.selectionEnd ?? input.value.length;
  input.value = input.value.slice(0, s) + text + input.value.slice(e);
  const pos = s + text.length;
  input.focus();
  try { input.setSelectionRange(pos, pos); } catch { /* number inputs etc. */ }
  input.dispatchEvent(new Event("input", { bubbles: true }));   // drive existing handlers → vceRebuild
}
function openEmojiPicker(anchor, input) {
  openPopover(anchor, `<div class="cb-pop-title">Add emoji</div>
    <div class="emoji-grid">${EMOJI_SET.map(e => `<button class="emoji-cell" type="button" data-e="${e}">${e}</button>`).join("")}</div>`,
    p => p.querySelectorAll("[data-e]").forEach(b => b.onclick = () => insertAtCursor(input, b.dataset.e)));  // stays open for multiple
}
function wireEmojiButtons(root = document) {
  $$(".emoji-btn[data-emoji-for]", root).forEach(b => {
    if (b._wired) return; b._wired = true;
    b.onclick = ev => { ev.preventDefault(); const inp = document.getElementById(b.dataset.emojiFor); if (inp) openEmojiPicker(b, inp); };
  });
}

// ------------------------------------------------------------------ wire up
// ============================================================= //
// Shared preview player (Library thumbnails + package candidates)
// ============================================================= //
function openPlayer(url, title, opts = {}) {
  const v = $("#player-video");
  $("#player-title").textContent = title || "Preview";
  v.src = url;
  v.currentTime = 0;
  const acts = $("#player-actions");
  acts.innerHTML = "";
  (opts.actions || []).forEach(a => {
    const b = document.createElement("button");
    b.className = a.primary ? "primary" : "ghost";
    b.type = "button"; b.textContent = a.label;
    b.onclick = a.onClick;
    acts.appendChild(b);
  });
  $("#player-modal").classList.remove("hidden");
  v.play().catch(() => {});
}
function closePlayer() {
  const v = $("#player-video");
  try { v.pause(); v.removeAttribute("src"); v.load(); } catch { /* ignore */ }
  $("#player-modal").classList.add("hidden");
}
function initPlayer() {
  $("#player-close").onclick = closePlayer;
  $("#player-modal").addEventListener("click", e => { if (e.target.id === "player-modal") closePlayer(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#player-modal").classList.contains("hidden")) closePlayer();
  });
}

// ============================================================= //
// B-roll effects — drag a moment onto the scrubber (or ⊕ at playhead)
// ============================================================= //
function brollCatalog() { return AL.fxData.effects || []; }

function renderBrollTray() {
  const tray = $("#broll-tray");
  if (!tray) return;
  const fx = brollCatalog();
  if (!fx.length) { tray.innerHTML = `<span class="hint">No effects available.</span>`; return; }
  tray.innerHTML = "";
  fx.forEach(e => {
    const chip = document.createElement("div");
    chip.className = "broll-chip";
    chip.draggable = true;
    chip.dataset.type = e.key;
    chip.title = e.hint || e.label;
    chip.innerHTML = `<span class="bc-emoji">${e.emoji || "✨"}</span><span>${esc(e.label)}</span><button class="bc-add" type="button" title="Drop at playhead">⊕</button>`;
    chip.addEventListener("dragstart", ev => { ev.dataTransfer.setData("text/broll", e.key); ev.dataTransfer.effectAllowed = "copy"; });
    chip.querySelector(".bc-add").onclick = ev => { ev.stopPropagation(); addEffectAtPlayhead(e.key); };
    tray.appendChild(chip);
  });
}

function addEffect(type, t) {
  if (!selectedClip) return toast("Load a clip first", true);
  const dur = AL.duration || 0;
  t = Math.max(0, Math.min(t, dur || t));
  const cat = brollCatalog().find(e => e.key === type) || {};
  AL.effects.push({
    type, t: +t.toFixed(2),
    dur: cat.flash_dur,                                  // per-marker flash/show length (slider)
    sound: (cat.sounds && cat.sounds[0] && cat.sounds[0].key) || null,   // default = first
    ...(cat.sounds && cat.sounds.length ? { volume: 1.0 } : {}),          // per-marker SFX gain
    // graphic-overlay chips (e.g. subscribe): size + centre position + VD mark colour
    ...(cat.overlay ? { scale: cat.scale ?? 1.0, x: 0.5, y: 0.5, vd_accent: cat.vd_accent || "#ff0033" } : {}),
    // animated CTA style variant (classic / card)
    ...(cat.styles && cat.styles.length ? { style: cat.style || cat.styles[0].key } : {}),
    // motion moments (e.g. Face Zoom): seed adjustable params from catalog defaults
    ...(cat.params && cat.params.length
        ? { params: Object.fromEntries(cat.params.map(p => [p.key, p.default])) } : {}),
  });
  AL.effects.sort((a, b) => a.t - b.t);
  renderBrollList(); drawEffectMarkers();
  toast(`${cat.label || type} @ ${t.toFixed(1)}s`);
}
function addEffectAtPlayhead(type) {
  const v = $("#al-video");
  addEffect(type, v.currentTime || 0);
}
function removeEffect(i) { AL.effects.splice(i, 1); renderBrollList(); drawEffectMarkers(); }

function renderBrollList() {
  const box = $("#broll-list");
  if (!box) return;
  box.innerHTML = "";
  const dur = AL.duration || 0;
  (AL.effects || []).forEach((e, i) => {
    const cat = brollCatalog().find(c => c.key === e.type) || {};
    const dmin = cat.dur_min ?? 0.25, dmax = cat.dur_max ?? 1.6;
    const durVal = e.dur ?? cat.flash_dur ?? 0.64;
    const sounds = cat.sounds || [];
    const overlay = !!cat.overlay;                        // graphic chip (subscribe)
    const smin = cat.scale_min ?? 0.6, smax = cat.scale_max ?? 1.6;
    const scaleVal = e.scale ?? cat.scale ?? 1.0;
    const params = cat.params || [];                      // motion moment sliders (Face Zoom)
    const styles = cat.styles || [];                      // CTA look variants (classic / card)
    const styleVal = e.style || cat.style || (styles[0] && styles[0].key);
    const vdColor = e.vd_accent || cat.vd_accent || "#ff0033";   // VD mark colour (overlay chips)
    const paramVal = p => (e.params && e.params[p.key] != null) ? e.params[p.key] : p.default;
    const fmtParam = (p, v) => p.fmt === "pct" ? Math.round(v * 100) + "%" : (+v).toFixed(2);
    const el = document.createElement("div");
    el.className = "broll-mk";
    el.innerHTML = `
      <span class="bm-name">${cat.emoji || "✨"} ${esc(cat.label || e.type)}</span>
      <label class="bm-ctl">@ <input class="bm-t" type="number" min="0" step="0.1" value="${e.t.toFixed(2)}"> s</label>
      <label class="bm-ctl bm-dur-ctl">${overlay ? "Show" : "Length"} <input class="bm-dur" type="range" min="${dmin}" max="${dmax}" step="0.01" value="${durVal}"><span class="bm-durv">${durVal.toFixed(2)}s</span></label>
      ${styles.length ? `<label class="bm-ctl bm-style-ctl">Style <select class="bm-style">${styles.map(s => `<option value="${esc(s.key)}"${s.key === styleVal ? " selected" : ""}>${esc(s.label)}</option>`).join("")}</select></label>` : ""}
      ${overlay ? `<label class="bm-ctl bm-scale-ctl">Size <input class="bm-scale" type="range" min="${smin}" max="${smax}" step="0.02" value="${scaleVal}"><span class="bm-scalev">${Math.round(scaleVal * 100)}%</span></label>` : ""}
      ${overlay ? `<label class="bm-ctl bm-vd-ctl" title="VD mark ring + D colour">VD <input class="bm-vd" type="color" value="${vdColor}"></label>` : ""}
      ${params.map(p => { const v = paramVal(p); return `<label class="bm-ctl bm-param-ctl">${esc(p.label)} <input class="bm-param" data-pk="${esc(p.key)}" type="range" min="${p.min}" max="${p.max}" step="${p.step}" value="${v}"><span class="bm-paramv">${fmtParam(p, v)}</span></label>`; }).join("")}
      ${sounds.length ? `<label class="bm-ctl">Sound <select class="bm-snd">${sounds.map(s => `<option value="${esc(s.key)}"${s.key === e.sound ? " selected" : ""}>${esc(s.label)}</option>`).join("")}</select></label><button class="bm-play ghost" type="button" title="Audition sound">▶</button>` : ""}
      ${sounds.length ? `<label class="bm-ctl bm-vol-ctl">Vol <input class="bm-vol" type="range" min="0" max="2" step="0.05" value="${e.volume ?? 1.0}"><span class="bm-volv">${Math.round((e.volume ?? 1.0) * 100)}%</span></label>` : ""}
      <button class="bm-moment ghost" type="button" title="Render just this moment to review it">🎯 Render</button>
      <button class="bm-del" type="button" title="Remove">✕</button>`;
    el.querySelector(".bm-t").addEventListener("change", ev => {
      let t = parseFloat(ev.target.value); if (!isFinite(t) || t < 0) t = 0; if (dur) t = Math.min(t, dur);
      e.t = +t.toFixed(2); AL.effects.sort((a, b) => a.t - b.t); renderBrollList(); drawEffectMarkers();
    });
    const dv = el.querySelector(".bm-durv");
    el.querySelector(".bm-dur").addEventListener("input", ev => { e.dur = +ev.target.value; dv.textContent = e.dur.toFixed(2) + "s"; drawEffectMarkers(); });
    const sc = el.querySelector(".bm-scale");
    if (sc) {
      const scv = el.querySelector(".bm-scalev");
      sc.addEventListener("input", ev => { e.scale = +ev.target.value; scv.textContent = Math.round(e.scale * 100) + "%"; });
    }
    const styleSel = el.querySelector(".bm-style");
    if (styleSel) styleSel.addEventListener("change", ev => { e.style = ev.target.value; });
    const vdInp = el.querySelector(".bm-vd");
    if (vdInp) vdInp.addEventListener("input", ev => { e.vd_accent = ev.target.value; });
    el.querySelectorAll(".bm-param").forEach(inp => {
      const pk = inp.dataset.pk;
      const p = params.find(x => x.key === pk) || {};
      const out = inp.parentElement.querySelector(".bm-paramv");
      inp.addEventListener("input", ev => {
        const v = +ev.target.value;
        e.params = e.params || {};
        e.params[pk] = v;
        if (out) out.textContent = fmtParam(p, v);
      });
    });
    const snd = el.querySelector(".bm-snd");
    if (snd) {
      snd.addEventListener("change", ev => { e.sound = ev.target.value; });
      el.querySelector(".bm-play").onclick = () => {
        const s = sounds.find(x => x.key === e.sound) || sounds[0];
        if (s && s.preview) { const a = new Audio(s.preview); a.volume = Math.min(1, e.volume ?? 1.0); a.play().catch(() => {}); }
      };
    }
    const vol = el.querySelector(".bm-vol");
    if (vol) {
      const vv = el.querySelector(".bm-volv");
      vol.addEventListener("input", ev => { e.volume = +ev.target.value; vv.textContent = Math.round(e.volume * 100) + "%"; });
    }
    el.querySelector(".bm-moment").onclick = () => renderMomentAroundMarker(e);
    el.querySelector(".bm-del").onclick = () => removeEffect(i);
    box.appendChild(el);
  });
}

function drawEffectMarkers() {
  const layer = $("#al-trim-fx");
  if (!layer) return;
  layer.innerHTML = "";
  const dur = AL.duration || 0;
  if (!dur) return;
  const bar = $("#al-trim-bar");
  const { s, e: ve } = viewRange(); const span = (ve - s) || 1;
  (AL.effects || []).forEach((e, i) => {
    const cat = brollCatalog().find(c => c.key === e.type) || {};
    const dmin = cat.dur_min ?? 0.15, dmax = cat.dur_max ?? 2.0;
    const durVal = e.dur ?? cat.flash_dur ?? 0.5;
    // a clip-like bar spanning the effect's length (start = marker time t)
    const mk = document.createElement("div");
    mk.className = "trim-fx-mk";
    mk.style.left = tToPct(e.t) + "%";
    mk.style.width = Math.max(0, (durVal / span) * 100) + "%";
    mk.title = `${cat.label || e.type} · ${durVal.toFixed(2)}s @ ${e.t.toFixed(1)}s — drag to move, drag right edge to resize`;
    mk.innerHTML = `<span class="trim-fx-lb">${cat.emoji || "✨"} ${durVal.toFixed(2)}s</span><span class="trim-fx-grip" title="Drag to change length"></span>`;
    const syncRow = () => {
      const row = $(`.broll-mk:nth-child(${i + 1})`, $("#broll-list"));
      if (!row) return;
      const tin = row.querySelector(".bm-t"); if (tin) tin.value = e.t.toFixed(2);
      const din = row.querySelector(".bm-dur"); if (din) din.value = e.dur ?? durVal;
      const dv = row.querySelector(".bm-durv"); if (dv) dv.textContent = (e.dur ?? durVal).toFixed(2) + "s";
    };
    // drag the bar body → reposition the effect (keeps the grab point under cursor)
    mk.addEventListener("mousedown", ev => {
      if (ev.target.classList.contains("trim-fx-grip")) return;   // resize handled below
      ev.preventDefault(); ev.stopPropagation();
      mk.classList.add("dragging");
      const r = bar.getBoundingClientRect();
      const grab = e.t - fracToT((ev.clientX - r.left) / r.width);
      const onMove = mv => {
        const t = fracToT((mv.clientX - r.left) / r.width) + grab;
        e.t = +Math.max(0, Math.min(t, dur)).toFixed(2);
        mk.style.left = tToPct(e.t) + "%"; syncRow();
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        AL.effects.sort((a, b) => a.t - b.t); renderBrollList(); drawEffectMarkers();
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
    // drag the right edge → change the effect's length (clamped to catalog range)
    mk.querySelector(".trim-fx-grip").addEventListener("mousedown", ev => {
      ev.preventDefault(); ev.stopPropagation();
      mk.classList.add("dragging");
      const r = bar.getBoundingClientRect();
      const onMove = mv => {
        const t = fracToT((mv.clientX - r.left) / r.width);
        const d = Math.max(dmin, Math.min(t - e.t, dmax));
        e.dur = +d.toFixed(2);
        mk.style.width = Math.max(0, (e.dur / span) * 100) + "%";
        mk.querySelector(".trim-fx-lb").textContent = `${cat.emoji || "✨"} ${e.dur.toFixed(2)}s`;
        syncRow();
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
    mk.addEventListener("click", ev => ev.stopPropagation());   // don't seek the scrubber
    layer.appendChild(mk);
  });
}

function initBroll() {
  renderBrollTray();
  const bar = $("#al-trim-bar");
  if (!bar) return;
  bar.addEventListener("dragover", e => {
    if (e.dataTransfer.types.includes("text/broll")) { e.preventDefault(); bar.classList.add("fx-dragover"); }
  });
  bar.addEventListener("dragleave", () => bar.classList.remove("fx-dragover"));
  bar.addEventListener("drop", e => {
    const type = e.dataTransfer.getData("text/broll");
    bar.classList.remove("fx-dragover");
    if (!type) return;
    e.preventDefault();
    const r = bar.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    addEffect(type, fracToT(frac));
  });
}

// ============================================================= //
// Render one moment — highlight a span on the scrubber (or render
// around a B-roll marker) and render just that into the popup player.
// ============================================================= //
function momentControlsSync() {
  const on = AL.moment.on;
  ["al-moment-in", "al-moment-out", "al-moment-render", "al-moment-clear"].forEach(id => {
    const b = $("#" + id); if (b) b.classList.toggle("hidden", !on);
  });
  const set = $("#al-moment-set"); if (set) set.classList.toggle("hidden", on);
  const info = $("#al-moment-info");
  if (info) info.textContent = on
    ? `moment: ${AL.moment.start.toFixed(1)}–${AL.moment.end.toFixed(1)}s (${Math.max(0, AL.moment.end - AL.moment.start).toFixed(1)}s)`
    : "";
}
function drawMomentBand() {
  const band = $("#al-trim-moment");
  if (!band) return;
  if (!AL.moment.on || !(AL.duration > 0)) { band.classList.add("hidden"); return; }
  band.classList.remove("hidden");
  const l = tToPct(AL.moment.start), r = tToPct(AL.moment.end);
  band.style.left = l + "%";
  band.style.width = Math.max(0, r - l) + "%";
}
function momentSetAtPlayhead() {
  if (!selectedClip) return toast("Load a clip first", true);
  const dur = AL.duration || 0;
  const t = $("#al-video").currentTime || 0;
  const half = 1.25;
  AL.moment.start = Math.max(0, t - half);
  AL.moment.end = Math.min(dur || (t + half), t + half);
  AL.moment.on = true;
  momentControlsSync(); drawMomentBand();
}
function momentInAtPlayhead() {
  const t = $("#al-video").currentTime || 0;
  AL.moment.start = Math.max(0, Math.min(t, AL.moment.end - 0.2));
  momentControlsSync(); drawMomentBand();
}
function momentOutAtPlayhead() {
  const dur = AL.duration || 0;
  const t = $("#al-video").currentTime || 0;
  AL.moment.end = Math.min(dur || t, Math.max(t, AL.moment.start + 0.2));
  momentControlsSync(); drawMomentBand();
}
function momentClear() { AL.moment.on = false; momentControlsSync(); drawMomentBand(); }

// resolve a render job to its output URL (moment renders play in the modal)
function pollJobUrl(job) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const j = await api("/api/jobs/" + job);
        if (j.status === "done") return resolve(j.output);
        if (j.status === "error") return reject(new Error(j.error || "render error"));
        setTimeout(tick, 800);
      } catch (e) { reject(e); }
    };
    tick();
  });
}
async function renderMoment(start, end, title, statusEl) {
  if (!selectedClip) return toast("Load a clip first", true);
  const info = statusEl || $("#al-moment-info");
  const prev = info ? info.textContent : "";
  if (info) info.textContent = "Rendering moment…";
  try {
    const body = Object.assign(buildOpts(), { moment_start: start, moment_end: end });
    const { job } = await api("/api/autolayout/moment", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const url = await pollJobUrl(job);
    if (info) info.textContent = prev;
    openPlayer(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now(), title || "Moment");
  } catch (e) {
    toast("Moment render failed: " + e.message, true);
    if (info) info.textContent = prev;
  }
}
// render a short window centred on one B-roll marker (to eyeball the punch)
function renderMomentAroundMarker(e) {
  const dur = AL.duration || 0;
  const flash = e.dur ?? 0.6;
  const start = Math.max(0, e.t - 1.0);
  const end = Math.min(dur || (e.t + flash + 1.2), e.t + flash + 1.2);
  renderMoment(start, end, `B-roll @ ${e.t.toFixed(1)}s`);
}
function initMoment() {
  const on = (id, fn) => { const b = $("#" + id); if (b) b.onclick = fn; };
  on("al-moment-set", momentSetAtPlayhead);
  on("al-moment-in", momentInAtPlayhead);
  on("al-moment-out", momentOutAtPlayhead);
  on("al-moment-clear", momentClear);
  on("al-moment-render", () => {
    if (AL.moment.end < AL.moment.start) { const t = AL.moment.start; AL.moment.start = AL.moment.end; AL.moment.end = t; }
    renderMoment(AL.moment.start, AL.moment.end, "Moment");
  });
  initMomentDrag();
  momentControlsSync();
}
function initMomentDrag() {
  const band = $("#al-trim-moment"), bar = $("#al-trim-bar");
  if (!band || !bar) return;
  const fracAt = cx => { const r = bar.getBoundingClientRect(); return Math.max(0, Math.min(1, (cx - r.left) / r.width)); };
  $$(".trim-moment-h", band).forEach(h => h.addEventListener("mousedown", ev => {
    ev.preventDefault(); ev.stopPropagation();
    const edge = h.dataset.edge;
    const onMove = mv => {
      const t = fracToT(fracAt(mv.clientX));
      if (edge === "start") AL.moment.start = Math.max(0, Math.min(t, AL.moment.end - 0.2));
      else AL.moment.end = Math.min(AL.duration || t, Math.max(t, AL.moment.start + 0.2));
      drawMomentBand(); momentControlsSync();
    };
    const onUp = () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove); window.addEventListener("mouseup", onUp);
  }));
  band.addEventListener("mousedown", ev => {
    if (ev.target.classList.contains("trim-moment-h")) return;
    ev.preventDefault(); ev.stopPropagation();
    band.classList.add("dragging");
    const dur = AL.duration || 0;
    const len = AL.moment.end - AL.moment.start;
    const grab = AL.moment.start - fracToT(fracAt(ev.clientX));
    const onMove = mv => {
      let s = fracToT(fracAt(mv.clientX)) + grab;
      s = Math.max(0, Math.min(s, (dur || (s + len)) - len));
      AL.moment.start = s; AL.moment.end = s + len;
      drawMomentBand(); momentControlsSync();
    };
    const onUp = () => { band.classList.remove("dragging"); window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove); window.addEventListener("mouseup", onUp);
  });
  band.addEventListener("click", ev => ev.stopPropagation());   // don't seek the scrubber
}

// ============================================================= //
// Clip packages — the agent's deliveries (notification, preview, select, critique)
// ============================================================= //
async function refreshPkgBadge() {
  try {
    const d = await api("/api/packages");
    AL.pkg.unseen = d.unseen || 0;
    const badge = $("#pkg-badge");
    badge.textContent = AL.pkg.unseen;
    badge.classList.toggle("hidden", !AL.pkg.unseen);
    return d;
  } catch { return null; }
}

function openPackages() {
  $("#pkg-modal").classList.remove("hidden");
  showPkgList();
}
function closePackages() { $("#pkg-modal").classList.add("hidden"); AL.pkg.cur = null; }
function showPkgList() {
  $("#pkg-detail-view").classList.add("hidden");
  $("#pkg-list-view").classList.remove("hidden");
  $("#pkg-ctx").textContent = "";
  loadPkgList();
}

async function loadPkgList() {
  const box = $("#pkg-list");
  box.innerHTML = `<div class="hint">Loading…</div>`;
  const d = await refreshPkgBadge();
  if (!d) { box.innerHTML = `<div class="hint">Failed to load packages.</div>`; return; }
  $("#pkg-count").value = d.agent?.default_count || 20;
  if (!d.packages.length) { box.innerHTML = `<div class="hint">No packages yet — request one above.</div>`; return; }
  box.innerHTML = "";
  d.packages.forEach(s => box.appendChild(pkgCard(s)));
  // keep polling while any package is still building
  if (d.packages.some(s => s.status === "building")) setTimeout(() => { if (!$("#pkg-list-view").classList.contains("hidden")) loadPkgList(); }, 2500);
}

function pkgCard(s) {
  const el = document.createElement("div");
  el.className = "result pkg-card" + (s.status === "building" ? " building" : "");
  const when = s.created ? new Date(s.created * 1000).toLocaleDateString() : "";
  const status = s.status === "building" ? "⏳ building…" : s.status === "error" ? "⚠ failed" : `${s.count} clips`;
  el.innerHTML = `
    <img src="${esc(s.thumb || "")}" onerror="this.style.visibility='hidden'"/>
    <div class="meta">
      <div class="title">${esc(s.title || "Package")}${!s.seen && s.status === "ready" ? `<span class="pkg-dot">•</span>` : ""}</div>
      <div class="sub">
        <span class="pkg-status">${status}</span>
        ${s.source === "agent" ? `<span class="ch">agent-curated</span>` : ""}
        ${s.critiques ? `<span class="vc">${s.critiques} critique${s.critiques === 1 ? "" : "s"}</span>` : ""}
        <span class="ch">${when}</span>
      </div>
    </div>
    <div class="res-actions"><button class="res-get">${s.status === "ready" ? "Open →" : "View"}</button></div>`;
  const open = () => openPkgDetail(s.id);
  el.querySelector(".res-get").onclick = open;
  el.onclick = e => { if (!e.target.closest(".res-get")) open(); };
  return el;
}

async function openPkgDetail(pid) {
  $("#pkg-list-view").classList.add("hidden");
  $("#pkg-detail-view").classList.remove("hidden");
  $("#pkg-clips").innerHTML = `<div class="hint">Loading…</div>`;
  let pkg;
  try { pkg = await api(`/api/packages/${pid}`); }
  catch (e) { $("#pkg-clips").innerHTML = `<div class="hint">Error: ${e.message}</div>`; return; }
  AL.pkg.cur = pkg; AL.pkg.sel = new Set();
  // mark it seen → clears its badge contribution
  if (!pkg.seen) { api(`/api/packages/${pid}/seen`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seen: true }) }).then(refreshPkgBadge); }
  $("#pkg-detail-title").textContent = pkg.title || "Package";
  $("#pkg-detail-note").textContent = pkg.note || "";
  $("#pkg-ctx").textContent = `→ ${pkg.prompt || ""}`;
  renderPkgClips();
  renderPkgCritiques();
}

function renderPkgClips() {
  const pkg = AL.pkg.cur, box = $("#pkg-clips");
  box.innerHTML = "";
  if (!pkg.clips.length) { box.innerHTML = `<div class="hint">${pkg.status === "building" ? "Still assembling…" : "No clips."}</div>`; return; }
  pkg.clips.forEach(c => box.appendChild(pkgClipRow(c)));
  updatePkgSelCount();
}

function pkgClipRow(c) {
  const el = document.createElement("div");
  el.className = "result pkg-clip" + (AL.pkg.sel.has(c.id) ? " on" : "");
  const dur = fmtDur(c.duration), views = fmtViews(c.view_count);
  const src = c.source === "twitch" ? "Twitch" : "YT";
  el.innerHTML = `
    <input type="checkbox" class="pkg-check"${AL.pkg.sel.has(c.id) ? " checked" : ""}>
    <img src="${esc(c.thumbnail || "")}" class="pkg-play" onerror="this.style.visibility='hidden'"/>
    <div class="meta">
      <div class="title">${esc(c.title || "(untitled)")}</div>
      <div class="sub">
        <span class="src src-${c.source}">${src}</span>
        ${dur ? `<span class="dur dur-short">${dur}</span>` : ""}
        ${c.channel ? `<span class="ch">${esc(c.channel)}</span>` : ""}
        ${views ? `<span class="vc">${views} views</span>` : ""}
        ${c.downloaded ? `<span class="ch">✓ grabbed</span>` : ""}
      </div>
      ${c.reason ? `<div class="pkg-reason">${esc(c.reason)}</div>` : ""}
    </div>`;
  const chk = el.querySelector(".pkg-check");
  const toggle = () => { if (AL.pkg.sel.has(c.id)) AL.pkg.sel.delete(c.id); else AL.pkg.sel.add(c.id); chk.checked = AL.pkg.sel.has(c.id); el.classList.toggle("on", chk.checked); updatePkgSelCount(); };
  chk.onclick = e => { e.stopPropagation(); toggle(); };
  // preview: downloaded → local file; else resolve a stream from the source URL
  el.querySelector(".pkg-play").onclick = () => pkgPreviewClip(c);
  el.querySelector(".meta").onclick = toggle;
  return el;
}

async function pkgPreviewClip(c) {
  if (c.downloaded && c.clip_path) {
    return openPlayer(`/storage/clips/${c.clip_path}`, c.title);
  }
  // resolve a progressive stream URL (reuses the Nb1 stream resolver)
  toast("Resolving preview…");
  try {
    const r = await api("/api/nb1/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: c.url, max_height: 720 }) });
    openPlayer(r.url, c.title || r.title);
  } catch (e) { toast("Can't preview this source — grab it first. (" + e.message + ")", true); }
}

function updatePkgSelCount() {
  const n = AL.pkg.sel.size, total = (AL.pkg.cur?.clips || []).length;
  $("#pkg-selcount").textContent = `${n} selected`;
  $("#pkg-selall").checked = n > 0 && n === total;
  $("#pkg-open").textContent = `Open ${n || "selected"} in Formatter →`;
}

function renderPkgCritiques() {
  const pkg = AL.pkg.cur, box = $("#pkg-crit-list");
  const crits = pkg.critiques || [];
  box.innerHTML = crits.length
    ? crits.map(c => `<div class="pkg-crit-item">${esc(c.text)}</div>`).join("")
    : `<div class="hint">No critiques yet. Tell the agent what to change and it'll steer future packages.</div>`;
}

async function pkgAddCritique() {
  const inp = $("#pkg-crit-in"), text = inp.value.trim();
  if (!text || !AL.pkg.cur) return;
  try {
    const r = await api(`/api/packages/${AL.pkg.cur.id}/critique`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    AL.pkg.cur.critiques = r.critiques; inp.value = "";
    renderPkgCritiques();
    toast("Critique saved — the agent will learn from it");
  } catch (e) { toast("Failed: " + e.message, true); }
}

async function pkgOpenInFormatter() {
  const pkg = AL.pkg.cur;
  if (!pkg) return;
  const ids = AL.pkg.sel.size ? [...AL.pkg.sel] : [];
  const picked = ids.length ? pkg.clips.filter(c => ids.includes(c.id)) : pkg.clips;
  if (!picked.length) return toast("Select at least one clip", true);
  if (picked.length > 10) toast(`Using the first 10 of ${picked.length} clips (Formatter caps at 10)`);
  const btn = $("#pkg-open");
  btn.disabled = true; btn.textContent = "Grabbing clips…";
  try {
    const { job } = await api(`/api/packages/${pkg.id}/select`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_ids: ids }),
    });
    const res = await pollPackageJob(job, btn);
    if (!res) return;
    loadClipsIntoFormatter(res.clips || []);
    if ((res.failed || []).length) toast(`${res.failed.length} clip(s) couldn't be grabbed`, true);
    closePackages();
  } catch (e) { toast("Failed: " + e.message, true); }
  finally { btn.disabled = false; updatePkgSelCount(); }
}

function pollPackageJob(job, btn) {
  return new Promise(resolve => {
    const tick = async () => {
      try {
        const j = await api("/api/jobs/" + job);
        if (j.status === "running" || j.status === "queued") { btn.textContent = "Downloading…"; setTimeout(tick, 1500); }
        else if (j.status === "done") resolve(j);
        else { toast("Grab error: " + (j.error || "unknown"), true); resolve(null); }
      } catch (e) { toast(e.message, true); resolve(null); }
    };
    tick();
  });
}

async function loadClipsIntoFormatter(clips) {
  if (!clips.length) return;
  await loadLibrary();                         // refresh AL.libClips so thumbs resolve
  const use = clips.slice(0, 10);
  // size the ranks to the number of clips, then assign each clip + a label
  AL.fmt.ranks = use.map((c, i) => ({
    label: c.title ? c.title.slice(0, 40) : `Rank ${i + 1}`,
    clip: c.path, layout: { clip: c.path },
    join: { visual: "cut", sound: "none", volume: 1.0 }, playPos: i + 1,
  }));
  seedPlayPositions();
  selectTab("formatter", AL.tabBtns["formatter"]);
  toast(`Loaded ${use.length} clips into the Formatter`);
}

async function pkgRequest() {
  const prompt = $("#pkg-prompt").value.trim();
  const count = +$("#pkg-count").value || 20;
  if (!prompt) return toast("Describe the package you want", true);
  const btn = $("#pkg-request");
  btn.disabled = true; btn.textContent = "Requesting…";
  try {
    await api("/api/packages/request", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, count }) });
    $("#pkg-prompt").value = "";
    toast("Agent is assembling your package…");
    loadPkgList();
  } catch (e) { toast("Failed: " + e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Request package"; }
}

async function pkgDelete() {
  if (!AL.pkg.cur || !confirm(`Delete "${AL.pkg.cur.title}"?`)) return;
  try { await api(`/api/packages/${AL.pkg.cur.id}`, { method: "DELETE" }); showPkgList(); refreshPkgBadge(); }
  catch (e) { toast("Failed: " + e.message, true); }
}

function initPackages() {
  $("#pkg-btn").onclick = openPackages;
  $("#pkg-modal-close").onclick = closePackages;
  $("#pkg-modal").addEventListener("click", e => { if (e.target.id === "pkg-modal") closePackages(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#pkg-modal").classList.contains("hidden")) closePackages();
  });
  $("#pkg-back").onclick = showPkgList;
  $("#pkg-request").onclick = pkgRequest;
  $("#pkg-prompt").addEventListener("keydown", e => { if (e.key === "Enter") pkgRequest(); });
  $("#pkg-del").onclick = pkgDelete;
  $("#pkg-open").onclick = pkgOpenInFormatter;
  $("#pkg-crit-add").onclick = pkgAddCritique;
  $("#pkg-crit-in").addEventListener("keydown", e => { if (e.key === "Enter") pkgAddCritique(); });
  $("#pkg-selall").onchange = e => {
    const all = e.target.checked, pkg = AL.pkg.cur;
    if (!pkg) return;
    AL.pkg.sel = all ? new Set(pkg.clips.map(c => c.id)) : new Set();
    renderPkgClips();
  };
  refreshPkgBadge();
  setInterval(refreshPkgBadge, 30000);          // pick up newly-delivered packages
}

function init() {
  wireEmojiButtons();
  // deep-linkable tabs; "#search:experimental" also selects a sub-tab
  initTabs().then(() => {
    const [tab, sub] = (location.hash.slice(1) || "valorant").split(":");
    selectTab(tab || "valorant");
    if (tab === "search" && sub) {
      selectSubTab($('[data-niche="search"]'), sub);
      if (sub === "experimental") xInit("x", xGetToLibrary);
    }
  });
  loadLibrary();
  initFx();
  initFormatter();
  initNb1();
  initFmtMusic();
  initFmtSub();
  initFmtHook();
  initPlayer();
  initBroll();
  initMoment();
  initPackages();
  initBoxDrag("facecam");
  initBoxDrag("mousecam");
  initMcGhostDrag();
  window.addEventListener("resize", drawAllBoxes);

  $("#v-search").onclick = doSearch;
  $("#v-query").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
  $("#v-creator").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
  $("#v-sort").addEventListener("change", renderResults);   // re-sort in place, no re-fetch
  $("#v-twitch").addEventListener("change", () => { if (lastResults.length) doSearch(); });
  $("#v-file").onchange = e => doImport(e.target.files[0]);
  $$("#search-subtabs .subtab").forEach(b =>
    b.onclick = () => {
      selectSubTab($('[data-niche="search"]'), b.dataset.sub);
      if (b.dataset.sub === "experimental") xInit("x", xGetToLibrary);
    });

  $("#al-detect").onclick = detectFacecam;
  $("#al-refresh").onclick = refreshPreview;
  $("#al-render").onclick = render;
  $("#al-save-slot").onclick = saveSlotFromEditor;
  $("#al-cancel-slot").onclick = cancelSlotEditor;

  // coordinate inputs
  ["al-x","al-y","al-w","al-h"].forEach(id =>
    $("#"+id).addEventListener("change", () => { readCoords("facecam"); refreshPreview(); }));
  ["al-mc-x","al-mc-y","al-mc-w","al-mc-h"].forEach(id =>
    $("#"+id).addEventListener("change", () => { readCoords("mousecam"); refreshPreview(); }));

  // toggles
  ["al-fc-on","al-mc-on","al-edges-on"].forEach(id =>
    $("#"+id).addEventListener("change", () => { syncBoxVisibility(); refreshPreview(); }));
  // Lock facecam height: on enable, seed the slider with the current auto height
  // (so the look doesn't jump and you can read the value to copy onto other clips).
  $("#al-fc-lock").addEventListener("change", e => {
    if (e.target.checked) {
      const p = autoFcHeightPct();
      $("#al-fc-h").value = p; $("#al-fc-h-val").textContent = p;
    }
    syncBoxVisibility(); refreshPreview();
  });
  $("#al-mc-pos").addEventListener("change", e => {
    AL.mcPos = mcCornerToPos(e.target.value);   // corner = quick preset for the draggable overlay
    drawMcGhost(); refreshPreview();
  });

  // trim start/end — editing a field also seeks the player to that bound
  $("#al-trim-start").addEventListener("change", () => {
    updateTrimInfo(); applyAutoZoom(); drawTrimBar();
    $("#al-video").currentTime = readTrim().start; refreshPreview();
  });
  $("#al-trim-end").addEventListener("change", () => {
    updateTrimInfo(); applyAutoZoom(); drawTrimBar();
    const end = readTrim().end;
    if (end !== null) $("#al-video").currentTime = end;
    refreshPreview();
  });
  $("#al-trim-set-start").onclick = () => {
    const v = $("#al-video");
    $("#al-trim-start").value = (v.currentTime || 0).toFixed(1);
    updateTrimInfo(); applyAutoZoom(); drawTrimBar(); refreshPreview();
  };
  $("#al-trim-set-end").onclick = () => {
    const v = $("#al-video");
    $("#al-trim-end").value = (v.currentTime || 0).toFixed(1);
    updateTrimInfo(); applyAutoZoom(); drawTrimBar(); refreshPreview();
  };
  $("#al-trim-play").onclick = playSelection;
  $("#al-trim-full").onclick = () => {
    $("#al-trim-start").value = 0;
    $("#al-trim-end").value = AL.duration ? AL.duration.toFixed(1) : "";
    AL.zoomFull = false;
    updateTrimInfo(); applyAutoZoom(); drawTrimBar();
    refreshPreview();
  };
  $("#al-trim-zoom").onclick = () => {
    AL.zoomFull = !!AL.view;   // currently zoomed -> show full; currently full -> zoom in
    applyAutoZoom(); drawTrimBar();
  };
  initTrimBar();
  window.addEventListener("resize", drawTrimBar);

  // sliders (label + debounced preview)
  $("#al-bar").addEventListener("input", e => { $("#al-bar-val").textContent = e.target.value; refreshPreview(); });
  $("#al-fc-h").addEventListener("input", e => { $("#al-fc-h-val").textContent = e.target.value; refreshPreview(); });
  $("#al-mc-scale").addEventListener("input", e => { $("#al-mc-scale-val").textContent = e.target.value; clampMcPos(); drawMcGhost(); refreshPreview(); });

  // speed / cuts / audio-fade — temporal edits (no still-frame change, just length)
  $("#al-speed").addEventListener("input", e => {
    $("#al-speed-val").textContent = (+e.target.value) + "×";
    updateLengthInfo();
  });
  $("#al-cut-add").onclick = addCut;
  $("#al-cut-start").onclick = markCutStart;
  $("#al-cut-end").onclick = markCutEnd;
  $("#al-fade-on").addEventListener("change", () =>
    $("#ctrl-fade").classList.toggle("hidden", !$("#al-fade-on").checked));
  $("#al-fade-dur").addEventListener("input", e => { $("#al-fade-dur-val").textContent = e.target.value; });
}
init();
