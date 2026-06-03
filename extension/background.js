// TaskHub Web Bridge — background service worker.
//
// Receives {source, title, status, url} from content scripts and forwards each
// as a task to the local TaskHub Host (POST /ingest). One stable task id per
// browser tab, so refreshes replace rather than duplicate. A short ttl_sec plus
// a heartbeat means: while a tab stays open its task is kept alive; close the
// tab (heartbeat stops) and it ages out of the Host on its own.
"use strict";

const DEFAULTS = { host: "127.0.0.1", port: 5577, token: "dev-token" };
const TTL_SEC = 60;        // task expires on the Host this long after the last push
const HEARTBEAT_MS = 30000; // re-push live tabs well within TTL (Chrome alarm min is 30s)

// tabId -> { source, title, status, url }
const live = new Map();

async function config() {
  const v = await chrome.storage.local.get(["host", "port", "token"]);
  return {
    host: v.host || DEFAULTS.host,
    port: v.port || DEFAULTS.port,
    token: v.token || DEFAULTS.token,
  };
}

async function pushTask(tabId, payload) {
  const cfg = await config();
  const body = {
    id: "ext-" + payload.source.toLowerCase() + "-" + tabId,
    source: payload.source,
    title: payload.title,
    status: payload.status,
    subtitle: "browser · " + payload.source.toLowerCase(),
    url: payload.url,
    ttl_sec: TTL_SEC,
  };
  try {
    const res = await fetch(`http://${cfg.host}:${cfg.port}/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Device-Token": cfg.token,
      },
      body: JSON.stringify(body),
    });
    return res.ok;
  } catch (_) {
    return false; // Host not running / unreachable — silently retry next tick
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || msg.type !== "taskhub-task") return;
  const tabId = sender && sender.tab && sender.tab.id;
  if (tabId == null) return;
  if (!msg.title) return;
  const payload = {
    source: msg.source,
    title: String(msg.title).slice(0, 200),
    status: msg.status || "running",
    url: msg.url || "",
  };
  live.set(tabId, payload);
  pushTask(tabId, payload);
});

// Stop tracking a closed tab; its Host task expires via TTL shortly after.
chrome.tabs.onRemoved.addListener((tabId) => live.delete(tabId));

// Heartbeat: keep open tabs' tasks alive (TTL would otherwise expire them).
chrome.alarms.create("taskhub-heartbeat", { periodInMinutes: HEARTBEAT_MS / 60000 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "taskhub-heartbeat") return;
  for (const [tabId, payload] of live.entries()) {
    pushTask(tabId, payload);
  }
});
