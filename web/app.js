// Anti-Detect Manager dashboard
const $ = (sel) => document.querySelector(sel);
let editingId = null; // null => creating new

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
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

// ------------------------------------------------------------- render list ---
async function loadProfiles() {
  const body = $("#profiles-body");
  try {
    const profiles = await api("/api/profiles");
    if (!profiles.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">No profiles yet. Click “+ New Profile”.</td></tr>`;
      return;
    }
    body.innerHTML = profiles
      .map((p) => {
        const fp = p.fingerprint;
        const proxy = p.proxy.host ? `${p.proxy.type}://${p.proxy.host}:${p.proxy.port}` : "—";
        const status = p.running
          ? `<span class="badge on">running</span>`
          : `<span class="badge off">stopped</span>`;
        const toggle = p.running
          ? `<button class="sm" data-act="stop" data-id="${p.id}">Stop</button>`
          : `<button class="sm primary" data-act="start" data-id="${p.id}">Launch</button>`;
        return `<tr>
          <td><strong>${esc(p.name)}</strong></td>
          <td><span class="badge os">${fp.os}</span></td>
          <td>${esc(proxy)}</td>
          <td>${fp.language} · ${fp.timezone}</td>
          <td><button class="sm ghost" data-act="cookies" data-id="${p.id}">Cookies</button></td>
          <td>${status}</td>
          <td class="actions-cell">
            ${toggle}
            <button class="sm" data-act="randomize" data-id="${p.id}" title="Regenerate fingerprint + fresh cookie jar">🎲 Randomize</button>
            <button class="sm" data-act="edit" data-id="${p.id}">Edit</button>
            <button class="sm" data-act="clone" data-id="${p.id}">Clone</button>
            <button class="sm danger" data-act="delete" data-id="${p.id}">Delete</button>
          </td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="7" class="empty">Error: ${esc(e.message)}</td></tr>`;
  }
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// -------------------------------------------------------------- row actions ---
$("#profiles-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  try {
    if (act === "start") {
      btn.textContent = "Starting…";
      btn.disabled = true;
      await api(`/api/profiles/${id}/start`, { method: "POST" });
      toast("Profile launched");
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
      if (!confirm("Regenerate this profile's entire fingerprint and reseed a fresh random cookie jar?")) return;
      await api(`/api/profiles/${id}/randomize-all`, { method: "POST", body: JSON.stringify({ seed_cookies: 15 }) });
      toast("Fingerprint + cookies randomized");
    } else if (act === "edit") {
      return openEditor(id);
    } else if (act === "cookies") {
      return cookieDialog(id);
    }
    loadProfiles();
  } catch (err) {
    toast(err.message, "err");
    loadProfiles();
  }
});

// ------------------------------------------------------------------- editor ---
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
  $("#proxy-result").textContent = "";
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
    `  Canvas:    AA offset ${fp.canvas_aa_offset} · font seed ${fp.fonts_spacing_seed}\n` +
    `  Fonts:     ${(fp.fonts || []).length} installed\n` +
    `  Media:     ${fp.webcams} cam · ${fp.micros} mic · ${fp.speakers} spk\n` +
    `  Battery:   ${fp.battery_charging ? "charging" : "on battery"} · ${Math.round(fp.battery_level * 100)}%\n` +
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

$("#bulk-btn").addEventListener("click", async () => {
  const n = prompt("How many profiles to create?\nEach gets a fully-randomized fingerprint + cookie jar.", "10");
  if (n === null) return;
  const count = parseInt(n, 10);
  if (isNaN(count) || count < 1) return toast("Enter a positive number", "err");
  const osChoice = prompt("OS for these profiles? (windows / macos / linux / android, or blank for random desktop per profile)", "");
  try {
    toast(`Creating ${count} profiles…`);
    const r = await api("/api/profiles/bulk", {
      method: "POST",
      body: JSON.stringify({ count, os: osChoice ? osChoice.trim() : null, seed_cookies: 15 }),
    });
    toast(`Created ${r.created} randomized profiles`);
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
});

$("#new-btn").addEventListener("click", () => openEditor(null));
$("#modal-close").addEventListener("click", closeEditor);
$("#modal-cancel").addEventListener("click", closeEditor);

$("#modal-save").addEventListener("click", async () => {
  const form = readForm();
  if (!form.name) return toast("Name is required", "err");
  try {
    if (editingId) {
      await api(`/api/profiles/${editingId}`, { method: "PATCH", body: JSON.stringify({
        name: form.name, start_url: form.start_url, proxy: form.proxy,
        humanize: form.humanize, block_webrtc: form.block_webrtc, geoip: form.geoip,
      })});
      toast("Saved");
    } else {
      await api("/api/profiles", { method: "POST", body: JSON.stringify(form) });
      toast("Profile created");
    }
    closeEditor();
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
});

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
      out.className = "proxy-result err";
      out.textContent = `✗ ${r.error}`;
    }
  } catch (e) { out.className = "proxy-result err"; out.textContent = `✗ ${e.message}`; }
});

// ------------------------------------------------------------------ cookies ---
async function cookieDialog(id) {
  const data = await api(`/api/profiles/${id}/cookies`);
  const n = data.cookies.length;
  const choice = prompt(
    `Profile has ${n} staged cookie(s).\n\n` +
    `Type a number to ADD that many random test cookies,\n` +
    `type 0 to CLEAR all cookies, or Cancel to close.`,
    "10"
  );
  if (choice === null) return;
  const count = parseInt(choice, 10);
  if (isNaN(count)) return;
  try {
    if (count === 0) {
      await api(`/api/profiles/${id}/cookies`, { method: "DELETE" });
      toast("Cookies cleared");
    } else {
      const r = await api(`/api/profiles/${id}/cookies/random`, {
        method: "POST", body: JSON.stringify({ count, domain: "example.com" }),
      });
      toast(`Added ${r.added} cookies (total ${r.count})`);
    }
    loadProfiles();
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------- engine setup ---
// On first run the Camoufox browser isn't downloaded yet. Show a blocking overlay
// with a live progress bar, trigger the one-time download, and poll until ready.
function fmtMB(bytes) { return (bytes / 1048576).toFixed(1) + " MB"; }

function renderEngine(s) {
  const bar = $("#progress-bar");
  const pct = $("#progress-pct");
  const detail = $("#progress-detail");
  const msg = $("#setup-msg");
  const errEl = $("#setup-err");
  const retry = $("#setup-retry");

  if (s.phase === "error" || s.error) {
    errEl.classList.remove("hidden");
    errEl.textContent = `Download failed: ${s.error || "unknown error"}`;
    msg.textContent = "Check your internet connection and try again.";
    retry.classList.remove("hidden");
    bar.classList.add("indeterminate");
    return;
  }
  retry.classList.add("hidden");
  errEl.classList.add("hidden");

  if (s.phase === "extracting") {
    msg.textContent = "Installing the browser engine (unpacking files)…";
    bar.classList.remove("indeterminate");
    if (s.total > 0) {
      bar.style.width = s.percent + "%";
      pct.textContent = s.percent + "%";
      detail.textContent = `${s.downloaded} / ${s.total} files`;
    } else {
      bar.classList.add("indeterminate");
      pct.textContent = "unpacking…";
      detail.textContent = "";
    }
  } else if (s.phase === "downloading") {
    msg.textContent = "Downloading the Camoufox browser (~500 MB, one-time setup).";
    if (s.total > 0) {
      bar.classList.remove("indeterminate");
      bar.style.width = s.percent + "%";
      pct.textContent = s.percent + "%";
      const speed = s.speed ? " · " + fmtMB(s.speed) + "/s" : "";
      let eta = "";
      if (s.speed > 0) {
        const secs = Math.round((s.total - s.downloaded) / s.speed);
        eta = secs > 90 ? ` · ~${Math.ceil(secs / 60)} min left` : ` · ~${secs}s left`;
      }
      detail.textContent = `${fmtMB(s.downloaded)} / ${fmtMB(s.total)}${speed}${eta}`;
    } else {
      bar.classList.add("indeterminate");
      pct.textContent = fmtMB(s.downloaded);
      detail.textContent = "starting…";
    }
  } else {
    bar.classList.add("indeterminate");
    pct.textContent = "";
    detail.textContent = "";
  }
}

async function ensureEngine() {
  const overlay = $("#setup");
  let status;
  try {
    status = await api("/api/engine/status?t=" + Date.now());
  } catch { return; } // server not ready; loadProfiles will retry anyway
  if (status.installed) return;

  overlay.classList.remove("hidden");
  const startDownload = () => api("/api/engine/ensure", { method: "POST" }).catch(() => {});
  $("#setup-retry").onclick = () => { autoRetries = 0; startDownload(); };
  await startDownload();

  const MAX_AUTO = 20;   // auto-resume through transient GitHub drops before giving up
  let autoRetries = 0;
  let handledError = false;

  await new Promise((resolve) => {
    const poll = setInterval(async () => {
      let s;
      try { s = await api("/api/engine/status?t=" + Date.now()); } catch { return; }

      // Self-heal: on a network error, auto-retry (which RESUMES from disk) a few
      // times before falling back to the manual Retry button.
      if (s.phase === "error" && !s.downloading) {
        if (!handledError && autoRetries < MAX_AUTO) {
          handledError = true;
          autoRetries++;
          $("#setup-msg").textContent = `Connection dropped — resuming (attempt ${autoRetries})…`;
          $("#setup-err").classList.add("hidden");
          $("#setup-retry").classList.add("hidden");
          setTimeout(async () => { await startDownload(); handledError = false; }, 3000);
          return;
        }
        renderEngine(s); // exhausted auto-retries → show manual Retry
        return;
      }
      if (s.phase !== "error") handledError = false;

      renderEngine(s);
      if (s.installed) {
        clearInterval(poll);
        overlay.classList.add("hidden");
        toast("Browser engine ready ✓");
        resolve();
      }
    }, 700);
  });
}

// ---------------------------------------------------------------------- init ---
// Go straight to the dashboard. The browser engine ships with the app (or is
// already installed), so we never block the UI on a setup/download screen. If the
// engine is genuinely missing, launching a profile surfaces a clear error instead.
$("#setup")?.classList.add("hidden");
$("#setup")?.remove();
loadProfiles();
setInterval(loadProfiles, 5000); // keep running status fresh
