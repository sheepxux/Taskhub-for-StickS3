/*
 * StickS3 AI task monitor.
 *
 * Battery-first workflow:
 *   wake -> Wi-Fi -> GET /tasks?format=stick -> show compact list -> sleep.
 *
 * BtnA: open the selected task on the Mac through Task Hub.
 * BtnB: next task.
 * BtnB hold: refresh now.
 *
 * The StickS3 does not scrape AI apps. The Mac-side Task Hub owns collection
 * and open actions. This keeps the device small, wireless, and low power.
 */

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>
#include <esp_wifi.h>           // for esp_wifi_set_ps (deeper modem sleep than WiFi.setSleep default)
#include <esp32-hal-cpu.h>
#include <driver/rtc_io.h>
#include <string.h>             // memcpy for BSSID cache

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "Using placeholder Wi-Fi/Task Hub config. Copy secrets.h.example to secrets.h."
#define WIFI_SSID       "your-wifi-ssid"
#define WIFI_PASSWORD   "your-wifi-password"
#define TASK_HUB_HOST   "192.168.1.100"
#define TASK_HUB_PORT   5577
#define DEVICE_ID       "sticks3-task-01"
#define DEVICE_TOKEN    "dev-token"
#define AUTO_WAKE_SECONDS 600
#endif

#if !defined(TASK_HUB_HOST) && defined(SERVER_HOST)
#define TASK_HUB_HOST SERVER_HOST
#endif

#if !defined(TASK_HUB_PORT) && defined(SERVER_PORT)
#define TASK_HUB_PORT SERVER_PORT
#endif

#if !defined(AUTO_WAKE_SECONDS)
#define AUTO_WAKE_SECONDS 600
#endif

#if !defined(DEVICE_ID)
#define DEVICE_ID "sticks3-task-01"
#endif

#if !defined(TASK_HUB_DISCOVERY_PORT)
#define TASK_HUB_DISCOVERY_PORT 5578
#endif

// Battery-first default. Override to 0 in secrets.h while debugging UI/network behavior.
#ifndef ENABLE_DEEP_SLEEP
#define ENABLE_DEEP_SLEEP 1
#endif

static constexpr int C_BG = TFT_BLACK;
static constexpr int C_WHITE = TFT_WHITE;
static constexpr int C_GRAY = TFT_DARKGREY;
static constexpr int C_GREEN = TFT_GREEN;
static constexpr int C_AMBER = TFT_ORANGE;
static constexpr int C_RED = TFT_RED;
static constexpr int C_LOVABLE_RED = 0xFA20;
static constexpr int C_LOVABLE_ORANGE = 0xFD20;
static constexpr int C_LOVABLE_SHADOW = 0x5BFF;
static constexpr int C_BLUE = 0x5BDF;
static constexpr int C_CARD = 0x1082;

static constexpr int MAX_TASKS = 10;
// Tightened: with a cached BSSID hint, a healthy join lands in <1s and the hub
// answers in <500ms on LAN. A wake that doesn't make it in these windows is
// almost certainly an outage — bail fast so the radio doesn't drain the cell.
static constexpr uint32_t WIFI_TIMEOUT_MS = 5000;
static constexpr uint32_t HTTP_TIMEOUT_MS = 3000;
static constexpr uint32_t DISCOVERY_TIMEOUT_MS = 900;
static constexpr uint32_t DISCOVERY_REFRESH_MS = 300000;

#if !defined(INTERACTIVE_TIMEOUT_MS)
#define INTERACTIVE_TIMEOUT_MS 10000
#endif

#if !defined(QUIET_TIMER_TIMEOUT_MS)
#define QUIET_TIMER_TIMEOUT_MS 3000
#endif

#if !defined(ACTIVE_WAKE_SECONDS)
// A WAIT almost always appears while a task is already running, so the device
// is most likely deep-sleeping with active/attention tasks when one shows up.
// 60s (was 180s) caps the worst-case "turned to WAIT" latency to ~1 min while
// staying battery-first — active windows are bounded, so the 3x wake frequency
// only applies briefly. Tune up for more battery, down for snappier alerts.
#define ACTIVE_WAKE_SECONDS 60
#endif

#if !defined(LOW_BATTERY_WAKE_SECONDS)
#define LOW_BATTERY_WAKE_SECONDS 900
#endif

#if !defined(LOW_BATTERY_THRESHOLD_PCT)
#define LOW_BATTERY_THRESHOLD_PCT 30
#endif

#if !defined(DISPLAY_BRIGHTNESS)
#define DISPLAY_BRIGHTNESS 32
#endif

#if !defined(LOW_BATTERY_BRIGHTNESS)
#define LOW_BATTERY_BRIGHTNESS 16
#endif

#if !defined(POWER_SAVE_CPU_MHZ)
#define POWER_SAVE_CPU_MHZ 80
#endif

#if !defined(CHARGE_CURRENT_MA)
#define CHARGE_CURRENT_MA 200
#endif

#if !defined(AWAKE_REFRESH_IDLE_MS)
#define AWAKE_REFRESH_IDLE_MS 30000
#endif

#if !defined(AWAKE_REFRESH_ACTIVE_MS)
#define AWAKE_REFRESH_ACTIVE_MS 5000
#endif

#if !defined(AWAKE_REFRESH_WAIT_MS)
#define AWAKE_REFRESH_WAIT_MS 5000
#endif

#if !defined(MANUAL_SELECTION_HOLD_MS)
#define MANUAL_SELECTION_HOLD_MS 10000
#endif

#if !defined(BTN_B_DEBOUNCE_MS)
#define BTN_B_DEBOUNCE_MS 35
#endif

#if !defined(BTN_B_HOLD_MS)
#define BTN_B_HOLD_MS 560
#endif

#if !defined(AUTO_REFRESH_INPUT_GUARD_MS)
#define AUTO_REFRESH_INPUT_GUARD_MS 1000
#endif

// Edge-triggered audible alerts, fired once per transition (including when a
// timer wake first observes it). The screen wakes and shows the task; the WAIT
// row renders amber on its own, so there is no full-screen flash.
//   - WAIT: a session is asking for human input (two urgent high beeps).
//   - DONE: a running task just finished, i.e. a turn completed (rising chime).
// Beeps use the StickS3 speaker (M5.Speaker). Vibration is left as a future
// hook: the pinned M5Unified does NOT drive a motor on board_M5StickS3
// (setVibration is a no-op there), so it stays off by default.
#if !defined(ALERT_ON_WAIT)
#define ALERT_ON_WAIT 1
#endif
#if !defined(ALERT_ON_DONE)
#define ALERT_ON_DONE 1
#endif
#if !defined(ALERT_BEEP)
#define ALERT_BEEP 1
#endif
#if !defined(ALERT_WAIT_HZ)
#define ALERT_WAIT_HZ 2400
#endif
#if !defined(ALERT_DONE_HZ)
#define ALERT_DONE_HZ 1500
#endif
#if !defined(ALERT_BEEP_VOLUME)
#define ALERT_BEEP_VOLUME 150
#endif
#if !defined(ALERT_VIBRATION)
#define ALERT_VIBRATION 0
#endif
#if !defined(ALERT_VIBRATION_LEVEL)
#define ALERT_VIBRATION_LEVEL 200
#endif
#if !defined(ALERT_VIBRATION_MS)
#define ALERT_VIBRATION_MS 180
#endif

#if !defined(STICK_HIDE_DONE_AFTER_SEC)
#define STICK_HIDE_DONE_AFTER_SEC 600
#endif

#if !defined(STICK_HIDE_IDLE_AFTER_SEC)
#define STICK_HIDE_IDLE_AFTER_SEC 600
#endif

#if !defined(STICK_HIDE_RECENT_AFTER_SEC)
#define STICK_HIDE_RECENT_AFTER_SEC 3600
#endif

#if !defined(STICK_HIDE_UNKNOWN_AFTER_SEC)
#define STICK_HIDE_UNKNOWN_AFTER_SEC 1800
#endif

static constexpr gpio_num_t PIN_BTN_A = GPIO_NUM_11;
static constexpr gpio_num_t PIN_BTN_B = GPIO_NUM_12;

// Persisted across deep sleep in RTC slow memory (~8KB available, free).
// After a successful join we stash the AP's BSSID + channel; on the next wake
// WiFi.begin() can target the radio directly instead of doing a full
// passive scan. Drops connect time from ~1.5-3s to ~0.5s, which is the
// single biggest awake-time win on a battery-bound device.
RTC_DATA_ATTR static uint8_t  rtcCachedBssid[6] = {0};
RTC_DATA_ATTR static int32_t  rtcCachedChannel = 0;
// Tracks whether a WAIT was present at the last refresh, persisted across deep
// sleep so the empty->WAIT edge alert fires once even when the transition is
// first observed on a timer wake (rather than re-alerting every refresh).
RTC_DATA_ATTR static bool     rtcWaitWasActive = false;
// Tracks whether a task was running at the last refresh, so a running->finished
// transition (turn complete) can fire a one-shot DONE chime, persisted across
// deep sleep like the WAIT edge above.
RTC_DATA_ATTR static bool     rtcWasRunning = false;
RTC_DATA_ATTR static bool     rtcHasCachedBssid = false;

struct AiTask {
  String id;
  String source;
  String title;
  String status;
  String subtitle;
  String usage;
  String device;
  bool attention = false;
  uint32_t ageSec = 0;
};

static AiTask tasks[MAX_TASKS];
static int taskCount = 0;
static int selected = 0;
static int activeCount = 0;
static int attentionCount = 0;
static int waitCount = 0;
static int runCount = 0;   // tasks with status "run" (used to detect turn completion)
static int totalCount = 0;
static int hiddenCount = 0;
static int battPct = 100;
static bool wifiOk = false;
static bool wokeByTimer = false;
static bool wokeFromSleep = false;
static uint32_t lastInputAt = 0;
static uint32_t lastManualSelectAt = 0;
static uint32_t lastRefreshAt = 0;
static uint32_t lastDiscoveryAt = 0;
static uint32_t activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
static String hubHost;
static int hubPort = TASK_HUB_PORT;
static bool hubDiscovered = false;
static String lastError;
static bool btnBReadingPressed = false;
static bool btnBStablePressed = false;
static bool btnBHoldFired = false;
static bool btnBClickEvent = false;
static bool btnBHoldEvent = false;
static uint32_t btnBLastChangeAt = 0;
static uint32_t btnBPressedAt = 0;
static bool bootScreenActive = false;
static String bootStatusText;

static void setBootStatus(const String& text, int color);
static void topBar();
static void centerText(const String& text, int y, int color, const lgfx::IFont* font);

static bool lowBatteryMode() {
  return battPct >= 0 && battPct <= LOW_BATTERY_THRESHOLD_PCT;
}

static uint8_t clampBrightness(int value) {
  if (value < 0) return 0;
  if (value > 255) return 255;
  return (uint8_t)value;
}

static uint8_t displayBrightness() {
  return lowBatteryMode() ? clampBrightness(LOW_BATTERY_BRIGHTNESS) : clampBrightness(DISPLAY_BRIGHTNESS);
}

static void applyDisplayBrightness() {
  M5.Display.setBrightness(displayBrightness());
}

static void applyPowerProfile() {
#if POWER_SAVE_CPU_MHZ > 0
  setCpuFrequencyMhz(POWER_SAVE_CPU_MHZ);
#endif
#if CHARGE_CURRENT_MA > 0
  M5.Power.setBatteryCharge(true);
  M5.Power.setChargeCurrent(CHARGE_CURRENT_MA);
#endif
  applyDisplayBrightness();
}

static uint32_t nextWakeSeconds() {
  if (lowBatteryMode()) return LOW_BATTERY_WAKE_SECONDS;
  if (waitCount > 0 || activeCount > 0 || attentionCount > 0) return ACTIVE_WAKE_SECONDS;
  return AUTO_WAKE_SECONDS;
}

static String apiBase() {
  String host = hubHost.length() ? hubHost : String(TASK_HUB_HOST);
  return String("http://") + host + ":" + String(hubPort);
}

static uint32_t awakeRefreshMs() {
  if (waitCount > 0) return AWAKE_REFRESH_WAIT_MS;
  return (activeCount > 0 || attentionCount > 0) ? AWAKE_REFRESH_ACTIVE_MS : AWAKE_REFRESH_IDLE_MS;
}

static String urlEncode(const String& s) {
  const char* hex = "0123456789ABCDEF";
  String out;
  for (size_t i = 0; i < s.length(); i++) {
    uint8_t c = (uint8_t)s[i];
    bool safe = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    if (safe) {
      out += (char)c;
    } else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 0x0F];
    }
  }
  return out;
}

// Connect with the cached BSSID/channel hint if we have one; on failure fall
// back to a full scan. Picks the deepest modem-sleep level once associated so
// the brief idle awake window also draws less current.
static bool ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiOk = true;
    setBootStatus("wifi ok", C_GREEN);
    return true;
  }

  setBootStatus("wifi...", C_BLUE);
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(true);

  auto waitForJoin = [](uint32_t budgetMs) {
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < budgetMs) {
      M5.update();
      delay(50);
    }
  };

  if (rtcHasCachedBssid && rtcCachedChannel > 0) {
    // Fast path: aim at the known AP. Most wakes land here, in well under 1s.
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, rtcCachedChannel, rtcCachedBssid);
    waitForJoin(2500);                      // generous enough for a slow router
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.disconnect(false, false);        // hint stale — fall through to scan
      rtcHasCachedBssid = false;
    }
  }
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    waitForJoin(WIFI_TIMEOUT_MS);
  }

  wifiOk = WiFi.status() == WL_CONNECTED;
  if (wifiOk) {
    setBootStatus("wifi ok", C_GREEN);
    // Cache for next wake.
    const uint8_t* bssid = WiFi.BSSID();
    if (bssid) {
      memcpy(rtcCachedBssid, bssid, 6);
      rtcCachedChannel = WiFi.channel();
      rtcHasCachedBssid = true;
    }
    // Deepest modem sleep level the driver allows. The default sleep(true)
    // is MIN_MODEM; MAX_MODEM aligns DTIM more aggressively and trims a few
    // mA off the awake-idle window.
    esp_wifi_set_ps(WIFI_PS_MAX_MODEM);
  } else {
    setBootStatus("wifi failed", C_RED);
  }
  return wifiOk;
}

static bool discoverHub(bool force) {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!force && hubDiscovered && millis() - lastDiscoveryAt < DISCOVERY_REFRESH_MS) {
    return true;
  }

  setBootStatus("hub...", C_BLUE);
  lastDiscoveryAt = millis();
  WiFiUDP udp;
  if (!udp.begin(0)) {
    Serial.println("[task-monitor] discovery failed: udp");
    return false;
  }

  JsonDocument req;
  req["type"] = "sticks3.discover";
  req["device"] = DEVICE_ID;
  req["token"] = DEVICE_TOKEN;
  String packet;
  serializeJson(req, packet);

  IPAddress broadcast(255, 255, 255, 255);
  udp.beginPacket(broadcast, TASK_HUB_DISCOVERY_PORT);
  udp.print(packet);
  udp.endPacket();

  uint32_t start = millis();
  while (millis() - start < DISCOVERY_TIMEOUT_MS) {
    int size = udp.parsePacket();
    if (size <= 0) {
      delay(25);
      continue;
    }

    char buf[256];
    int len = udp.read(buf, sizeof(buf) - 1);
    if (len <= 0) continue;
    buf[len] = 0;

    JsonDocument resp;
    DeserializationError err = deserializeJson(resp, buf);
    if (err) continue;
    const char* type = resp["type"] | "";
    if (String(type) != "sticks3.hub") continue;
    if (!(bool)(resp["ok"] | false)) continue;

    String host = resp["host"].as<String>();
    int port = resp["port"] | TASK_HUB_PORT;
    if (!host.length()) host = udp.remoteIP().toString();
    if (!host.length() || port <= 0) continue;

    hubHost = host;
    hubPort = port;
    hubDiscovered = true;
    Serial.printf("[task-monitor] discovery ok host=%s port=%d\n", hubHost.c_str(), hubPort);
    setBootStatus("hub ok", C_GREEN);
    udp.stop();
    return true;
  }

  udp.stop();
  Serial.printf("[task-monitor] discovery fallback host=%s port=%d\n", hubHost.c_str(), hubPort);
  setBootStatus("hub fallback", C_AMBER);
  return false;
}

static void updateBattery() {
  int b = M5.Power.getBatteryLevel();
  if (b >= 0 && b <= 100) battPct = b;
  applyDisplayBrightness();
}

static int statusColor(const String& status) {
  if (status == "wait") return C_AMBER;
  if (status == "fail") return C_RED;
  if (status == "run") return C_BLUE;
  if (status == "done") return C_GREEN;
  if (status == "rec") return C_WHITE;
  return C_GRAY;
}

static const char* statusLabel(const String& status) {
  if (status == "wait") return "WAIT";
  if (status == "fail") return "FAIL";
  if (status == "run") return "RUN";
  if (status == "done") return "DONE";
  if (status == "rec") return "REC";
  if (status == "idle") return "IDLE";
  return "UNK";
}

static String ageLabel(uint32_t sec) {
  if (sec < 60) return String(sec) + "s";
  if (sec < 3600) return String(sec / 60) + "m";
  if (sec < 86400) return String(sec / 3600) + "h";
  return String(sec / 86400) + "d";
}

static int nextUtf8Index(const String& s, int idx) {
  if (idx >= (int)s.length()) return s.length();
  uint8_t c = (uint8_t)s[idx];
  int step = 1;
  if ((c & 0xE0) == 0xC0) step = 2;
  else if ((c & 0xF0) == 0xE0) step = 3;
  else if ((c & 0xF8) == 0xF0) step = 4;
  int next = idx + step;
  return next > (int)s.length() ? s.length() : next;
}

static int prevUtf8Index(const String& s, int idx) {
  if (idx <= 0) return 0;
  idx--;
  while (idx > 0 && (((uint8_t)s[idx] & 0xC0) == 0x80)) idx--;
  return idx;
}

static String trimmedCopy(String s) {
  s.trim();
  return s;
}

static String fitText(String text, const lgfx::IFont* font, int maxWidth) {
  text = trimmedCopy(text);
  if (!text.length()) return "";
  M5.Display.setFont(font);
  if (M5.Display.textWidth(text) <= maxWidth) return text;

  const String suffix = "...";
  int end = text.length();
  while (end > 0) {
    end = prevUtf8Index(text, end);
    String candidate = text.substring(0, end) + suffix;
    if (M5.Display.textWidth(candidate) <= maxWidth) return candidate;
  }
  return suffix;
}

static void drawFittedText(const String& text, int x, int y, int maxWidth, int color, int bg, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(color, bg);
  M5.Display.drawString(fitText(text, font, maxWidth), x, y);
}

static void drawWrappedText(const String& text, int x, int y, int maxWidth, int lineHeight,
                            int maxLines, int color, int bg, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(color, bg);

  String rest = trimmedCopy(text);
  for (int line = 0; line < maxLines && rest.length(); line++) {
    if (line == maxLines - 1 || M5.Display.textWidth(rest) <= maxWidth) {
      M5.Display.drawString(fitText(rest, font, maxWidth), x, y + line * lineHeight);
      return;
    }

    int best = 0;
    int lastBreak = 0;
    for (int idx = 0; idx < (int)rest.length();) {
      int next = nextUtf8Index(rest, idx);
      String candidate = rest.substring(0, next);
      if (M5.Display.textWidth(candidate) > maxWidth) break;
      best = next;
      char ch = rest[idx];
      if (ch == ' ' || ch == '-' || ch == '/' || ch == '_') lastBreak = next;
      idx = next;
    }
    if (lastBreak > 0 && lastBreak > best / 2) best = lastBreak;
    if (best <= 0) best = nextUtf8Index(rest, 0);

    String lineText = trimmedCopy(rest.substring(0, best));
    M5.Display.drawString(fitText(lineText, font, maxWidth), x, y + line * lineHeight);
    rest = trimmedCopy(rest.substring(best));
  }
}

static bool isPriorityTask(const AiTask& t) {
  return t.attention || t.status == "run" || t.status == "wait" || t.status == "fail";
}

static bool hasWaitingTasks() {
  return waitCount > 0;
}

// Play two sequential tones on the speaker. tone() is non-blocking, so the
// delays keep each note audible before the next; alerts are rare (edge-only),
// so the brief block is fine. ensureSpeakerReady() guarantees the I2S amp is up.
static void alertBeep2(int hz1, int ms1, int gap, int hz2, int ms2) {
#if ALERT_BEEP
  M5.Speaker.begin();                 // idempotent; re-arms the amp after sleep
  M5.Speaker.setVolume(ALERT_BEEP_VOLUME);
  M5.Speaker.tone(hz1, ms1);
  delay(ms1 + gap);
  M5.Speaker.tone(hz2, ms2);
  delay(ms2 + 20);
  M5.Speaker.stop();
#else
  (void)hz1; (void)ms1; (void)gap; (void)hz2; (void)ms2;
#endif
}

static void alertVibrateHook() {
#if ALERT_VIBRATION
  // No-op on board_M5StickS3 in the pinned M5Unified; kept as a future hook.
  M5.Power.setVibration(ALERT_VIBRATION_LEVEL);
  delay(ALERT_VIBRATION_MS);
  M5.Power.setVibration(0);
#endif
}

// WAIT entry: two urgent same-pitch beeps. Caller repaints the amber row.
static void alertWait() {
  M5.Display.wakeup();
  applyDisplayBrightness();
  alertBeep2(ALERT_WAIT_HZ, 90, 55, ALERT_WAIT_HZ, 120);
  alertVibrateHook();
}

// Turn complete (a running task finished): a gentler rising two-note chime.
static void alertDone() {
  M5.Display.wakeup();
  applyDisplayBrightness();
  alertBeep2(ALERT_DONE_HZ, 80, 50, ALERT_DONE_HZ + 500, 140);
  alertVibrateHook();
}

// Edge detector for both alerts. WAIT fires on empty->WAIT; DONE fires when a
// running task disappears with nothing now waiting (a turn just finished).
static void updateAlerts() {
  bool waitActive = waitCount > 0;
  bool running = runCount > 0;
#if ALERT_ON_WAIT
  if (waitActive && !rtcWaitWasActive) {
    alertWait();
  }
#endif
#if ALERT_ON_DONE
  if (rtcWasRunning && !running && !waitActive && taskCount > 0) {
    alertDone();
  }
#endif
  rtcWaitWasActive = waitActive;
  rtcWasRunning = running;
}

static uint32_t hideAfterSec(const String& status) {
  if (status == "run" || status == "wait" || status == "fail") return 0;
  if (status == "done") return STICK_HIDE_DONE_AFTER_SEC;
  if (status == "idle") return STICK_HIDE_IDLE_AFTER_SEC;
  if (status == "rec") return STICK_HIDE_RECENT_AFTER_SEC;
  return STICK_HIDE_UNKNOWN_AFTER_SEC;
}

static bool shouldShowOnStick(const String& status, uint32_t ageSec) {
  uint32_t maxAge = hideAfterSec(status);
  return maxAge == 0 || ageSec <= maxAge;
}

static int findTaskById(const String& id) {
  if (!id.length()) return -1;
  for (int i = 0; i < taskCount; i++) {
    if (tasks[i].id == id) return i;
  }
  return -1;
}

static int firstPriorityTask() {
  for (int i = 0; i < taskCount; i++) {
    if (isPriorityTask(tasks[i])) return i;
  }
  return -1;
}

static void clearStaleWaitSnapshot() {
  taskCount = 0;
  selected = 0;
  activeCount = 0;
  attentionCount = 0;
  waitCount = 0;
  runCount = 0;
  hiddenCount = 0;
}

static void updateBtnBEdge() {
  uint32_t now = millis();
  bool reading = digitalRead((int)PIN_BTN_B) == LOW;
  if (reading != btnBReadingPressed) {
    btnBReadingPressed = reading;
    btnBLastChangeAt = now;
  }

  if (now - btnBLastChangeAt >= BTN_B_DEBOUNCE_MS && reading != btnBStablePressed) {
    btnBStablePressed = reading;
    if (btnBStablePressed) {
      btnBPressedAt = now;
      btnBHoldFired = false;
      lastInputAt = now;
    } else if (!btnBHoldFired && btnBPressedAt != 0) {
      btnBClickEvent = true;
    }
  }

  if (btnBStablePressed && !btnBHoldFired && btnBPressedAt != 0 && now - btnBPressedAt >= BTN_B_HOLD_MS) {
    btnBHoldFired = true;
    btnBHoldEvent = true;
    lastInputAt = now;
  }
}

static void drawTaskHubMark(int x, int y, int scale, int color) {
  auto px = [&](int px, int py, int w, int h) {
    M5.Display.fillRect(x + px * scale, y + py * scale, w * scale, h * scale, color);
  };

  // 24x22 pixel computer mark derived from the TaskHub logo.
  px(6, 1, 15, 1);
  px(5, 2, 1, 11);
  px(20, 2, 1, 12);
  px(6, 13, 15, 1);
  px(21, 3, 2, 1);
  px(22, 4, 1, 10);
  px(21, 14, 2, 1);

  px(8, 4, 12, 1);
  px(8, 5, 1, 8);
  px(19, 5, 1, 8);
  px(9, 12, 10, 1);
  px(12, 7, 4, 1);
  px(10, 9, 8, 1);
  px(11, 11, 6, 1);

  px(4, 14, 17, 1);
  px(3, 15, 1, 4);
  px(21, 15, 1, 4);
  px(4, 18, 17, 1);
  px(6, 16, 2, 1);
  px(17, 16, 5, 1);
  px(22, 15, 1, 3);
  px(20, 19, 3, 1);

  px(3, 18, 1, 1);
  px(2, 19, 1, 1);
  px(1, 20, 1, 1);
  px(0, 21, 20, 1);
  px(20, 19, 1, 1);
  px(19, 20, 1, 1);
}

static void drawTaskHubMiniMark(int x, int y, int color) {
  M5.Display.drawRect(x + 4, y, 9, 7, color);
  M5.Display.drawRect(x + 5, y + 2, 7, 4, color);
  M5.Display.drawLine(x + 13, y + 1, x + 15, y + 3, color);
  M5.Display.drawLine(x + 15, y + 3, x + 15, y + 9, color);
  M5.Display.drawLine(x + 4, y + 8, x + 14, y + 8, color);
  M5.Display.drawRect(x + 3, y + 9, 13, 3, color);
  M5.Display.drawLine(x + 3, y + 12, x + 1, y + 15, color);
  M5.Display.drawLine(x + 16, y + 12, x + 13, y + 15, color);
  M5.Display.drawLine(x + 1, y + 15, x + 13, y + 15, color);
  M5.Display.drawFastHLine(x + 7, y + 3, 3, color);
  M5.Display.drawFastHLine(x + 7, y + 5, 5, color);
}

static int sourceLogoColor(const String& source) {
  String s = source;
  s.toLowerCase();
  if (s.indexOf("codex") >= 0) return C_BLUE;
  if (s.indexOf("claude") >= 0) return C_AMBER;
  if (s.indexOf("perplexity") >= 0) return C_WHITE;
  if (s.indexOf("gemini") >= 0) return C_BLUE;
  if (s.indexOf("lovable") >= 0) return C_LOVABLE_RED;
  if (s.indexOf("manus") >= 0) return C_GREEN;
  if (s.indexOf("openclaw") >= 0 || s.indexOf("claw") >= 0) return C_RED;
  return C_GRAY;
}

static void drawAiSourceIcon(const String& source, int x, int y, int bg) {
  String s = source;
  s.toLowerCase();
  int c = sourceLogoColor(source);
  M5.Display.fillRect(x, y, 12, 12, bg);

  if (s.indexOf("codex") >= 0) {
    M5.Display.fillCircle(x + 4, y + 5, 3, c);
    M5.Display.fillCircle(x + 7, y + 5, 4, c);
    M5.Display.fillCircle(x + 8, y + 8, 3, c);
    M5.Display.fillRect(x + 2, y + 5, 8, 6, c);
    M5.Display.drawLine(x + 3, y + 4, x + 5, y + 6, C_WHITE);
    M5.Display.drawLine(x + 5, y + 6, x + 3, y + 8, C_WHITE);
    M5.Display.drawFastHLine(x + 7, y + 8, 3, C_WHITE);
  } else if (s.indexOf("claude") >= 0) {
    M5.Display.drawFastVLine(x + 6, y + 1, 10, c);
    M5.Display.drawFastHLine(x + 1, y + 6, 10, c);
    M5.Display.drawLine(x + 3, y + 3, x + 9, y + 9, c);
    M5.Display.drawLine(x + 9, y + 3, x + 3, y + 9, c);
  } else if (s.indexOf("perplexity") >= 0) {
    M5.Display.drawFastVLine(x + 6, y + 0, 12, c);
    M5.Display.drawFastHLine(x + 1, y + 6, 11, c);
    M5.Display.drawLine(x + 2, y + 1, x + 6, y + 5, c);
    M5.Display.drawLine(x + 10, y + 1, x + 6, y + 5, c);
    M5.Display.drawLine(x + 2, y + 11, x + 6, y + 7, c);
    M5.Display.drawLine(x + 10, y + 11, x + 6, y + 7, c);
    M5.Display.drawLine(x + 2, y + 1, x + 2, y + 11, c);
    M5.Display.drawLine(x + 10, y + 1, x + 10, y + 11, c);
  } else if (s.indexOf("gemini") >= 0) {
    M5.Display.fillTriangle(x + 6, y + 0, x + 8, y + 5, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 6, y + 0, x + 4, y + 5, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 6, y + 11, x + 8, y + 7, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 6, y + 11, x + 4, y + 7, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 0, y + 6, x + 5, y + 4, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 11, y + 6, x + 7, y + 4, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 0, y + 6, x + 5, y + 8, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 11, y + 6, x + 7, y + 8, x + 6, y + 6, c);
  } else if (s.indexOf("lovable") >= 0) {
    M5.Display.fillRect(x + 2, y + 8, 7, 3, C_LOVABLE_SHADOW);
    M5.Display.fillCircle(x + 4, y + 4, 3, C_LOVABLE_ORANGE);
    M5.Display.fillCircle(x + 8, y + 4, 3, C_LOVABLE_RED);
    M5.Display.fillTriangle(x + 1, y + 5, x + 11, y + 5, x + 6, y + 11, C_LOVABLE_RED);
    M5.Display.fillTriangle(x + 2, y + 5, x + 7, y + 5, x + 6, y + 10, C_LOVABLE_ORANGE);
    M5.Display.drawPixel(x + 6, y + 2, C_LOVABLE_RED);
    M5.Display.drawPixel(x + 0, y + 2, bg);
    M5.Display.drawPixel(x + 11, y + 2, bg);
  } else if (s.indexOf("manus") >= 0) {
    M5.Display.drawFastVLine(x + 2, y + 2, 9, c);
    M5.Display.drawFastVLine(x + 10, y + 2, 9, c);
    M5.Display.drawLine(x + 3, y + 3, x + 6, y + 7, c);
    M5.Display.drawLine(x + 9, y + 3, x + 6, y + 7, c);
  } else if (s.indexOf("openclaw") >= 0 || s.indexOf("claw") >= 0) {
    M5.Display.drawLine(x + 3, y + 1, x + 1, y + 0, c);
    M5.Display.drawLine(x + 9, y + 1, x + 11, y + 0, c);
    M5.Display.fillCircle(x + 6, y + 6, 5, c);
    M5.Display.fillCircle(x + 1, y + 6, 2, c);
    M5.Display.fillCircle(x + 11, y + 6, 2, c);
    M5.Display.fillRect(x + 4, y + 10, 2, 2, c);
    M5.Display.fillRect(x + 7, y + 10, 2, 2, c);
    M5.Display.fillCircle(x + 4, y + 5, 1, C_BG);
    M5.Display.fillCircle(x + 8, y + 5, 1, C_BG);
  } else {
    M5.Display.drawRect(x + 1, y + 1, 10, 10, c);
    M5.Display.drawLine(x + 3, y + 9, x + 6, y + 2, c);
    M5.Display.drawLine(x + 6, y + 2, x + 9, y + 9, c);
    M5.Display.drawFastHLine(x + 4, y + 6, 5, c);
  }
}

static void drawBootScreen(const String& status) {
  bootScreenActive = true;
  bootStatusText = "";
  M5.Display.fillScreen(C_BG);
  int scale = 3;
  int iconW = 24 * scale;
  int iconH = 22 * scale;
  int iconX = (M5.Display.width() - iconW) / 2;
  int iconY = 8;
  drawTaskHubMark(iconX, iconY, scale, C_BLUE);

  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::efontCN_16);
  M5.Display.setTextColor(C_BLUE, C_BG);
  M5.Display.drawString("TaskHub", M5.Display.width() / 2, iconY + iconH + 15);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.drawString("Developed by Axu", M5.Display.width() / 2, iconY + iconH + 30);
  setBootStatus(status, C_GRAY);
}

static void drawWakeSyncScreen(const String& status) {
  bootScreenActive = true;
  bootStatusText = "";
  M5.Display.fillScreen(C_BG);
  topBar();
  centerText("连接 Wi-Fi", 56, C_BLUE, &fonts::efontCN_16);
  centerText("同步任务状态", 84, C_GRAY, &fonts::efontCN_12);
  setBootStatus(status, C_GRAY);
}

static void setBootStatus(const String& text, int color) {
  if (!bootScreenActive || text == bootStatusText) return;
  bootStatusText = text;
  int y = M5.Display.height() - 18;
  M5.Display.fillRect(0, y - 4, M5.Display.width(), 22, C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(color, C_BG);
  M5.Display.drawString(text, M5.Display.width() / 2, y);
}

static void topBar() {
  M5.Display.fillRect(0, 0, M5.Display.width(), 20, C_BG);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(C_GRAY, C_BG);
  drawTaskHubMiniMark(5, 2, C_BLUE);
  M5.Display.drawString("TaskHub", 26, 6);

  M5.Display.setTextDatum(top_right);
  M5.Display.setTextColor(wifiOk ? C_GREEN : C_AMBER, C_BG);
  M5.Display.drawString(wifiOk ? "wifi" : "net", M5.Display.width() - 38, 6);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.drawString(String(battPct) + "%", M5.Display.width() - 6, 6);
}

static void centerText(const String& text, int y, int color, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextColor(color, C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString(text, M5.Display.width() / 2, y);
}

static void drawMessage(const String& line1, const String& line2, int color) {
  M5.Display.fillScreen(C_BG);
  topBar();
  centerText(line1, 58, color, &fonts::efontCN_16);
  centerText(line2, 88, C_GRAY, &fonts::efontCN_12);
}

static void drawList() {
  M5.Display.fillScreen(C_BG);
  topBar();

  if (taskCount == 0) {
    bool allHidden = !lastError.length() && hiddenCount > 0;
    centerText(lastError.length() ? "无法读取任务" : (allHidden ? "旧任务已隐藏" : "暂无任务"),
               56, lastError.length() ? C_RED : C_GRAY, &fonts::efontCN_16);
    centerText(lastError.length() ? lastError : (allHidden ? String(hiddenCount) + " hidden · 会自动刷新" : "会定时自动刷新"),
               88, C_GRAY, &fonts::efontCN_12);
    centerText("BtnB 刷新", 122, C_GRAY, &fonts::efontCN_12);
    return;
  }

  if (selected >= taskCount) selected = 0;
  AiTask& t = tasks[selected];
  int col = statusColor(t.status);
  int screenW = M5.Display.width();
  int screenH = M5.Display.height();
  int cardX = 6;
  int cardY = 23;
  int cardW = screenW - 12;
  int cardH = screenH - 43;
  int contentX = cardX + 14;
  int contentW = cardW - 24;

  M5.Display.fillRoundRect(cardX, cardY, cardW, cardH, 7, C_CARD);
  M5.Display.fillRoundRect(cardX, cardY, 8, cardH, 7, col);

  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(col, C_CARD);
  M5.Display.drawString(statusLabel(t.status), contentX, cardY + 8);
  int sourceIconX = contentX + 38;
  int sourceTextX = sourceIconX + 15;
  drawAiSourceIcon(t.source, sourceIconX, cardY + 7, C_CARD);
  String sourceLabel = t.source;
  if (t.device.length()) sourceLabel += "@" + t.device;
  drawFittedText(sourceLabel, sourceTextX, cardY + 8, screenW - sourceTextX - 58, C_GRAY, C_CARD, &fonts::Font0);

  M5.Display.setTextDatum(top_right);
  M5.Display.setTextColor(C_GRAY, C_CARD);
  M5.Display.drawString(ageLabel(t.ageSec), screenW - 16, cardY + 8);

  drawWrappedText(t.title, contentX, cardY + 28, contentW, 18, 2, C_WHITE, C_CARD, &fonts::efontCN_16);

  String meta = t.subtitle.length() ? t.subtitle : t.usage;
  drawFittedText(meta, contentX, cardY + cardH - 22, contentW, C_GRAY, C_CARD, &fonts::efontCN_12);

  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(t.usage.length() ? C_AMBER : (attentionCount > 0 ? C_AMBER : C_GRAY), C_BG);
  M5.Display.setTextDatum(bottom_left);
  String footerLeft = t.usage.length() ? t.usage : String(activeCount) + " active · " + String(attentionCount) + " alert";
  if (!t.usage.length() && waitCount > 0) footerLeft += " · " + String(waitCount) + " wait";
  if (!t.usage.length() && hiddenCount > 0) footerLeft += " · " + String(hiddenCount) + " hidden";
  M5.Display.drawString(fitText(footerLeft, &fonts::Font0, screenW - 78), 6, screenH - 3);

  M5.Display.setTextDatum(bottom_right);
  M5.Display.setTextColor(C_GRAY, C_BG);
  String footerRight = String(selected + 1) + "/" + String(taskCount) + " A";
  M5.Display.drawString(footerRight, screenW - 6, screenH - 3);
}

static bool fetchTasks() {
  lastError = "";
  String previousSelectedId = (taskCount > 0 && selected < taskCount) ? tasks[selected].id : "";
  bool previousHadWait = waitCount > 0;
  if (!ensureWifi()) {
    lastError = "Wi-Fi failed";
    if (previousHadWait) clearStaleWaitSnapshot();
    Serial.println("[task-monitor] fetch failed: wifi");
    return false;
  }

  discoverHub(false);

  String body;
  int code = -1;
  bool requestOpen = false;
  HTTPClient http;
  String url = apiBase() + "/tasks?format=stick&limit=" + String(MAX_TASKS);
  setBootStatus("sync...", C_BLUE);
  http.begin(url);
  requestOpen = true;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  code = http.GET();
  if (code != 200) {
    http.end();
    requestOpen = false;
    if (discoverHub(true)) {
      url = apiBase() + "/tasks?format=stick&limit=" + String(MAX_TASKS);
      http.begin(url);
      requestOpen = true;
      http.setTimeout(HTTP_TIMEOUT_MS);
      http.addHeader("X-Device-Token", DEVICE_TOKEN);
      code = http.GET();
    }
  }

  if (code != 200) {
    lastError = String("HTTP ") + String(code);
    setBootStatus(lastError, C_RED);
    if (previousHadWait) clearStaleWaitSnapshot();
    Serial.printf("[task-monitor] fetch failed: http=%d url=%s\n", code, url.c_str());
    if (requestOpen) http.end();
    return false;
  }

  body = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    lastError = "JSON error";
    setBootStatus(lastError, C_RED);
    if (previousHadWait) clearStaleWaitSnapshot();
    Serial.printf("[task-monitor] fetch failed: json=%s\n", err.c_str());
    return false;
  }

  totalCount = doc["count"] | 0;
  activeCount = doc["active"] | 0;
  attentionCount = doc["attention"] | 0;
  taskCount = 0;
  hiddenCount = 0;
  waitCount = 0;
  runCount = 0;

  JsonArray arr = doc["tasks"].as<JsonArray>();
  for (JsonObject o : arr) {
    String status = o["st"].as<String>();
    uint32_t ageSec = o["u"] | 0;
    if (!shouldShowOnStick(status, ageSec)) {
      hiddenCount++;
      continue;
    }
    if (taskCount >= MAX_TASKS) break;
    AiTask& t = tasks[taskCount++];
    t.id = o["id"].as<String>();
    t.source = o["s"].as<String>();
    t.title = o["t"].as<String>();
    t.status = status;
    t.attention = (int)(o["a"] | 0) != 0;
    t.ageSec = ageSec;
    t.subtitle = o["sub"].as<String>();
    t.usage = o["us"].as<String>();
    t.device = o["d"].as<String>();
    if (t.status == "wait") waitCount++;
    if (t.status == "run") runCount++;
  }
  Serial.printf("[task-monitor] fetch ok tasks=%d hidden=%d total=%d active=%d attention=%d wait=%d wifi=%s ip=%s\n",
                taskCount, hiddenCount, totalCount, activeCount, attentionCount, waitCount,
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  setBootStatus("ready", C_GREEN);
  if (taskCount == 0) {
    selected = 0;
  } else if (lastManualSelectAt != 0 && millis() - lastManualSelectAt < MANUAL_SELECTION_HOLD_MS) {
    int prev = findTaskById(previousSelectedId);
    if (prev >= 0) selected = prev;
    else if (selected >= taskCount) selected = taskCount - 1;
  } else {
    int priority = firstPriorityTask();
    selected = priority >= 0 ? priority : 0;
  }
  return true;
}

static bool openSelectedTask() {
  if (taskCount == 0 || selected >= taskCount) return false;
  if (!ensureWifi()) {
    lastError = "Wi-Fi failed";
    return false;
  }
  AiTask& t = tasks[selected];

  discoverHub(false);

  HTTPClient http;
  String url = apiBase() + "/tasks/" + urlEncode(t.id) + "/open";
  http.begin(url);
  bool requestOpen = true;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.POST("");
  if (code != 200) {
    http.end();
    requestOpen = false;
    if (discoverHub(true)) {
      url = apiBase() + "/tasks/" + urlEncode(t.id) + "/open";
      http.begin(url);
      requestOpen = true;
      http.setTimeout(HTTP_TIMEOUT_MS);
      http.addHeader("X-Device-Token", DEVICE_TOKEN);
      code = http.POST("");
    }
  }
  if (requestOpen) http.end();

  if (code == 200) {
    lastError = "";
    Serial.printf("[task-monitor] open ok id=%s title=%s\n", t.id.c_str(), t.title.c_str());
    return true;
  }
  lastError = String("open HTTP ") + String(code);
  Serial.printf("[task-monitor] open failed id=%s http=%d\n", t.id.c_str(), code);
  return false;
}

static void enterDeepSleep() {
#if ENABLE_DEEP_SLEEP
  uint32_t wakeSeconds = nextWakeSeconds();
  M5.Display.fillScreen(C_BG);
  centerText(String("sleep ") + String(wakeSeconds / 60) + "m", M5.Display.height() / 2, C_GRAY, &fonts::Font0);
  delay(120);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  M5.Display.setBrightness(0);
  M5.Display.sleep();

  const uint64_t mask = (1ULL << PIN_BTN_A) | (1ULL << PIN_BTN_B);
  esp_sleep_enable_ext1_wakeup_io(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  rtc_gpio_pullup_en(PIN_BTN_A);
  rtc_gpio_pulldown_dis(PIN_BTN_A);
  rtc_gpio_pullup_en(PIN_BTN_B);
  rtc_gpio_pulldown_dis(PIN_BTN_B);
  esp_sleep_enable_timer_wakeup((uint64_t)wakeSeconds * 1000000ULL);
  esp_deep_sleep_start();
#else
  lastInputAt = millis();
  activeTimeoutMs = UINT32_MAX;
  drawList();
#endif
}

static void refreshNow() {
  drawMessage("刷新中", apiBase(), C_AMBER);
  bool ok = fetchTasks();
  updateBattery();
  updateAlerts();
  if (hasWaitingTasks()) {
    M5.Display.wakeup();
    applyDisplayBrightness();
    activeTimeoutMs = UINT32_MAX;
#if ENABLE_DEEP_SLEEP
  } else if (activeTimeoutMs == UINT32_MAX) {
    activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
    lastInputAt = millis();
#endif
  }
  lastRefreshAt = millis();
  drawList();
  lastInputAt = millis();
  (void)ok;
}

static void handleButtons() {
  if (M5.BtnA.wasClicked()) {
    lastInputAt = millis();
    drawMessage("打开任务", taskCount ? tasks[selected].source : "no task", C_BLUE);
    bool ok = openSelectedTask();
    drawMessage(ok ? "已发送打开请求" : "打开失败", ok ? "Mac 会切到对应 App" : lastError, ok ? C_GREEN : C_RED);
    delay(900);
    drawList();
  }

  bool bHold = btnBHoldEvent;
  bool bClick = btnBClickEvent;
  btnBHoldEvent = false;
  btnBClickEvent = false;

  if (bHold) {
    refreshNow();
  } else if (bClick) {
    lastInputAt = millis();
    if (taskCount > 0) {
      lastManualSelectAt = millis();
      selected = (selected + 1) % taskCount;
      drawList();
    } else {
      refreshNow();
    }
  }
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  pinMode((int)PIN_BTN_B, INPUT_PULLUP);
  M5.BtnA.setHoldThresh(350);
  M5.BtnB.setHoldThresh(600);
  Serial.begin(115200);
  updateBattery();
  applyPowerProfile();
  hubHost = String(TASK_HUB_HOST);
  hubPort = TASK_HUB_PORT;

  esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  wokeByTimer = wakeCause == ESP_SLEEP_WAKEUP_TIMER;
  wokeFromSleep = wakeCause == ESP_SLEEP_WAKEUP_TIMER || wakeCause == ESP_SLEEP_WAKEUP_EXT1;
  if (wokeFromSleep) {
    drawWakeSyncScreen("wifi...");
  } else {
    drawBootScreen("boot...");
  }

  bool ok = fetchTasks();
  updateBattery();
  updateAlerts();
#if ENABLE_DEEP_SLEEP
  activeTimeoutMs = hasWaitingTasks() ? UINT32_MAX : ((wokeByTimer && attentionCount == 0 && ok) ? QUIET_TIMER_TIMEOUT_MS : INTERACTIVE_TIMEOUT_MS);
#else
  activeTimeoutMs = UINT32_MAX;
#endif
  lastInputAt = millis();
  lastRefreshAt = millis();
  bootScreenActive = false;
  drawList();

  Serial.printf("[task-monitor] up ok=%d tasks=%d active=%d attention=%d wait=%d batt=%d deepSleep=%d wake=%lus bright=%u cpu=%d charge=%d\n",
                (int)ok, taskCount, activeCount, attentionCount, waitCount, battPct, (int)ENABLE_DEEP_SLEEP,
                (unsigned long)nextWakeSeconds(), displayBrightness(), POWER_SAVE_CPU_MHZ, CHARGE_CURRENT_MA);
}

void loop() {
  M5.update();
  updateBtnBEdge();
  handleButtons();

  static uint32_t lastBattAt = 0;
  if (millis() - lastBattAt > 2000) {
    lastBattAt = millis();
    int old = battPct;
    bool oldWifi = wifiOk;
    updateBattery();
    wifiOk = WiFi.status() == WL_CONNECTED;
    if (old != battPct || oldWifi != wifiOk) drawList();
  }

  if ((!ENABLE_DEEP_SLEEP || hasWaitingTasks()) &&
      millis() - lastRefreshAt > awakeRefreshMs() &&
      millis() - lastInputAt > AUTO_REFRESH_INPUT_GUARD_MS) {
    bool ok = fetchTasks();
    updateBattery();
    updateAlerts();
    if (hasWaitingTasks()) {
      M5.Display.wakeup();
      applyDisplayBrightness();
      activeTimeoutMs = UINT32_MAX;
#if ENABLE_DEEP_SLEEP
    } else if (activeTimeoutMs == UINT32_MAX) {
      activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
      lastInputAt = millis();
#endif
    }
    lastRefreshAt = millis();
    drawList();
    (void)ok;
  }

  if (millis() - lastInputAt > activeTimeoutMs) {
    enterDeepSleep();
  }
  delay(40);
}
