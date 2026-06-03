// Options page: persist Host host/port/token and test the connection.
"use strict";

const $ = (id) => document.getElementById(id);

async function load() {
  const v = await chrome.storage.local.get(["host", "port", "token"]);
  $("host").value = v.host || "127.0.0.1";
  $("port").value = v.port || "5577";
  $("token").value = v.token || "dev-token";
}

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = cls || "muted";
}

async function save() {
  const host = $("host").value.trim() || "127.0.0.1";
  const port = parseInt($("port").value.trim(), 10) || 5577;
  const token = $("token").value.trim() || "dev-token";
  await chrome.storage.local.set({ host, port, token });
  setStatus("Saved.", "ok");
}

async function test() {
  await save();
  const host = $("host").value.trim();
  const port = $("port").value.trim();
  const token = $("token").value.trim();
  setStatus("Testing…", "muted");
  try {
    const res = await fetch(`http://${host}:${port}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Device-Token": token },
      body: JSON.stringify({
        source: "Bridge",
        title: "TaskHub Web Bridge connected",
        status: "done",
        ttl_sec: 20,
      }),
    });
    if (res.ok) {
      const j = await res.json().catch(() => ({}));
      setStatus("Connected ✓ (accepted " + (j.accepted ?? "?") + ")", "ok");
    } else if (res.status === 401) {
      setStatus("Reached Host but token rejected (401).", "err");
    } else {
      setStatus("Host responded " + res.status + ".", "err");
    }
  } catch (e) {
    setStatus("Cannot reach Host — is it running on this Mac?", "err");
  }
}

document.addEventListener("DOMContentLoaded", load);
$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
