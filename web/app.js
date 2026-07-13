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
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  setTimeout(() => t.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

const OS_ICON = { windows: "🪟", macos: "🍎", linux: "🐧", android: "📱", ios: "🍏" };

// ------------------------------------------------------------- render list ---
function deviceCell(fp) {
  if (fp.is_mobile) {
    const icon = fp.os === "ios" ? "🍏" : "📱";
    return `<span class="badge device mobile">${icon} ${esc(fp.device_name || "Phone")}</span>`;
  }
  const label = { windows: "Windows", macos: "macOS", linux: "Linux" }[fp.os] || fp.os;
  return `<span class="badge device">${OS_ICON[fp.os] || "💻"} ${esc(label)}</span>`;
}

function proxyCell(p) {
  if (p.proxy_mode && p.proxy_mode !== "manual") {
    const n = (p.proxy_pool || []).length;
    return `<span class="badge mode">${p.proxy_mode}</span> <span class="sub">${n} prox${n === 1 ? "y" : "ies"}</span>`;
  }
  if (p.proxy && p.proxy.host) {
    return `<span class="mono">${esc(p.proxy.type)}://${esc(p.proxy.host)}:${p.proxy.port}</span>`;
  }
  return `<span class="dash">— direct</span>`;
}

function renderList(profiles) {
  const body = $("#profiles-body");
  if (!profiles.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">No profiles yet. Click “+ New Profile”.</td></tr>`;
    return;
  }
  body.innerHTML = profiles.map((p) => {
    const fp = p.fingerprint;
    const status = p.running ? `<span class="badge on">running</span>` : `<span class="badge off">stopped</span>`;
    const toggle = p.running
      ? `<button class="sm" data-act="stop" data-id="${p.id}">Stop</button>`
      : `<button class="sm primary" data-act="start" data-id="${p.id}">Launch</button>`;
    return `<tr>
      <td><div class="name">${esc(p.name)}</div><div class="sub">${fp.screen_width}×${fp.screen_height}</div></td>
      <td>${deviceCell(fp)}</td>
      <td>${proxyCell(p)}</td>
      <td class="hide-sm sub">${esc(fp.language)} · ${esc(fp.timezone)}</td>
      <td class="hide-sm"><button class="sm ghost" data-act="cookies" data-id="${p.id}">Cookies</button></td>
      <td>${status}</td>
      <td class="actions-cell">
        ${toggle}
        <button class="sm" data-act="randomize" data-id="${p.id}" title="Regenerate fingerprint + fresh cookie jar">🎲</button>
        <button class="sm" data-act="edit" data-id="${p.id}">Edit</button>
        <button class="sm" data-act="clone" data-id="${p.id}">Clone</button>
        <button class="sm danger" data-act="delete" data-id="${p.id}">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

function applySearch() {
  const q = $("#search").value.trim().toLowerCase();
  const filtered = !q ? allProfiles : allProfiles.filter((p) => {
    const fp = p.fingerprint;
    return (p.name + " " + fp.os + " " + (fp.device_name || "") + " " + fp.language).toLowerCase().includes(q);
  });
  renderList(filtered);
}

async function loadProfiles() {
  try {
    allProfiles = await api("/api/profiles");
    $("#profile-count").textContent = `${allProfiles.length} profile${allProfiles.length === 1 ? "" : "s"}`;
    applySearch();
  } catch (e) {
    $("#profiles-body").innerHTML = `<tr><td colspan="7" class="empty">Error: ${esc(e.message)}</td></tr>`;
  }
}

$("#search").addEventListener("input", applySearch);

// -------------------------------------------------------------- row actions ---
$("#profiles-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  try {
    if (act === "start") {
      btn.textContent = "Starting…"; btn.disabled = true;
      const r = await api(`/api/profiles/${id}/start`, { method: "POST" });
      toast(r.proxy ? `Launched · ${r.proxy}` : "Profile launched");
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
      return openEditor(id);
    } else if (act === "cookies") {
      return openCookies(id);
    }
    loadProfiles();
  } catch (err) {
    toast(err.message, "err");
    loadProfiles();
  }
});

// ------------------------------------------------------------------- editor ---
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
  const auth = p.username ? `${p.username}:${p.password}@` : "";
  return `${p.type}://${auth}${p.host}:${p.port}`;
}

function readForm() {
  return {
    name: $("#f-name").value.trim(),
    os: $("#f-os").value || null,
    start_url: $("#f-url").value.trim() || "about:blank",
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
  $("#f-url").value = p?.start_url || "about:blank";
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
    `  Fonts:     ${(fp.fonts || []).length} installed\n` +
    `  Media:     ${fp.webcams} cam · ${fp.micros} mic · ${fp.speakers} spk\n` +
    `  Touch:     ${fp.max_touch_points} points · DNT ${fp.do_not_track}`;
}

async function openEditor(id) {
  editingId = id || null;
  $("#modal-title").textContent = id ? "Edit Profile" : "New Profile";
  let profile = null;
  if (id) profile = await api(`/api/profiles/${id}`);
  fillForm(profile);
  $("#modal").classList.remove("hidden");
}
function closeEditor() { $("#modal").classList.add("hidden"); }

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
      name: form.name, start_url: form.start_url, proxy: form.proxy,
      proxy_mode: proxyMode, proxy_pool: pool,
      humanize: form.humanize, block_webrtc: form.block_webrtc, geoip: form.geoip,
    };
    if (editingId) {
      await api(`/api/profiles/${editingId}`, { method: "PATCH", body: JSON.stringify(common) });
      toast("Saved");
    } else {
      await api("/api/profiles", { method: "POST", body: JSON.stringify({ ...common, os: form.os }) });
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

// re-count pool as you type
$("#f-pool").addEventListener("input", () => {
  const n = $("#f-pool").value.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  $("#pool-summary").innerHTML = n ? `<span class="chip">${n} line${n === 1 ? "" : "s"}</span>` : "";
});

// -------------------------------------------------------------- bulk create ---
$("#bulk-btn").addEventListener("click", () => $("#bulk-modal").classList.remove("hidden"));
$("#bulk-close").addEventListener("click", () => $("#bulk-modal").classList.add("hidden"));
$("#bulk-cancel").addEventListener("click", () => $("#bulk-modal").classList.add("hidden"));

$("#bulk-create").addEventListener("click", async () => {
  const count = parseInt($("#bulk-count").value, 10);
  if (isNaN(count) || count < 1) return toast("Enter a positive number", "err");
  const body = {
    count,
    name_prefix: $("#bulk-prefix").value.trim() || "Profile",
    os: $("#bulk-os").value || null,
    seed_cookies: parseInt($("#bulk-cookies").value || "0", 10),
  };
  try {
    $("#bulk-create").textContent = "Creating…"; $("#bulk-create").disabled = true;
    const r = await api("/api/profiles/bulk", { method: "POST", body: JSON.stringify(body) });
    toast(`Created ${r.created} randomized profiles`);
    $("#bulk-modal").classList.add("hidden");
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
  finally { $("#bulk-create").textContent = "Create"; $("#bulk-create").disabled = false; }
});

// ------------------------------------------------------------------ cookies ---
let cookieProfileId = null;
async function openCookies(id) {
  cookieProfileId = id;
  const data = await api(`/api/profiles/${id}/cookies`);
  $("#cookie-info").textContent = `This profile has ${data.cookies.length} staged cookie(s).`;
  $("#cookie-count").value = "10";
  $("#cookie-modal").classList.remove("hidden");
}
$("#cookie-close").addEventListener("click", () => $("#cookie-modal").classList.add("hidden"));
$("#cookie-add").addEventListener("click", async () => {
  const count = parseInt($("#cookie-count").value, 10);
  if (isNaN(count) || count < 1) return toast("Enter a positive number", "err");
  try {
    const r = await api(`/api/profiles/${cookieProfileId}/cookies/random`, {
      method: "POST", body: JSON.stringify({ count, domain: "example.com" }),
    });
    toast(`Added ${r.added} cookies (total ${r.count})`);
    $("#cookie-modal").classList.add("hidden");
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
});
$("#cookie-clear").addEventListener("click", async () => {
  if (!confirm("Clear all staged cookies for this profile?")) return;
  try {
    await api(`/api/profiles/${cookieProfileId}/cookies`, { method: "DELETE" });
    toast("Cookies cleared");
    $("#cookie-modal").classList.add("hidden");
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
});

// close any modal on backdrop click
$$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); }));

// ---------------------------------------------------------------------- init ---
loadProfiles();
setInterval(loadProfiles, 5000);
