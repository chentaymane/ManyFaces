// ManyFaces dashboard
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
let editingId = null;      // null => creating new
let proxyMode = "manual";  // manual | random | rotate
let allProfiles = [];      // cache for client-side search

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const detail = Array.isArray(d)
      ? d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ")
      : (d && typeof d === "object" ? d.message : d);
    const err = new Error(detail || res.statusText);
    if (d && typeof d === "object" && !Array.isArray(d)) err.detail = d;  // structured errors (e.g. display_fit)
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(msg, kind = "ok") {
  const t = $("#toast");
  const dialog = $(".modal[open]");
  const container = dialog || document.body;
  if (t.parentElement !== container) container.append(t);
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toastTimer);  // an older toast's timer must not hide this one early
  toastTimer = setTimeout(() => t.classList.add("hidden"), kind === "err" ? 6000 : 3200);
}

// Save a JS value as a downloaded .json file.
function downloadJSON(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function engineBadge(engine) {
  const meta = {
    chromium: { cls: "chromium", label: "Chromium", title: "Chromium engine" },
    android: { cls: "android", label: "Android", title: "Android emulator" },
  }[engine] || { cls: "camoufox", label: "Camoufox", title: "Camoufox (Firefox) engine" };
  return `<span class="badge engine ${meta.cls}" title="${meta.title}">${meta.label}</span>`;
}

// ------------------------------------------------------------- render list ---
function deviceCell(fp) {
  if (fp.is_mobile) {
    return esc(fp.device_name || "Phone");
  }
  return esc({ windows: "Windows", macos: "macOS", linux: "Linux" }[fp.os] || fp.os);
}

function proxyCell(p) {
  if (p.proxy_mode && p.proxy_mode !== "manual") {
    const n = (p.proxy_pool || []).length;
    return `<span class="badge mode">${p.proxy_mode}</span> <span class="sub">${n} prox${n === 1 ? "y" : "ies"}</span>`;
  }
  if (p.proxy && p.proxy.host) {
    return `<span class="mono">${esc(p.proxy.type)}://${esc(p.proxy.host)}:${p.proxy.port}</span>`;
  }
  return `<span class="sub">Direct connection</span>`;
}

// selection + filter state (the "select area")
const selected = new Set();
let activeFilter = "all";
let sortOrder = "newest";
try {
  const saved = JSON.parse(localStorage.getItem("manyfaces-view") || "{}");
  if (["all", "running", "stopped", "error", "android", "chromium", "camoufox"].includes(saved.filter)) activeFilter = saved.filter;
  if (["newest", "oldest", "name", "updated"].includes(saved.sort)) sortOrder = saved.sort;
} catch (_) { /* local storage can be disabled */ }
function saveView() {
  try { localStorage.setItem("manyfaces-view", JSON.stringify({ filter: activeFilter, sort: sortOrder })); } catch (_) {}
}

const launchErrors = {};   // profile id -> last launch error message

// Host display facts for headful launches: fit_limit is the workarea (monitor px)
// a profile's window must open in here (null when it doesn't matter).
let displayInfo = { fit_limit: null, virtual_available: false };
api("/api/display").then((d) => { displayInfo = d; syncFitRows(); }).catch(() => {});

const isPhoneOs = (os) => os === "android" || os === "ios";
function syncFitRows() {
  const can = !!displayInfo.fit_limit;
  $("#f-fit-row").classList.toggle("hidden-block", !can || !!editingId || $("#f-engine").value !== "camoufox" || isPhoneOs($("#f-os").value));
  $("#bulk-fit-row").classList.toggle("hidden-block", !can || $("#bulk-engine").value !== "camoufox" || isPhoneOs($("#bulk-os").value));
}

function statusPill(p) {
  const st = p.status || (p.running ? "running" : "stopped");
  if (st === "launching") return `<span class="pill launching"><span class="spin"></span>starting…</span>`;
  if (st === "running") return `<span class="pill on"><span class="dot"></span>running</span>`;
  if (st === "error") return `<button class="pill err" data-err="${p.id}" aria-label="Show launch error for ${esc(p.name)}">Failed</button>`;
  return `<span class="pill off"><span class="dot"></span>stopped</span>`;
}

function renderList(profiles) {
  const body = $("#profiles-body");
  if (!profiles.length) {
    const msg = allProfiles.length
      ? `<strong>No profiles match</strong><p>Try a different search or reset the filters.</p><button id="clear-filter">Reset filters</button>`
      : `<strong>No profiles yet</strong><p>Create a profile to save browser settings and session data.</p><button class="primary" id="empty-create">Create profile</button>`;
    body.innerHTML = `<tr><td colspan="7" class="empty">${msg}</td></tr>`;
    const cf = $("#clear-filter");
    if (cf) cf.onclick = () => { activeFilter = "all"; $("#search").value = ""; saveView(); renderFilters(); applySearch(); $("#search").focus(); };
    $("#empty-create")?.addEventListener("click", () => openEditor(null));
    syncBulkBar();
    return;
  }
  body.innerHTML = profiles.map((p) => {
    const fp = p.fingerprint;
    const sel = selected.has(p.id);
    if (p.launch_error) launchErrors[p.id] = p.launch_error; else if (p.status !== "error") delete launchErrors[p.id];
    const st = p.status || (p.running ? "running" : "stopped");
    const toggle = (st === "running" || st === "launching")
      ? `<button class="sm" data-act="stop" data-id="${p.id}">Stop</button>`
      : `<button class="sm primary" data-act="start" data-id="${p.id}">Launch</button>`;
    return `<tr class="${sel ? "sel" : ""}">
      <td class="col-check"><input type="checkbox" class="row-check" aria-label="Select ${esc(p.name)}" data-id="${p.id}" ${sel ? "checked" : ""} /></td>
      <td class="profile-cell"><div class="name" title="${esc(p.notes || "")}">${esc(p.name)}</div><div class="sub">${(p.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}${fp.screen_width} × ${fp.screen_height}</div></td>
      <td class="device-cell">${deviceCell(fp)}<div class="sub">${engineBadge(p.engine)}</div></td>
      <td class="proxy-cell">${proxyCell(p)}</td>
      <td class="locale-cell"><div>${esc(fp.language)}</div><div class="sub">${esc(fp.timezone)}</div></td>
      <td class="status-cell">${statusPill(p)}</td>
      <td class="actions-cell"><div class="actions">
        ${toggle}
        <button class="sm" data-act="edit" data-id="${p.id}" ${p.running ? 'disabled title="Stop this profile before editing"' : ""}>Edit</button>
        <details class="more-actions">
          <summary aria-label="More actions for ${esc(p.name)}">More</summary>
          <div class="action-list">
            <button data-act="cookies" data-id="${p.id}">Manage cookies</button>
            <button data-act="clone" data-id="${p.id}">Clone profile</button>
            <button data-act="randomize" data-id="${p.id}" ${p.running ? 'disabled title="Stop this profile before randomizing"' : ""}>Randomize fingerprint and cookies</button>
            <button class="danger" data-act="delete" data-id="${p.id}">Delete profile</button>
          </div>
        </details>
      </div></td>
    </tr>`;
  }).join("");
  syncBulkBar();
}

function currentView() {
  const q = $("#search").value.trim().toLowerCase();
  return allProfiles.filter((p) => {
    const fp = p.fingerprint;
    if (activeFilter === "running" && !p.running) return false;
    if (activeFilter === "stopped" && (p.status || "stopped") !== "stopped") return false;
    if (activeFilter === "error" && p.status !== "error") return false;
    if (["android", "chromium", "camoufox"].includes(activeFilter) && p.engine !== activeFilter) return false;
    if (!q) return true;
    return [p.name, fp.os, fp.device_name, fp.language, fp.timezone, p.proxy?.host, p.notes, ...(p.tags || [])]
      .join(" ").toLowerCase().includes(q);
  }).sort((a, b) => {
    if (sortOrder === "name") return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }) || a.id.localeCompare(b.id);
    const field = sortOrder === "updated" ? "updated_at" : "created_at";
    const result = (a[field] || "").localeCompare(b[field] || "");
    return (sortOrder === "oldest" ? result : -result) || a.id.localeCompare(b.id);
  });
}

function applySearch() {
  const profiles = currentView();
  const total = allProfiles.length;
  const count = profiles.length === total ? `${total}` : `${profiles.length} of ${total}`;
  $("#profile-count").textContent = `${count} profile${total === 1 ? "" : "s"}`;
  renderList(profiles);
}

function renderFilters() {
  const total = allProfiles.length;
  const running = allProfiles.filter((p) => p.running).length;
  const byEngine = {};
  allProfiles.forEach((p) => (byEngine[p.engine] = (byEngine[p.engine] || 0) + 1));
  const chips = [["all", "All", total], ["running", "Running", running],
    ["stopped", "Stopped", allProfiles.filter((p) => p.status === "stopped").length],
    ["error", "Failed", allProfiles.filter((p) => p.status === "error").length],
    ["android", "Android", byEngine.android || 0], ["chromium", "Chromium", byEngine.chromium || 0],
    ["camoufox", "Camoufox", byEngine.camoufox || 0]];
  $("#filter-chips").innerHTML = chips
    .filter((c) => ["all", "running", "stopped", "error", activeFilter].includes(c[0]) || c[2] > 0)
    .map(([k, label, n]) => `<button class="chip" aria-pressed="${activeFilter === k}" data-filter="${k}">${label} <span>${n}</span></button>`).join("");
}

function syncBulkBar() {
  const n = selected.size;
  $("#bulk-bar").classList.toggle("hidden", n === 0);
  $("#bulk-count").textContent = `${n} selected`;
  const view = currentView();
  const allSel = view.length > 0 && view.every((p) => selected.has(p.id));
  $("#head-check").checked = allSel;
  $("#bulk-master").checked = allSel;
  const partial = !allSel && view.some((p) => selected.has(p.id));
  $("#head-check").indeterminate = partial;
  $("#bulk-master").indeterminate = partial;
}

function toggleSelectAll(on) {
  currentView().forEach((p) => (on ? selected.add(p.id) : selected.delete(p.id)));
  applySearch();
}

let profilesLoading = false;
let lastProfilesJSON = "";
async function loadProfiles() {
  if (profilesLoading) return;
  profilesLoading = true;
  try {
    const profiles = await api("/api/profiles");
    const snapshot = JSON.stringify(profiles);
    if (snapshot === lastProfilesJSON) return;
    lastProfilesJSON = snapshot;
    allProfiles = profiles;
    // Drop selections for profiles that no longer exist.
    const ids = new Set(allProfiles.map((p) => p.id));
    [...selected].forEach((id) => { if (!ids.has(id)) selected.delete(id); });
    renderFilters();
    applySearch();
    if (allProfiles.some((p) => p.status === "launching")) ensurePolling();
  } catch (e) {
    lastProfilesJSON = "";
    $("#profiles-body").innerHTML = `<tr><td colspan="7" class="empty"><strong>Could not load profiles</strong><p>${esc(e.message)}</p><button id="retry-profiles">Try again</button></td></tr>`;
    $("#retry-profiles").addEventListener("click", loadProfiles);
  } finally { profilesLoading = false; }
}

$("#sort-order").value = sortOrder;
$("#sort-order").addEventListener("change", (e) => { sortOrder = e.target.value; saveView(); applySearch(); });
$("#search").addEventListener("input", applySearch);
$("#head-check").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
$("#bulk-master").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
$("#filter-chips").addEventListener("click", (e) => {
  const b = e.target.closest("[data-filter]"); if (!b) return;
  activeFilter = b.dataset.filter;
  saveView();
  $$("#filter-chips button").forEach((chip) => chip.setAttribute("aria-pressed", chip === b));
  applySearch();
});
$("#profiles-body").addEventListener("change", (e) => {
  const cb = e.target.closest(".row-check"); if (!cb) return;
  if (cb.checked) selected.add(cb.dataset.id); else selected.delete(cb.dataset.id);
  cb.closest("tr").classList.toggle("sel", cb.checked);
  syncBulkBar();
});
$("#bulk-bar").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-bulk]"); if (!b) return;
  const act = b.dataset.bulk;
  if (act === "clear") { selected.clear(); applySearch(); return; }
  const ids = [...selected];
  if (!ids.length) return;
  if (act === "export") {
    try {
      const data = await api(`/api/profiles-export?ids=${encodeURIComponent(ids.join(","))}`);
      downloadJSON(`manyfaces-${ids.length}-profiles.json`, data);
      toast(`Exported ${ids.length} profile(s)`);
    } catch (err) { toast(err.message, "err"); }
    return;
  }
  if (act === "delete" && !confirm(`Delete ${ids.length} profile(s) and all their data?`)) return;
  if (act === "randomize" && !confirm(`Regenerate fingerprint + cookies for ${ids.length} profile(s)?`)) return;
  b.disabled = true; const lbl = b.textContent; b.textContent = "…";
  let ok = 0, fail = 0, fitErr = null;
  for (const id of ids) {
    try {
      if (act === "launch") await api(`/api/profiles/${id}/start`, { method: "POST" });
      else if (act === "stop") await api(`/api/profiles/${id}/stop`, { method: "POST" });
      else if (act === "delete") { await api(`/api/profiles/${id}`, { method: "DELETE" }); selected.delete(id); }
      else if (act === "randomize") await api(`/api/profiles/${id}/randomize-all`, { method: "POST", body: JSON.stringify({ seed_cookies: 15 }) });
      ok++;
    } catch (err) {
      fail++;
      if (act === "launch" && !fitErr && err.detail && err.detail.code === "display_fit") fitErr = [id, err.detail];
    }
  }
  b.disabled = false; b.textContent = lbl;
  toast(`${act}: ${ok} done${fail ? `, ${fail} failed` : ""}`, fail ? "err" : "ok");
  if (fitErr) openFit(...fitErr);  // explain the first profile that can't fit this display
  loadProfiles();
});

// -------------------------------------------------------------- row actions ---
$("#profiles-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  const menu = btn.closest(".more-actions");
  if (menu) {
    menu.open = false;
    menu.querySelector("summary").focus();
  }
  try {
    if (act === "start") {
      btn.textContent = "Starting…"; btn.disabled = true;
      delete launchErrors[id];
      const r = await api(`/api/profiles/${id}/start`, { method: "POST" });
      toast(r.proxy ? `Launching · ${r.proxy}` : "Launching…");
      ensurePolling();
    } else if (act === "stop") {
      await api(`/api/profiles/${id}/stop`, { method: "POST" });
      toast("Profile stopped");
    } else if (act === "delete") {
      if (!confirm("Delete this profile and all its data?")) return;
      await api(`/api/profiles/${id}`, { method: "DELETE" });
      toast("Deleted");
    } else if (act === "clone") {
      await api(`/api/profiles/${id}/clone`, { method: "POST" });
      toast("Cloned");
    } else if (act === "randomize") {
      if (!confirm("Regenerate this profile's entire fingerprint and reseed a fresh cookie jar?")) return;
      await api(`/api/profiles/${id}/randomize-all`, { method: "POST", body: JSON.stringify({ seed_cookies: 15 }) });
      toast("Fingerprint + cookies randomized");
    } else if (act === "edit") {
      return await openEditor(id);
    } else if (act === "cookies") {
      return await openCookies(id);
    }
    loadProfiles();
  } catch (err) {
    if (act === "start") { btn.disabled = false; btn.textContent = "Launch"; }
    // A real-Android launch fails until the SDK is installed — route to setup.
    if (act === "start" && /Android engine isn't installed/i.test(err.message)) {
      toast("Install the Android engine first", "err");
      openAndroid();
    } else if (act === "start" && err.detail && err.detail.code === "display_fit") {
      openFit(id, err.detail);
    } else {
      toast(err.message, "err");
    }
    loadProfiles();
  }
});

// -------------------------------------------------------- display fit dialog ---
// A headful launch the server refused because the profile's window can't open on
// this monitor without changing its fingerprint. Offer the honest ways out.
let fitProfileId = null;
function openFit(id, d) {
  fitProfileId = id;
  const [w, h] = d.css_px, [rw, rh] = d.required_px, [aw, ah] = d.available_px;
  $("#fit-msg").textContent =
    `This profile needs ${rw}×${rh} display pixels (${w}×${h} CSS px at ${d.scale}x), ` +
    `but this display's usable area is ${aw}×${ah}. Shrinking its window further would break its fingerprint.`;
  $("#fit-hint").textContent = (d.mobile
    ? "A phone profile always opens at its exact screen size. The Chromium engine shows phones in a scaled window. "
    : "") + (d.virtual_available
    ? "A virtual display runs the browser on an invisible screen: good for automation, not for clicking around yourself."
    : "Virtual displays need Linux with Xvfb installed.");
  $("#fit-virtual").classList.toggle("hidden-block", !d.virtual_available);
  $("#fit-new").classList.toggle("hidden-block", !!d.mobile || !displayInfo.fit_limit);
  $("#fit-modal").showModal();
}
const closeFit = () => $("#fit-modal").close();
$("#fit-close").addEventListener("click", closeFit);
$("#fit-cancel").addEventListener("click", closeFit);
$("#fit-virtual").addEventListener("click", async () => {
  const id = fitProfileId; closeFit();
  try {
    await api(`/api/profiles/${id}/start`, { method: "POST", body: JSON.stringify({ headless: "virtual" }) });
    toast("Launching in a virtual display…");
    ensurePolling();
  } catch (err) { toast(err.message, "err"); }
  loadProfiles();
});
$("#fit-new").addEventListener("click", async () => {
  const src = allProfiles.find((p) => p.id === fitProfileId);
  closeFit();
  await openEditor(null);
  if (src && !isPhoneOs(src.fingerprint.os)) $("#f-os").value = src.fingerprint.os;
  $("#f-fit").checked = true;
  syncFitRows();
});

// Live status: while any profile is launching, refresh so pills update on their own.
let pollTimer = null;
function ensurePolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (document.hidden) return;
    await loadProfiles();
    const anyLaunching = allProfiles.some((p) => p.status === "launching");
    if (!anyLaunching) { clearInterval(pollTimer); pollTimer = null; }
  }, 2000);
}

// Click a "⚠ failed" pill to see why the launch failed.
$("#profiles-body").addEventListener("click", (e) => {
  const pill = e.target.closest(".pill.err[data-err]");
  if (!pill) return;
  const id = pill.dataset.err;
  const msg = launchErrors[id] || "Launch failed. Check that the engine is installed and try again.";
  if (/Android engine isn't installed/i.test(msg)) { openAndroid(); return; }
  alert("Launch failed:\n\n" + msg);
});

// ------------------------------------------------------------------- editor ---
function updateEngineHint() {
  const engine = $("#f-engine").value;
  const hints = {
    chromium: "Chromium: best site compatibility, and phone profiles get a true mobile interface (real viewport, touch, DPR). Slightly less stealthy than Camoufox.",
    android: "Android: boots a real Android device in the official emulator — a genuine Chrome-for-Android engine, not a spoof. Needs a one-click SDK install (a few GB) and boots in 20–60s. One running device per profile.",
    camoufox: "Camoufox: patched Firefox with native-level fingerprint spoofing — the strongest stealth. Phone profiles are emulated at the browser level.",
  };
  $("#engine-hint").textContent = hints[engine] || hints.camoufox;
}
$("#f-engine").addEventListener("change", updateEngineHint);
$("#f-os").addEventListener("change", syncFitRows);
$("#f-engine").addEventListener("change", syncFitRows);
$("#bulk-os").addEventListener("change", syncFitRows);
$("#bulk-engine").addEventListener("change", syncFitRows);

function setProxyMode(mode) {
  proxyMode = mode;
  $$("#proxy-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#proxy-manual").classList.toggle("hidden-block", mode !== "manual");
  $("#proxy-pool-box").classList.toggle("hidden-block", mode === "manual");
  $("#pool-mode-hint").textContent = mode === "random"
    ? "A random proxy is picked from the pool on every launch."
    : mode === "rotate"
    ? "Proxies are used round-robin — each launch advances to the next one."
    : "";
}

$("#proxy-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if (b) setProxyMode(b.dataset.mode);
});

function proxyToLine(p) {
  const auth = p.username ? `${encodeURIComponent(p.username)}:${encodeURIComponent(p.password || "")}@` : "";
  const host = p.host.includes(":") && !p.host.startsWith("[") ? `[${p.host}]` : p.host;
  return `${p.type}://${auth}${host}:${p.port}`;
}

function readForm() {
  return {
    name: $("#f-name").value.trim(),
    os: $("#f-os").value || null,
    engine: $("#f-engine").value || "camoufox",
    start_url: $("#f-url").value.trim() || "about:blank",
    tags: $("#f-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    notes: $("#f-notes").value.trim(),
    humanize: $("#f-humanize").checked,
    block_webrtc: $("#f-webrtc").checked,
    geoip: $("#f-geoip").checked,
    proxy: {
      type: $("#f-ptype").value,
      host: $("#f-phost").value.trim(),
      port: parseInt($("#f-pport").value || "0", 10),
      username: $("#f-puser").value,
      password: $("#f-ppass").value,
    },
  };
}

function fillForm(p) {
  $("#f-name").value = p?.name || "";
  $("#f-os").value = p?.fingerprint?.os || "";
  $("#f-os").disabled = !!p; // OS/device is fixed after creation (randomize to change)
  $("#f-engine").value = p?.engine || "camoufox";
  updateEngineHint();
  $("#f-url").value = p?.start_url || "about:blank";
  $("#f-tags").value = (p?.tags || []).join(", ");
  $("#f-notes").value = p?.notes || "";
  $("#f-humanize").checked = p ? p.humanize : true;
  $("#f-webrtc").checked = p ? p.block_webrtc : true;
  $("#f-geoip").checked = p ? p.geoip : true;
  const px = p?.proxy || {};
  $("#f-ptype").value = px.type || "http";
  $("#f-phost").value = px.host || "";
  $("#f-pport").value = px.port || "";
  $("#f-puser").value = px.username || "";
  $("#f-ppass").value = px.password || "";
  $("#f-pool").value = (p?.proxy_pool || []).map(proxyToLine).join("\n");
  $("#f-pool-type").value = "http";
  $("#proxy-result").textContent = "";
  $("#pool-summary").innerHTML = "";
  setProxyMode(p?.proxy_mode || "manual");
  renderFpPreview(p?.fingerprint);
}

function renderFpPreview(fp) {
  const box = $("#fp-preview");
  if (!fp) { box.textContent = "A coherent, deeply-randomized fingerprint is generated on save."; return; }
  box.textContent =
    `Fingerprint (pinned natively by Camoufox, identical every launch)\n` +
    `  OS:        ${fp.os}${fp.is_mobile ? " · 📱 " + (fp.device_name || "phone") : ""}\n` +
    (fp.is_mobile ? `  UA:        ${fp.user_agent}\n` : "") +
    `  Screen:    ${fp.screen_width}×${fp.screen_height} · ${fp.color_depth}-bit · DPR ${fp.device_pixel_ratio}\n` +
    `  GPU:       ${fp.webgl_renderer}\n` +
    `  CPU/RAM:   ${fp.hardware_concurrency} cores · ${fp.device_memory} GB\n` +
    `  Locale:    ${fp.language} (${fp.region}) · ${fp.timezone}\n` +
    `  Audio:     ${fp.audio_sample_rate} Hz · ${fp.audio_channels}ch\n` +
    `  Fonts:     ${(fp.fonts || []).length ? (fp.fonts.length + " installed") : "realistic per-identity set (drawn by Camoufox, stable)"}\n` +
    `  Media:     ${fp.webcams} cam · ${fp.micros} mic · ${fp.speakers} spk\n` +
    `  Touch:     ${fp.max_touch_points} points · DNT ${fp.do_not_track}`;
}

async function openEditor(id) {
  editingId = id || null;
  $("#modal-title").textContent = id ? "Edit Profile" : "New Profile";
  let profile = null;
  if (id) profile = await api(`/api/profiles/${id}`);
  fillForm(profile);
  syncFitRows();
  $("#modal").showModal();
}
function closeEditor() { $("#modal").close(); }

// Turn the pasted pool text into structured proxies via the server parser.
async function collectPool() {
  const text = $("#f-pool").value;
  if (!text.trim()) return [];
  const r = await api("/api/proxy/parse", {
    method: "POST",
    body: JSON.stringify({ text, default_type: $("#f-pool-type").value }),
  });
  return r.proxies;
}

$("#modal-save").addEventListener("click", async () => {
  const form = readForm();
  if (!form.name) return toast("Name is required", "err");
  try {
    let pool = [];
    if (proxyMode !== "manual") {
      pool = await collectPool();
      if (!pool.length) return toast("Add at least one proxy, or switch to Manual", "err");
    }
    const common = {
      name: form.name, start_url: form.start_url, engine: form.engine, proxy: form.proxy,
      tags: form.tags, notes: form.notes,
      proxy_mode: proxyMode, proxy_pool: pool,
      humanize: form.humanize, block_webrtc: form.block_webrtc, geoip: form.geoip,
    };
    if (editingId) {
      await api(`/api/profiles/${editingId}`, { method: "PATCH", body: JSON.stringify(common) });
      toast("Saved");
    } else {
      const fit = !$("#f-fit-row").classList.contains("hidden-block") && $("#f-fit").checked;
      await api("/api/profiles", { method: "POST", body: JSON.stringify({ ...common, os: form.os, fit_display: fit }) });
      toast("Profile created");
    }
    closeEditor();
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
});

$("#new-btn").addEventListener("click", () => openEditor(null));
$("#modal-close").addEventListener("click", closeEditor);
$("#modal-cancel").addEventListener("click", closeEditor);

// single-proxy test
$("#test-proxy").addEventListener("click", async () => {
  const px = readForm().proxy;
  const out = $("#proxy-result");
  if (!px.host || !px.port) { out.className = "proxy-result err"; out.textContent = "Enter host and port first."; return; }
  out.className = "proxy-result"; out.textContent = "Testing…";
  try {
    const r = await api("/api/proxy/test", { method: "POST", body: JSON.stringify(px) });
    if (r.ok) {
      out.className = "proxy-result ok";
      out.textContent = `✓ ${r.ip} · ${r.city || ""} ${r.country || ""} · ${r.latency_ms} ms`;
    } else {
      out.className = "proxy-result err"; out.textContent = `✗ ${r.error}`;
    }
  } catch (e) { out.className = "proxy-result err"; out.textContent = `✗ ${e.message}`; }
});

// whole-pool test
$("#test-pool").addEventListener("click", async () => {
  const text = $("#f-pool").value;
  const sum = $("#pool-summary");
  if (!text.trim()) { sum.innerHTML = `<span class="chip bad">No proxies entered</span>`; return; }
  sum.innerHTML = `<span class="chip">Testing…</span>`;
  try {
    const r = await api("/api/proxy/test-pool", {
      method: "POST", body: JSON.stringify({ text, default_type: $("#f-pool-type").value }),
    });
    const dead = r.count - r.alive;
    sum.innerHTML =
      `<span class="chip">${r.count} parsed</span>` +
      `<span class="chip ok">✓ ${r.alive} alive</span>` +
      (dead > 0 ? `<span class="chip bad">✗ ${dead} dead</span>` : "");
  } catch (e) { sum.innerHTML = `<span class="chip bad">${esc(e.message)}</span>`; }
});

// fetch free proxies from public sources into the textarea
$("#fetch-free").addEventListener("click", async () => {
  const btn = $("#fetch-free");
  const sum = $("#pool-summary");
  const verify = $("#free-verify").checked;
  const protocol = $("#f-pool-type").value === "socks5" ? "socks5" : "http";
  btn.disabled = true; btn.textContent = verify ? "Fetching + testing…" : "Fetching…";
  sum.innerHTML = `<span class="chip">Contacting free-proxy sources…</span>`;
  try {
    const r = await api("/api/proxy/fetch-free", {
      method: "POST", body: JSON.stringify({ protocol, limit: 50, verify }),
    });
    if (!r.proxies.length) {
      sum.innerHTML = `<span class="chip bad">No proxies returned — try again or paste your own</span>`;
    } else {
      const existing = $("#f-pool").value.trim();
      $("#f-pool").value = (existing ? existing + "\n" : "") + r.proxies.join("\n");
      $("#f-pool").dispatchEvent(new Event("input"));
      sum.innerHTML = verify
        ? `<span class="chip ok">✓ added ${r.alive} live proxies</span>`
        : `<span class="chip">added ${r.count} proxies (untested)</span>`;
      // default the pool type select to match what we fetched
      $("#f-pool-type").value = r.protocol;
    }
  } catch (e) {
    sum.innerHTML = `<span class="chip bad">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = "⬇ Fetch free proxies";
  }
});

// re-count pool as you type; hide any open picker (it's now stale)
$("#f-pool").addEventListener("input", () => {
  const n = $("#f-pool").value.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  $("#pool-summary").innerHTML = n ? `<span class="chip">${n} line${n === 1 ? "" : "s"}</span>` : "";
  $("#pool-list").classList.add("hidden-block");
});

// "Choose one to use": test the pool, list each proxy live-status first, and let the
// user drop a single working one into the Manual proxy slot. Parse + test share the
// same parser (server-side), so their results line up by index.
let pickedPool = [];  // structured proxies, index-aligned with the picker rows
$("#pick-proxy").addEventListener("click", async () => {
  const text = $("#f-pool").value;
  const box = $("#pool-list");
  box.classList.remove("hidden-block");
  if (!text.trim()) { box.innerHTML = `<div class="pool-item empty">No proxies entered — paste or fetch some first.</div>`; return; }
  box.innerHTML = `<div class="pool-item empty">Testing every proxy…</div>`;
  const btn = $("#pick-proxy");
  btn.disabled = true;
  try {
    const body = JSON.stringify({ text, default_type: $("#f-pool-type").value });
    const [parsed, tested] = await Promise.all([
      api("/api/proxy/parse", { method: "POST", body }),
      api("/api/proxy/test-pool", { method: "POST", body }),
    ]);
    pickedPool = parsed.proxies || [];
    const byIndex = new Map((tested.results || []).map((r) => [r.index, r]));
    if (!pickedPool.length) { box.innerHTML = `<div class="pool-item empty">Nothing parsed from that list.</div>`; return; }
    // Alive proxies first, then fastest; dead ones sink to the bottom.
    const order = pickedPool.map((_, i) => i).sort((a, b) => {
      const ra = byIndex.get(a) || {}, rb = byIndex.get(b) || {};
      if (!!rb.ok !== !!ra.ok) return rb.ok ? 1 : -1;
      return (ra.latency_ms ?? 1e9) - (rb.latency_ms ?? 1e9);
    });
    box.innerHTML = order.map((i) => {
      const p = pickedPool[i], r = byIndex.get(i) || {};
      const status = r.ok
        ? `<span class="ps ok">✓ ${esc(r.ip || "")} · ${esc([r.city, r.country].filter(Boolean).join(" "))} · ${r.latency_ms} ms</span>`
        : `<span class="ps bad">✗ ${esc(r.error || "no response")}</span>`;
      return `<button class="pool-item" data-index="${i}" ${r.ok ? "" : 'data-dead="1"'}>` +
        `<span class="mono">${esc(proxyToLine(p))}</span>${status}</button>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="pool-item empty">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

// Pick a proxy from the list → load it into the Manual slot and switch to Manual mode.
$("#pool-list").addEventListener("click", (e) => {
  const row = e.target.closest("button.pool-item[data-index]");
  if (!row) return;
  const p = pickedPool[parseInt(row.dataset.index, 10)];
  if (!p) return;
  $("#f-ptype").value = p.type || "http";
  $("#f-phost").value = p.host || "";
  $("#f-pport").value = p.port || "";
  $("#f-puser").value = p.username || "";
  $("#f-ppass").value = p.password || "";
  $("#pool-list").classList.add("hidden-block");
  setProxyMode("manual");
  $("#proxy-result").className = "proxy-result"; $("#proxy-result").textContent = "Loaded into the Manual proxy — test it before saving.";
  toast("Proxy loaded into Manual");
});

// -------------------------------------------------------------- bulk create ---
$("#bulk-btn").addEventListener("click", () => { syncFitRows(); $("#bulk-modal").showModal(); });
$("#bulk-close").addEventListener("click", () => $("#bulk-modal").close());
$("#bulk-cancel").addEventListener("click", () => $("#bulk-modal").close());

$("#bulk-create").addEventListener("click", async () => {
  const count = parseInt($("#bulk-count").value, 10);
  if (isNaN(count) || count < 1) return toast("Enter a positive number", "err");
  const body = {
    count,
    name_prefix: $("#bulk-prefix").value.trim() || "Profile",
    os: $("#bulk-os").value || null,
    engine: $("#bulk-engine").value || "camoufox",
    seed_cookies: parseInt($("#bulk-cookies").value || "0", 10),
    fit_display: !$("#bulk-fit-row").classList.contains("hidden-block") && $("#bulk-fit").checked,
  };
  try {
    $("#bulk-create").textContent = "Creating…"; $("#bulk-create").disabled = true;
    const r = await api("/api/profiles/bulk", { method: "POST", body: JSON.stringify(body) });
    toast(`Created ${r.created} randomized profiles`);
    $("#bulk-modal").close();
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
  finally { $("#bulk-create").textContent = "Create"; $("#bulk-create").disabled = false; }
});

// --------------------------------------------------------------- new phone ---
// One-click phone profiles (Multilogin-style). Two modes:
//   emulated — Chromium with a mobile fingerprint (instant, unlimited).
//   android  — a real Android device in the official emulator (genuine engine).
let deviceCatalog = null;   // cached [{name, os, screen, dpr}]
let phoneMode = "emulated";

async function loadDevices() {
  if (deviceCatalog) return deviceCatalog;
  const r = await api("/api/devices");
  deviceCatalog = r.devices || [];
  return deviceCatalog;
}

function osLabel(os) { return os === "ios" ? "🍏 iPhone" : "📱 Android"; }

function fillPhoneDevices() {
  const sel = $("#phone-device");
  const devices = deviceCatalog || [];
  // Real Android can only be an Android device — you can't boot an iPhone in an AVD.
  const allowed = phoneMode === "android" ? ["android"] : ["android", "ios"];
  const groups = {};
  devices.forEach((d) => { if (allowed.includes(d.os)) (groups[d.os] || (groups[d.os] = [])).push(d); });
  let html = "";
  for (const os of ["android", "ios"]) {
    if (!groups[os] || !groups[os].length) continue;
    html += `<optgroup label="${osLabel(os)}">`;
    html += groups[os].map((d) => `<option value="${esc(d.name)}">${esc(d.name)} · ${esc(d.screen)} · DPR ${d.dpr}</option>`).join("");
    html += `</optgroup>`;
  }
  sel.innerHTML = html || `<option value="">No devices</option>`;
  updatePhoneInfo();
}

function setPhoneMode(mode) {
  phoneMode = mode;
  $$("#phone-mode .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#phone-mode-hint").textContent = mode === "android"
    ? "Real Android: boots a genuine Android device (Chrome-for-Android) in the official emulator. Free, but needs a one-click SDK install (a few GB) and boots in 20–60s. One device per profile."
    : "Emulated: Chromium wearing a mobile fingerprint — a phone-shaped window, mobile viewport, touch and DPR. Instant, unlimited profiles.";
  $("#phone-launch").textContent = mode === "android" ? "Create & boot" : "Create & launch";
  if (deviceCatalog) fillPhoneDevices();
}

async function openPhone() {
  $("#phone-name").value = "";
  $("#phone-url").value = "";
  $("#phone-device-info").textContent = "";
  $("#phone-device").innerHTML = `<option value="">Loading devices…</option>`;
  setPhoneMode("emulated");
  $("#phone-modal").showModal();
  try {
    await loadDevices();
    fillPhoneDevices();
  } catch (e) {
    $("#phone-device").innerHTML = `<option value="">Failed to load: ${esc(e.message)}</option>`;
  }
}

function updatePhoneInfo() {
  const name = $("#phone-device").value;
  const d = (deviceCatalog || []).find((x) => x.name === name);
  const info = $("#phone-device-info");
  if (!d) { info.textContent = ""; return; }
  if (phoneMode === "android") {
    info.innerHTML = `<span class="chip ok">Real Android · Chrome-for-Android</span><span class="chip">${esc(d.screen)}</span><span class="chip">not a spoof — genuine engine</span>`;
    return;
  }
  info.innerHTML = d.os === "ios"
    ? `<span class="chip">iPhone · Safari surface</span><span class="chip">${esc(d.screen)} @ ${d.dpr}x</span><span class="chip warn">iOS is a weaker spoof than Android</span>`
    : `<span class="chip ok">Android · Chrome</span><span class="chip">${esc(d.screen)} @ ${d.dpr}x</span>`;
}
$("#phone-device").addEventListener("change", updatePhoneInfo);
$$("#phone-mode .seg-btn").forEach((b) => b.addEventListener("click", () => setPhoneMode(b.dataset.mode)));

async function createPhone(launch) {
  const name = $("#phone-name").value.trim();
  const device = $("#phone-device").value;
  if (!name) return toast("Name is required", "err");
  if (!device) return toast("Pick a device", "err");
  const d = (deviceCatalog || []).find((x) => x.name === device);
  const android = phoneMode === "android";

  // A real-Android launch needs the SDK installed first. Gate it before we create.
  if (android && launch) {
    const st = await api("/api/android/status");
    if (!st.ready) {
      $("#phone-modal").close();
      toast("Install the Android engine first", "err");
      return openAndroid();
    }
  }

  const btn = launch ? $("#phone-launch") : $("#phone-create");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Creating…";
  try {
    const created = await api("/api/profiles", {
      method: "POST",
      body: JSON.stringify({
        name,
        engine: android ? "android" : "chromium",
        os: d ? d.os : "android",
        device,
        start_url: $("#phone-url").value.trim() || "about:blank",
      }),
    });
    $("#phone-modal").close();
    if (launch) {
      btn.textContent = android ? "Booting…" : "Launching…";
      const r = await api(`/api/profiles/${created.id}/start`, { method: "POST" });
      toast(r.proxy ? `Phone launched · ${r.proxy}` : (android ? "Android device booted 🤖" : "Phone launched 📱"));
    } else {
      toast(android ? "Android phone profile created 🤖" : "Phone profile created 📱");
    }
    loadProfiles();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

$("#phone-btn").addEventListener("click", openPhone);
$("#phone-close").addEventListener("click", () => $("#phone-modal").close());
$("#phone-cancel").addEventListener("click", () => $("#phone-modal").close());
$("#phone-create").addEventListener("click", () => createPhone(false));
$("#phone-launch").addEventListener("click", () => createPhone(true));

// ----------------------------------------------------------- android setup ---
let androidPoll = null;

function renderAndroidStatus(st) {
  const box = $("#android-status-box");
  // Hardware-acceleration warning: the usual cause of a black screen on boot.
  let accelNote = "";
  if (st.accel && st.accel.ok === false) {
    accelNote = `<div class="android-accel-warn">⚠ Hardware acceleration unavailable — the emulator will run in software mode (slow, but avoids a black screen). To speed it up, enable <strong>Windows Hypervisor Platform</strong> in “Turn Windows features on or off”.${st.accel.detail ? "<br><span class='mono'>" + esc(st.accel.detail) + "</span>" : ""}</div>`;
  }
  if (st.ready) {
    box.className = "android-status ok";
    const mirror = st.scrcpy
      ? "It opens in a clean <strong>mirror window</strong> (scrcpy) with real touch input."
      : "⚠ The mirror window (scrcpy) isn't installed — click Reinstall to add it, otherwise the emulator's own window may show black.";
    box.innerHTML = "✅ Android engine is installed and ready. New real-Android phone profiles boot a genuine device. " + mirror + accelNote;
    $("#android-install").textContent = st.scrcpy ? "Reinstall" : "Add mirror window";
  } else {
    box.className = "android-status warn";
    const bits = [];
    bits.push(st.java ? "Java ✓" : "Java ✗ (a small runtime will be fetched)");
    bits.push("SDK tools " + (st.components.adb && st.components.emulator ? "✓" : "✗"));
    bits.push("system image " + (st.system_image ? "✓" : "✗"));
    box.innerHTML = "Not installed yet. One-click setup will fetch: <strong>" + bits.join(" · ") + "</strong>." + accelNote;
    $("#android-install").textContent = "Install Android engine";
  }
}

async function openAndroid() {
  $("#android-modal").showModal();
  $("#android-log").classList.add("hidden-block");
  $("#android-install").disabled = false;
  $("#android-status-box").textContent = "Checking…";
  try { renderAndroidStatus(await api("/api/android/status")); }
  catch (e) { $("#android-status-box").textContent = "Status check failed: " + e.message; }
  // If an install is already running (e.g. reopened modal), resume polling.
  try {
    const ins = await api("/api/android/install/status");
    if (ins.running && $("#android-modal").open) startAndroidPoll();
  } catch (e) {
    if ($("#android-modal").open) toast(e.message, "err");
  }
}

function startAndroidPoll() {
  const log = $("#android-log");
  log.classList.remove("hidden-block");
  $("#android-install").disabled = true;
  $("#android-install").textContent = "Installing…";
  clearInterval(androidPoll);
  androidPoll = setInterval(async () => {
    let s;
    try { s = await api("/api/android/install/status"); } catch { return; }
    log.textContent = (s.lines || []).join("\n");
    log.scrollTop = log.scrollHeight;
    if (!s.running) {
      clearInterval(androidPoll);
      $("#android-install").disabled = false;
      if (s.error) { toast("Install failed: " + s.error, "err"); }
      else if (s.done) { toast("Android engine ready 🤖"); }
      renderAndroidStatus(await api("/api/android/status"));
    }
  }, 1200);
}

async function installAndroid() {
  try {
    await api("/api/android/install", { method: "POST" });
    if ($("#android-modal").open) startAndroidPoll();
  } catch (e) { toast(e.message, "err"); }
}

$("#android-install").addEventListener("click", installAndroid);
$("#android-close").addEventListener("click", () => $("#android-modal").close());
$("#android-cancel").addEventListener("click", () => $("#android-modal").close());
$("#android-modal").addEventListener("close", () => clearInterval(androidPoll));

// ------------------------------------------------------------------ cookies ---
let cookieProfileId = null;
async function refreshCookieInfo() {
  const data = await api(`/api/profiles/${cookieProfileId}/cookies`);
  $("#cookie-info").textContent = `This profile has ${data.cookies.length} staged cookie(s). They're injected into the browser on its next launch.`;
  return data.cookies;
}
async function openCookies(id) {
  cookieProfileId = id;
  $("#cookie-json").value = "";
  $("#cookie-count").value = "10";
  await refreshCookieInfo();
  $("#cookie-modal").showModal();
}
const closeCookies = () => { $("#cookie-modal").close(); loadProfiles(); };
$("#cookie-close").addEventListener("click", closeCookies);
$("#cookie-done").addEventListener("click", closeCookies);
$("#cookie-add").addEventListener("click", async () => {
  const count = parseInt($("#cookie-count").value, 10);
  if (isNaN(count) || count < 1) return toast("Enter a positive number", "err");
  try {
    const r = await api(`/api/profiles/${cookieProfileId}/cookies/random`, {
      method: "POST", body: JSON.stringify({ count, domain: "example.com" }),
    });
    toast(`Added ${r.added} cookies (total ${r.count})`);
    refreshCookieInfo();
  } catch (e) { toast(e.message, "err"); }
});
$("#cookie-import").addEventListener("click", async () => {
  let cookies;
  try {
    cookies = JSON.parse($("#cookie-json").value);
    if (cookies && !Array.isArray(cookies) && Array.isArray(cookies.cookies)) cookies = cookies.cookies;
    if (!Array.isArray(cookies)) throw new Error("expected a JSON array of cookies");
  } catch (e) { return toast(`Invalid JSON: ${e.message}`, "err"); }
  try {
    const r = await api(`/api/profiles/${cookieProfileId}/cookies`, { method: "PUT", body: JSON.stringify({ cookies }) });
    toast(`Imported ${r.count} cookie(s)${r.count < cookies.length ? ` (${cookies.length - r.count} skipped: no name/value)` : ""}`);
    $("#cookie-json").value = "";
    refreshCookieInfo();
  } catch (e) { toast(e.message, "err"); }
});
$("#cookie-export").addEventListener("click", async () => {
  try {
    const cookies = await refreshCookieInfo();
    const p = allProfiles.find((x) => x.id === cookieProfileId);
    downloadJSON(`cookies-${(p?.name || cookieProfileId).replace(/[^\w.-]+/g, "_")}.json`, cookies);
  } catch (e) { toast(e.message, "err"); }
});
$("#cookie-clear").addEventListener("click", async () => {
  if (!confirm("Clear all staged cookies for this profile?")) return;
  try {
    await api(`/api/profiles/${cookieProfileId}/cookies`, { method: "DELETE" });
    toast("Cookies cleared");
    refreshCookieInfo();
  } catch (e) { toast(e.message, "err"); }
});

// --------------------------------------------------------- profile import ---
$("#import-btn").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";  // let the same file be picked again later
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const profiles = Array.isArray(data) ? data : data.profiles;
    if (!Array.isArray(profiles) || !profiles.length) throw new Error("no profiles in that file");
    const r = await api("/api/profiles-import", { method: "POST", body: JSON.stringify({ profiles }) });
    toast(`Imported ${r.created} profile(s)${r.errors.length ? `, ${r.errors.length} failed` : ""}`, r.errors.length ? "err" : "ok");
    if (r.errors.length) console.warn("Import errors:", r.errors);
    loadProfiles();
  } catch (err) { toast(`Import failed: ${err.message}`, "err"); }
});

// ------------------------------------------------------------ engine setup ---
// The Camoufox browser (~1.3 GB) downloads once. The server does it resumably
// with progress; this banner starts it and shows how it's going.
let enginePoll = null;
const fmtMB = (b) => (b / 1048576).toFixed(0) + " MB";

function renderEngine(st) {
  const banner = $("#engine-banner");
  if (st.installed) {
    banner.classList.add("hidden");
    return false;  // stop polling
  }
  banner.classList.remove("hidden");
  banner.classList.toggle("err", st.phase === "error");
  const bar = $("#engine-bar"), prog = $("#engine-progress"), btn = $("#engine-download");
  if (st.downloading) {
    prog.classList.remove("hidden-block");
    btn.disabled = true;
    if (st.phase === "extracting") {
      $("#engine-title").textContent = "Installing Camoufox browser…";
      $("#engine-sub").textContent = "Verifying and unpacking — almost done.";
      prog.classList.add("indeterminate"); bar.style.width = "";
    } else {
      prog.classList.remove("indeterminate");
      bar.style.width = `${st.percent || 0}%`;
      $("#engine-title").textContent = `Downloading Camoufox browser… ${st.percent || 0}%`;
      const eta = st.speed > 0 && st.total ? Math.round((st.total - st.downloaded) / st.speed) : null;
      $("#engine-sub").textContent = st.total
        ? `${fmtMB(st.downloaded)} of ${fmtMB(st.total)} · ${fmtMB(st.speed)}/s${eta !== null ? ` · ~${eta > 90 ? Math.round(eta / 60) + " min" : eta + " s"} left` : ""}`
        : "Contacting GitHub…";
    }
    btn.textContent = "Downloading…";
    return true;
  }
  prog.classList.add("hidden-block");
  btn.disabled = false;
  if (st.phase === "error") {
    $("#engine-title").textContent = "Engine download failed";
    $("#engine-sub").textContent = `${st.error || "Unknown error"} — Retry resumes where it stopped.`;
    btn.textContent = "↻ Retry";
  } else {
    $("#engine-title").textContent = "Camoufox browser not downloaded yet";
    $("#engine-sub").textContent = "One-time download (~1.3 GB). Needed to launch Camoufox profiles; Chromium and Android profiles work without it.";
    btn.textContent = "⬇ Download engine";
  }
  return false;
}

async function checkEngine() {
  try {
    const keep = renderEngine(await api("/api/engine/status"));
    if (keep && !enginePoll) enginePoll = setInterval(checkEngine, 1000);
    if (!keep && enginePoll) {
      clearInterval(enginePoll); enginePoll = null;
      const st = await api("/api/engine/status");
      if (st.installed) toast(`Camoufox ${st.version || ""} ready 🦊`);
    }
  } catch (_) { /* server restarting — try again on the next tick */ }
}
$("#engine-download").addEventListener("click", async () => {
  try {
    renderEngine(await api("/api/engine/ensure", { method: "POST" }));
    if (!enginePoll) enginePoll = setInterval(checkEngine, 1000);
  } catch (e) { toast(e.message, "err"); }
});

// Native dialogs handle Escape and keep keyboard focus inside.
$$(".modal").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    const toast = $("#toast");
    if (toast.parentElement === dialog) document.body.append(toast);
  });
});

document.addEventListener("click", (event) => {
  $$(".more-actions[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || $(".modal[open]")) return;
  const menu = $(".more-actions[open]");
  if (menu) {
    menu.open = false;
    menu.querySelector("summary").focus();
  }
});

// ---------------------------------------------------------------------- init ---
loadProfiles();
checkEngine();
setInterval(() => { if (!document.hidden && !pollTimer) loadProfiles(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadProfiles(); });
