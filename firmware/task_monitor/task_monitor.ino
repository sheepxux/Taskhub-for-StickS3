/*
 * StickS3 AI task monitor.
 *
 * Battery-first workflow:
 *   wake -> show cached task preview -> Wi-Fi -> GET /tasks?format=stick
 *   -> show fresh compact list -> sleep.
 *
 * BtnB: open the selected task on the Mac through Task Hub.
 * BtnA: next task.
 * BtnA hold: refresh now.
 *
 * The StickS3 does not scrape AI apps. The Mac-side Task Hub owns collection
 * and open actions. This keeps the device small, wireless, and low power.
 */

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WebServer.h>          // first-run Wi-Fi captive portal
#include <DNSServer.h>
#include <esp_sleep.h>
#include <esp_wifi.h>           // for esp_wifi_set_ps (deeper modem sleep than WiFi.setSleep default)
#include <esp32-hal-cpu.h>
#include <driver/rtc_io.h>
#include <string.h>             // memcpy for BSSID cache

#if !defined(TASKHUB_PUBLIC_BUILD) && __has_include("secrets.h")
#include "secrets.h"
#else
#warning "Using runtime provisioning / placeholder Wi-Fi config. Configure over USB before normal use."
#define WIFI_SSID       "your-wifi-ssid"
#define WIFI_PASSWORD   "your-wifi-password"
#define TASK_HUB_HOST   "192.168.1.100"
#define TASK_HUB_PORT   5577
#define DEVICE_ID       "sticks3-task-01"
#define DEVICE_TOKEN    ""
#define TASKHUB_LANG    "en"
#define VOICE_AUTO_SEND 1
#define AUTO_WAKE_SECONDS 600
#endif

#if !defined(TASK_HUB_HOST) && defined(SERVER_HOST)
#define TASK_HUB_HOST SERVER_HOST
#endif

#if !defined(TASK_HUB_PORT) && defined(SERVER_PORT)
#define TASK_HUB_PORT SERVER_PORT
#endif

#if !defined(TASKHUB_LANG)
#define TASKHUB_LANG "en"
#endif

#if !defined(VOICE_AUTO_SEND)
#define VOICE_AUTO_SEND 1
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

// Reported to the Host during pairing and used to name the M5Burner artifact.
#define TASKHUB_FW_VERSION "2.3.0"

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
static constexpr int C_WORKBUDDY = 0x2EB7;
static constexpr int C_LOVABLE_RED = 0xFA20;
static constexpr int C_LOVABLE_ORANGE = 0xFD20;
static constexpr int C_LOVABLE_SHADOW = 0x5BFF;
static constexpr int C_BLUE = 0x5BDF;
static constexpr int C_CARD = 0x1082;
static constexpr int C_PANEL = 0x18C3;
static constexpr int C_LINE = 0x2945;
static constexpr int C_DARK = 0x0841;

static constexpr int MAX_TASKS = 10;
// Tightened: with a cached BSSID hint, a healthy join lands in <1s and the hub
// usually answers in <500ms on LAN once its cache is warm. A cold Host pass can
// take several seconds while desktop adapters scan local stores, so HTTP gets a
// larger window to avoid a false "Cannot read tasks" on boot.
static constexpr uint32_t WIFI_TIMEOUT_MS = 5000;
static constexpr uint32_t HTTP_TIMEOUT_MS = 8000;
static constexpr uint32_t DISCOVERY_TIMEOUT_MS = 900;
static constexpr uint32_t DISCOVERY_REFRESH_MS = 300000;
static constexpr uint32_t BOOT_FETCH_RETRY_MS = 45000;
static constexpr uint32_t WAKE_FETCH_RETRY_MS = 12000;
static constexpr uint32_t FETCH_RETRY_DELAY_MS = 1200;
static constexpr uint32_t STALE_SYNC_FOLLOWUP_MS = 4500;
static constexpr int RTC_SNAPSHOT_TASKS = 5;
static constexpr size_t RTC_ID_LEN = 40;
static constexpr size_t RTC_SOURCE_LEN = 16;
static constexpr size_t RTC_TITLE_LEN = 72;
static constexpr size_t RTC_STATUS_LEN = 8;
static constexpr size_t RTC_SUBTITLE_LEN = 48;
static constexpr size_t RTC_USAGE_LEN = 32;
static constexpr size_t RTC_DEVICE_LEN = 12;

#if !defined(ECO_MODE)
#define ECO_MODE 1
#endif

#if !defined(INTERACTIVE_TIMEOUT_MS)
#if ECO_MODE
#define INTERACTIVE_TIMEOUT_MS 6000
#else
#define INTERACTIVE_TIMEOUT_MS 10000
#endif
#endif

#if !defined(QUIET_TIMER_TIMEOUT_MS)
#if ECO_MODE
#define QUIET_TIMER_TIMEOUT_MS 1500
#else
#define QUIET_TIMER_TIMEOUT_MS 3000
#endif
#endif

#if !defined(ACTIVE_WAKE_SECONDS)
// A WAIT almost always appears while a task is already running, so the device
// is most likely deep-sleeping with active/attention tasks when one shows up.
// 120s caps the worst-case "turned to WAIT" latency to ~2 min while cutting
// radio wakeups in half versus the old 60s default.
#if ECO_MODE
#define ACTIVE_WAKE_SECONDS 120
#else
#define ACTIVE_WAKE_SECONDS 60
#endif
#endif

#if !defined(LOW_BATTERY_WAKE_SECONDS)
#if ECO_MODE
#define LOW_BATTERY_WAKE_SECONDS 1200
#else
#define LOW_BATTERY_WAKE_SECONDS 900
#endif
#endif

#if !defined(LOW_BATTERY_THRESHOLD_PCT)
#define LOW_BATTERY_THRESHOLD_PCT 30
#endif

#if !defined(DISPLAY_BRIGHTNESS)
#if ECO_MODE
#define DISPLAY_BRIGHTNESS 22
#else
#define DISPLAY_BRIGHTNESS 32
#endif
#endif

#if !defined(LOW_BATTERY_BRIGHTNESS)
#if ECO_MODE
#define LOW_BATTERY_BRIGHTNESS 8
#else
#define LOW_BATTERY_BRIGHTNESS 16
#endif
#endif

#if !defined(WAIT_ATTENTION_TIMEOUT_MS)
#if ECO_MODE
#define WAIT_ATTENTION_TIMEOUT_MS 180000
#else
#define WAIT_ATTENTION_TIMEOUT_MS 0
#endif
#endif

#if !defined(LOW_BATTERY_WAIT_ATTENTION_TIMEOUT_MS)
#if ECO_MODE
#define LOW_BATTERY_WAIT_ATTENTION_TIMEOUT_MS 45000
#else
#define LOW_BATTERY_WAIT_ATTENTION_TIMEOUT_MS 0
#endif
#endif

#if !defined(POWER_SAVE_CPU_MHZ)
#define POWER_SAVE_CPU_MHZ 80
#endif

#if !defined(CHARGE_CURRENT_MA)
#define CHARGE_CURRENT_MA 200
#endif

#if !defined(AWAKE_REFRESH_IDLE_MS)
#if ECO_MODE
#define AWAKE_REFRESH_IDLE_MS 60000
#else
#define AWAKE_REFRESH_IDLE_MS 30000
#endif
#endif

#if !defined(AWAKE_REFRESH_ACTIVE_MS)
#if ECO_MODE
#define AWAKE_REFRESH_ACTIVE_MS 15000
#else
#define AWAKE_REFRESH_ACTIVE_MS 5000
#endif
#endif

#if !defined(AWAKE_REFRESH_WAIT_MS)
#if ECO_MODE
#define AWAKE_REFRESH_WAIT_MS 15000
#else
#define AWAKE_REFRESH_WAIT_MS 5000
#endif
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
//   - FAIL: a task errored out (falling two-note buzz).
// Edges are detected per task (by id) against the status seen at the previous
// refresh, so a second session entering WAIT while another is already waiting
// still rings. At most one alert plays per refresh: WAIT > FAIL > DONE.
// Beeps use the StickS3 speaker (M5.Speaker). Vibration is left as a future
// hook: the pinned M5Unified does NOT drive a motor on board_M5StickS3
// (setVibration is a no-op there), so it stays off by default.
#if !defined(ALERT_ON_WAIT)
#define ALERT_ON_WAIT 1
#endif
#if !defined(ALERT_ON_DONE)
#define ALERT_ON_DONE 1
#endif
#if !defined(ALERT_ON_FAIL)
#define ALERT_ON_FAIL 1
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
#if !defined(ALERT_FAIL_HZ)
#define ALERT_FAIL_HZ 1800
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

// Auto-rotate the display to match how the StickS3 is held, using the IMU's
// gravity vector. Only meaningful while awake (deep sleep powers the screen
// down). ROT_* map the dominant in-plane gravity axis to a display rotation;
// if an orientation comes out upside-down or 90° off on the real device, swap
// the matching ROT_* value. Set ROTATE_DEBUG 1 to show live ax/ay + rotation.
#if !defined(ENABLE_AUTO_ROTATE)
#define ENABLE_AUTO_ROTATE 1
#endif
#if !defined(ROTATE_POLL_MS)
#define ROTATE_POLL_MS 150
#endif
#if !defined(ROTATE_STABLE_MS)
#define ROTATE_STABLE_MS 350
#endif
#if !defined(ROTATE_DEADZONE_G)
#define ROTATE_DEADZONE_G 0.40f
#endif
// On this StickS3 the X axis dominates in PORTRAIT and Y in LANDSCAPE, so the
// X-dominant case maps to portrait (0/2) and Y-dominant to landscape (1/3).
// If an orientation is upside-down, swap the POS/NEG value within its pair.
#if !defined(ROT_X_POS)
#define ROT_X_POS 2   // gravity toward +X -> portrait
#endif
#if !defined(ROT_X_NEG)
#define ROT_X_NEG 0   // gravity toward -X -> portrait flipped
#endif
#if !defined(ROT_Y_POS)
#define ROT_Y_POS 1   // gravity toward +Y -> landscape
#endif
#if !defined(ROT_Y_NEG)
#define ROT_Y_NEG 3   // gravity toward -Y -> landscape flipped
#endif
#if !defined(ROTATE_DEBUG)
#define ROTATE_DEBUG 0
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

// Voice mode: hold BtnB to record (M5.Mic -> PSRAM), release to POST the clip
// to the Host's /voice endpoint, which transcribes it with whisper and pastes
// the text into the selected task's app. Recording uses the speaker's I2S, so
// we stop M5.Speaker while the mic runs and restore it afterwards.
#if !defined(ENABLE_VOICE)
#define ENABLE_VOICE 1
#endif
#if !defined(VOICE_SAMPLE_RATE)
#define VOICE_SAMPLE_RATE 16000
#endif
#if !defined(VOICE_MAX_SECONDS)
#define VOICE_MAX_SECONDS 20
#endif
#if !defined(VOICE_HTTP_TIMEOUT_MS)
#define VOICE_HTTP_TIMEOUT_MS 20000
#endif
#define VOICE_MAX_SAMPLES ((uint32_t)VOICE_SAMPLE_RATE * VOICE_MAX_SECONDS)
#define VOICE_WAV_HEADER 44
#define VOICE_MIC_CHUNK 1600   // ~100ms at 16 kHz

static constexpr gpio_num_t PIN_BTN_A = GPIO_NUM_11;
static constexpr gpio_num_t PIN_BTN_B = GPIO_NUM_12;
static constexpr size_t SERIAL_CONFIG_MAX = 768;
static const char* CONFIG_NAMESPACE = "taskhub";
static const char* PLACEHOLDER_WIFI_SSID = "your-wifi-ssid";
static const char* PLACEHOLDER_WIFI_PASSWORD = "your-wifi-password";

// First-run setup (M5Burner firmware ships without Wi-Fi or a token):
//   1. no Wi-Fi  -> open AP "TaskHub-XXXX" + captive portal to pick a network
//   2. no token  -> join Wi-Fi, find the Host over UDP, show a 4-digit code,
//                   poll POST /pair until someone approves the code on the Mac
// USB serial provisioning keeps working in both stages for developers.
static constexpr uint32_t PORTAL_STA_JOIN_TIMEOUT_MS = 20000;   // password check budget
static constexpr uint32_t PORTAL_LINGER_AFTER_JOIN_MS = 180000; // keep the AP so the phone sees the result
static constexpr uint32_t PAIR_POLL_MS = 2500;
static constexpr uint32_t PAIR_STA_RETRY_MS = 15000;
static constexpr uint32_t SETUP_RESET_HOLD_MS = 3000;           // hold BtnA in pairing to redo Wi-Fi

// Persisted across deep sleep in RTC slow memory (~8KB available, free).
// After a successful join we stash the AP's BSSID + channel; on the next wake
// WiFi.begin() can target the radio directly instead of doing a full
// passive scan. Drops connect time from ~1.5-3s to ~0.5s, which is the
// single biggest awake-time win on a battery-bound device.
RTC_DATA_ATTR static uint8_t  rtcCachedBssid[6] = {0};
RTC_DATA_ATTR static int32_t  rtcCachedChannel = 0;
RTC_DATA_ATTR static bool     rtcHasCachedBssid = false;

// Per-task status as of the last successful refresh, keyed by task id and
// persisted across deep sleep. Alerts are edge-triggered against this table so
// each WAIT/FAIL/DONE transition rings exactly once, even when it is first
// observed on a timer wake, and even when another task is already waiting.
// Status is stored as a single char (see statusCode()); 0 = never seen.
struct RtcTaskAlertState {
  char id[RTC_ID_LEN];
  char status;
};
RTC_DATA_ATTR static RtcTaskAlertState rtcPrevTasks[MAX_TASKS] = {};
RTC_DATA_ATTR static int      rtcPrevTaskCount = 0;
RTC_DATA_ATTR static bool     rtcPrevTasksValid = false;

// Alert decided by the latest fetchTasks(), consumed by updateAlerts().
enum AlertKind : uint8_t { ALERT_NONE = 0, ALERT_DONE = 1, ALERT_FAIL = 2, ALERT_WAIT = 3 };
static AlertKind pendingAlert = ALERT_NONE;
static int pendingAlertTask = -1;

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

struct RtcTaskSnapshotItem {
  char id[RTC_ID_LEN];
  char source[RTC_SOURCE_LEN];
  char title[RTC_TITLE_LEN];
  char status[RTC_STATUS_LEN];
  char subtitle[RTC_SUBTITLE_LEN];
  char usage[RTC_USAGE_LEN];
  char device[RTC_DEVICE_LEN];
  uint32_t ageSec;
  bool attention;
};

struct RtcTaskSnapshot {
  uint32_t magic;
  int count;
  int selected;
  int total;
  int active;
  int attention;
  int wait;
  int run;
  int hidden;
  RtcTaskSnapshotItem items[RTC_SNAPSHOT_TASKS];
};

static constexpr uint32_t RTC_TASK_SNAPSHOT_MAGIC = 0x54485542;  // THUB
RTC_DATA_ATTR static RtcTaskSnapshot rtcTaskSnapshot = {};

static AiTask tasks[MAX_TASKS];
static int taskCount = 0;
static int selected = 0;
static int activeCount = 0;
static int attentionCount = 0;
static int waitCount = 0;
static int runCount = 0;   // tasks with status "run" (top-bar metric dot)
static int displayRotation = 1;       // currently applied M5.Display rotation
static int pendingRotation = 1;       // candidate rotation awaiting the stability window
static uint32_t pendingRotationSince = 0;
static uint32_t lastRotatePollAt = 0;
static int totalCount = 0;
static int hiddenCount = 0;
static int battPct = 100;
static bool battCharging = false;
static bool wifiOk = false;
static bool wokeByTimer = false;
static bool wokeFromSleep = false;
static bool hostSyncing = false;
static uint32_t lastInputAt = 0;
static uint32_t lastManualSelectAt = 0;
static uint32_t lastRefreshAt = 0;
static uint32_t lastDiscoveryAt = 0;
static uint32_t nextSyncFollowupAt = 0;
static uint32_t activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
static String hubHost;
static int hubPort = TASK_HUB_PORT;
static bool hubDiscovered = false;
static String cfgWifiSsid;
static String cfgWifiPassword;
static String cfgHubHost;
static int cfgHubPort = TASK_HUB_PORT;
static String cfgDeviceId;
static String cfgDeviceToken;
static String cfgLang;
static bool cfgVoiceAutoSend = VOICE_AUTO_SEND != 0;
static bool cfgReady = false;
static bool cfgWifiReady = false;
static bool setupMode = false;
static String serialConfigLine;
static uint32_t lastSetupStatusAt = 0;

enum SetupStage { SETUP_NONE, SETUP_WIFI_PORTAL, SETUP_PAIRING };
static SetupStage setupStage = SETUP_NONE;
static WebServer* portalServer = nullptr;
static DNSServer* portalDns = nullptr;
static bool portalActive = false;
static String apSsid;
static String portalScanOptions;          // <option> list from the last scan
static String portalPendingSsid;
static String portalPendingPassword;
static String portalPendingLang;
static bool portalStaConnecting = false;
static bool portalStaFailed = false;
static uint32_t portalStaStartedAt = 0;
static uint32_t portalJoinedAt = 0;
static String pairCode;
static String pairName;
static String pairDeviceId;
static String pairHostName;
static String pairStatus;
static uint32_t lastPairPollAt = 0;
static uint32_t lastPairStaAttemptAt = 0;
static bool pairDone = false;
static String lastError;
static bool btnBReadingPressed = false;
static bool btnBStablePressed = false;
static bool btnBHoldFired = false;
static bool btnBClickEvent = false;
static bool btnBHoldEvent = false;
static bool snapshotPreviewActive = false;

// Voice mode state. voiceBuf is one PSRAM block: [44-byte WAV header][PCM16].
static uint8_t* voiceBuf = nullptr;
static int16_t* voicePcm = nullptr;       // = voiceBuf + 44, where samples land
static uint32_t voiceSamples = 0;         // samples queued/recorded so far
static bool voiceRecording = false;
static uint32_t voiceStartMs = 0;
static uint32_t voiceLastUiSec = 0;
static uint32_t btnBLastChangeAt = 0;
static uint32_t btnBPressedAt = 0;
static bool bootScreenActive = false;
static String bootStatusText;

static void setBootStatus(const String& text, int color);
static void topBar();
static void centerText(const String& text, int y, int color, const lgfx::IFont* font);
static void handleSerialConfig();
static bool uiZh();
static void sendSerialConfigStatus(const char* type, bool ok, const char* message);
static void clearRtcTaskSnapshot();
static void saveRtcTaskSnapshot();
static bool restoreRtcTaskSnapshot();
static void copyRtcField(char* dst, size_t len, const String& value);

static bool isPlaceholder(const String& value, const char* placeholder) {
  return !value.length() || value == placeholder;
}

static String normalizeLang(String lang) {
  lang.trim();
  lang.toLowerCase();
  if (lang.startsWith("zh")) return "zh";
  return "en";
}

static bool jsonBoolOrDefault(JsonVariantConst value, bool fallback) {
  if (value.isNull()) return fallback;
  if (value.is<bool>()) return value.as<bool>();
  if (value.is<int>()) return value.as<int>() != 0;
  String text = value.as<String>();
  text.trim();
  text.toLowerCase();
  if (text == "0" || text == "false" || text == "no" || text == "off") return false;
  if (text == "1" || text == "true" || text == "yes" || text == "on") return true;
  return fallback;
}

static bool loadRuntimeConfig() {
  // Two NVS layers: "wifi_saved" (captive portal wrote ssid/password/lang)
  // and "configured" (full config incl. token, from pairing or USB). A device
  // can have Wi-Fi but no token yet; that is the pairing stage.
  bool fromNvs = false;
  bool wifiSaved = false;
  Preferences prefs;
  if (prefs.begin(CONFIG_NAMESPACE, true)) {
    fromNvs = prefs.getBool("configured", false);
    wifiSaved = fromNvs || prefs.getBool("wifi_saved", false);
    if (wifiSaved) {
      cfgWifiSsid = prefs.getString("ssid", "");
      cfgWifiPassword = prefs.getString("password", "");
      cfgLang = normalizeLang(prefs.getString("lang", TASKHUB_LANG));
    }
    if (fromNvs) {
      cfgHubHost = prefs.getString("host", "");
      cfgHubPort = prefs.getInt("port", TASK_HUB_PORT);
      cfgDeviceId = prefs.getString("device_id", DEVICE_ID);
      cfgDeviceToken = prefs.getString("token", "");
      cfgVoiceAutoSend = prefs.getBool("voice_send", VOICE_AUTO_SEND != 0);
    }
    prefs.end();
  }

  if (!wifiSaved) {
    cfgWifiSsid = String(WIFI_SSID);
    cfgWifiPassword = String(WIFI_PASSWORD);
    cfgLang = normalizeLang(String(TASKHUB_LANG));
  }
  if (!fromNvs) {
    cfgHubHost = String(TASK_HUB_HOST);
    cfgHubPort = TASK_HUB_PORT;
    cfgDeviceId = String(DEVICE_ID);
    cfgDeviceToken = String(DEVICE_TOKEN);
    cfgVoiceAutoSend = VOICE_AUTO_SEND != 0;
  }

  if (!cfgLang.length()) cfgLang = "en";
  if (!cfgHubHost.length()) cfgHubHost = String(TASK_HUB_HOST);
  if (cfgHubPort <= 0) cfgHubPort = TASK_HUB_PORT;
  if (!cfgDeviceId.length()) cfgDeviceId = String(DEVICE_ID);

  bool ssidOk = !isPlaceholder(cfgWifiSsid, PLACEHOLDER_WIFI_SSID);
  bool passwordOk = wifiSaved || cfgWifiPassword != PLACEHOLDER_WIFI_PASSWORD;
  bool tokenOk = cfgDeviceToken.length() > 0;
  cfgWifiReady = ssidOk && passwordOk;
  cfgReady = cfgWifiReady && tokenOk && cfgHubPort > 0;
  return cfgReady;
}

static bool saveWifiConfig(const String& ssid, const String& password, const String& lang) {
  if (isPlaceholder(ssid, PLACEHOLDER_WIFI_SSID)) return false;
  Preferences prefs;
  if (!prefs.begin(CONFIG_NAMESPACE, false)) return false;
  prefs.putString("ssid", ssid);
  prefs.putString("password", password);
  prefs.putString("lang", normalizeLang(lang.length() ? lang : cfgLang));
  prefs.putBool("wifi_saved", true);
  prefs.end();
  loadRuntimeConfig();
  return cfgWifiReady;
}

static bool saveRuntimeConfig(const String& ssid, const String& password, const String& host,
                              int port, const String& deviceId, const String& token,
                              const String& lang, bool voiceAutoSend) {
  if (isPlaceholder(ssid, PLACEHOLDER_WIFI_SSID)) return false;
  if (!token.length()) return false;

  Preferences prefs;
  if (!prefs.begin(CONFIG_NAMESPACE, false)) return false;
  prefs.putString("ssid", ssid);
  prefs.putString("password", password);
  prefs.putString("host", host.length() ? host : String(TASK_HUB_HOST));
  prefs.putInt("port", port > 0 ? port : TASK_HUB_PORT);
  prefs.putString("device_id", deviceId.length() ? deviceId : String(DEVICE_ID));
  prefs.putString("token", token);
  prefs.putString("lang", normalizeLang(lang));
  prefs.putBool("voice_send", voiceAutoSend);
  prefs.putBool("wifi_saved", true);
  prefs.putBool("configured", true);
  prefs.end();

  clearRtcTaskSnapshot();
  loadRuntimeConfig();
  return cfgReady;
}

static void clearRuntimeConfig() {
  Preferences prefs;
  if (prefs.begin(CONFIG_NAMESPACE, false)) {
    prefs.clear();
    prefs.end();
  }
  clearRtcTaskSnapshot();
  loadRuntimeConfig();
}

static void sendSerialConfigStatus(const char* type, bool ok, const char* message) {
  JsonDocument doc;
  doc["type"] = type;
  doc["ok"] = ok;
  doc["configured"] = cfgReady;
  doc["wifi_configured"] = cfgWifiReady;
  doc["stage"] = setupStage == SETUP_WIFI_PORTAL ? "wifi" : (setupStage == SETUP_PAIRING ? "pair" : "ready");
  if (setupStage == SETUP_WIFI_PORTAL) doc["ap_ssid"] = apSsid;
  if (setupStage == SETUP_PAIRING) doc["pair_code"] = pairCode;
  doc["message"] = message;
  doc["ssid"] = cfgReady ? cfgWifiSsid : "";
  doc["host"] = cfgHubHost;
  doc["port"] = cfgHubPort;
  doc["device_id"] = cfgDeviceId;
  doc["lang"] = cfgLang;
  doc["voice_send"] = cfgVoiceAutoSend;
  serializeJson(doc, Serial);
  Serial.println();
}

static void handleSerialConfigLine(String line) {
  line.trim();
  if (!line.length() || line[0] != '{') return;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    sendSerialConfigStatus("taskhub.error", false, err.c_str());
    return;
  }

  String cmd = doc["cmd"].as<String>();
  if (cmd == "taskhub.status" || cmd == "status") {
    sendSerialConfigStatus("taskhub.status", true, cfgReady ? "configured" : "setup required");
    return;
  }

  if (cmd == "taskhub.reset" || cmd == "reset") {
    clearRuntimeConfig();
    sendSerialConfigStatus("taskhub.reset", true, "configuration cleared");
    delay(250);
    ESP.restart();
    return;
  }

  if (cmd != "taskhub.configure" && cmd != "configure") {
    sendSerialConfigStatus("taskhub.error", false, "unknown command");
    return;
  }

  String ssid = doc["ssid"].as<String>();
  String password = doc["password"].as<String>();
  String host = doc["host"].as<String>();
  int port = doc["port"] | TASK_HUB_PORT;
  String deviceId = doc["device_id"].as<String>();
  if (!deviceId.length()) deviceId = doc["device"].as<String>();
  String token = doc["token"].as<String>();
  String lang = doc["lang"].as<String>();
  if (!lang.length()) lang = doc["language"].as<String>();
  if (!lang.length()) lang = cfgLang.length() ? cfgLang : String(TASKHUB_LANG);
  bool voiceAutoSend = cfgVoiceAutoSend;
  if (!doc["voice_send"].isNull()) {
    voiceAutoSend = jsonBoolOrDefault(doc["voice_send"], cfgVoiceAutoSend);
  } else if (!doc["voice_auto_send"].isNull()) {
    voiceAutoSend = jsonBoolOrDefault(doc["voice_auto_send"], cfgVoiceAutoSend);
  }

  if (isPlaceholder(ssid, PLACEHOLDER_WIFI_SSID)) {
    sendSerialConfigStatus("taskhub.error", false, "ssid required");
    return;
  }
  if (!token.length()) {
    sendSerialConfigStatus("taskhub.error", false, "token required");
    return;
  }

  bool saved = saveRuntimeConfig(ssid, password, host, port, deviceId, token, lang, voiceAutoSend);
  sendSerialConfigStatus(saved ? "taskhub.configured" : "taskhub.error", saved,
                         saved ? "saved; restarting" : "save failed");
  if (saved) {
    delay(400);
    ESP.restart();
  }
}

static void handleSerialConfig() {
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      handleSerialConfigLine(serialConfigLine);
      serialConfigLine = "";
      continue;
    }
    if (serialConfigLine.length() < SERIAL_CONFIG_MAX) {
      serialConfigLine += ch;
    } else {
      serialConfigLine = "";
      sendSerialConfigStatus("taskhub.error", false, "line too long");
    }
  }
}

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

static uint32_t waitAttentionTimeoutMs() {
  uint32_t configured = lowBatteryMode() ? LOW_BATTERY_WAIT_ATTENTION_TIMEOUT_MS : WAIT_ATTENTION_TIMEOUT_MS;
  return configured == 0 ? UINT32_MAX : configured;
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
  String host = hubHost.length() ? hubHost : cfgHubHost;
  if (!host.length()) host = String(TASK_HUB_HOST);
  int port = hubPort > 0 ? hubPort : cfgHubPort;
  if (port <= 0) port = TASK_HUB_PORT;
  return String("http://") + host + ":" + String(port);
}

static uint32_t awakeRefreshMs() {
  if (waitCount > 0) return AWAKE_REFRESH_WAIT_MS;
  return (activeCount > 0 || attentionCount > 0) ? AWAKE_REFRESH_ACTIVE_MS : AWAKE_REFRESH_IDLE_MS;
}

static void keepAwakeForHostSyncing() {
  if (!hostSyncing) return;
  uint32_t minTimeout = STALE_SYNC_FOLLOWUP_MS + 2500;
  if (activeTimeoutMs != UINT32_MAX && activeTimeoutMs < minTimeout) activeTimeoutMs = minTimeout;
  lastInputAt = millis();
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

static bool uiZh() {
  return cfgLang == "zh";
}

static const char* uiText(const char* en, const char* zh) {
  return uiZh() ? zh : en;
}

static String uiText(const String& en, const String& zh) {
  return uiZh() ? zh : en;
}

// Connect with the cached BSSID/channel hint if we have one; on failure fall
// back to a full scan. Picks the deepest modem-sleep level once associated so
// the brief idle awake window also draws less current.
static bool ensureWifi() {
  if (!cfgReady) {
    wifiOk = false;
    lastError = uiText("Setup required", "需要配置");
    setBootStatus(uiText("setup required", "需要配置"), C_AMBER);
    return false;
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifiOk = true;
    setBootStatus(uiText("wifi ok", "Wi-Fi 已连接"), C_GREEN);
    return true;
  }

  setBootStatus(uiText("wifi...", "Wi-Fi..."), C_BLUE);
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(true);

  auto waitForJoin = [](uint32_t budgetMs) {
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < budgetMs) {
      M5.update();
      handleSerialConfig();
      delay(50);
    }
  };

  if (rtcHasCachedBssid && rtcCachedChannel > 0) {
    // Fast path: aim at the known AP. Most wakes land here, in well under 1s.
    WiFi.begin(cfgWifiSsid.c_str(), cfgWifiPassword.c_str(), rtcCachedChannel, rtcCachedBssid);
    waitForJoin(2500);                      // generous enough for a slow router
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.disconnect(false, false);        // hint stale — fall through to scan
      rtcHasCachedBssid = false;
    }
  }
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.begin(cfgWifiSsid.c_str(), cfgWifiPassword.c_str());
    waitForJoin(WIFI_TIMEOUT_MS);
  }

  wifiOk = WiFi.status() == WL_CONNECTED;
  if (wifiOk) {
    setBootStatus(uiText("wifi ok", "Wi-Fi 已连接"), C_GREEN);
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
    setBootStatus(uiText("wifi failed", "Wi-Fi 失败"), C_RED);
  }
  return wifiOk;
}

static bool discoverHub(bool force) {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!force && hubDiscovered && millis() - lastDiscoveryAt < DISCOVERY_REFRESH_MS) {
    return true;
  }

  setBootStatus(uiText("hub...", "Host..."), C_BLUE);
  lastDiscoveryAt = millis();
  WiFiUDP udp;
  if (!udp.begin(0)) {
    Serial.println("[task-monitor] discovery failed: udp");
    return false;
  }

  JsonDocument req;
  req["type"] = "sticks3.discover";
  req["device"] = cfgDeviceId;
  if (cfgDeviceToken.length()) {
    req["token"] = cfgDeviceToken;
  } else {
    req["pair"] = true;   // unpaired: the Host answers with its address only
  }
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
    int port = resp["port"] | cfgHubPort;
    if (!host.length()) host = udp.remoteIP().toString();
    if (!host.length() || port <= 0) continue;

    hubHost = host;
    hubPort = port;
    hubDiscovered = true;
    Serial.printf("[task-monitor] discovery ok host=%s port=%d\n", hubHost.c_str(), hubPort);
    setBootStatus(uiText("hub ok", "Host 已连接"), C_GREEN);
    udp.stop();
    return true;
  }

  udp.stop();
  Serial.printf("[task-monitor] discovery fallback host=%s port=%d\n", hubHost.c_str(), hubPort);
  setBootStatus(uiText("hub fallback", "Host fallback"), C_AMBER);
  return false;
}

static void updateBattery() {
  int b = M5.Power.getBatteryLevel();
  if (b >= 0 && b <= 100) battPct = b;
  battCharging = M5.Power.isCharging() == m5::Power_Class::is_charging;
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

static int statusTextColor(const String& status) {
  if (status == "run" || status == "fail" || status == "idle") return C_WHITE;
  return C_BG;
}

static int statusPillWidth(const String& status) {
  const char* label = statusLabel(status);
  M5.Display.setFont(&fonts::Font0);
  int w = M5.Display.textWidth(label) + 12;
  return w < 34 ? 34 : w;
}

static void drawStatusPill(const String& status, int x, int y, int bg, bool compact = false) {
  int col = statusColor(status);
  int w = compact ? 34 : statusPillWidth(status);
  int h = compact ? 12 : 15;
  M5.Display.fillRoundRect(x, y, w, h, 4, col);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setTextColor(statusTextColor(status), col);
  M5.Display.drawString(statusLabel(status), x + w / 2, y + h / 2 + 1);
  M5.Display.setTextDatum(top_left);
  (void)bg;
}

static String ageLabel(uint32_t sec) {
  if (sec < 60) return String(sec) + "s";
  if (sec < 3600) return String(sec / 60) + "m";
  if (sec < 86400) return String(sec / 3600) + "h";
  return String(sec / 86400) + "d";
}

static String displaySourceLabel(const AiTask& t) {
  String label = t.source;
  if (t.device.length()) label += "@" + t.device;
  return label;
}

// 19x9 battery outline with a level bar; while on USB power the bar turns blue
// and a small white bolt is drawn over it so charging is visible at a glance.
static void drawBatteryGlyph(int x, int y, int pct, bool charging = false) {
  int col = pct <= LOW_BATTERY_THRESHOLD_PCT ? C_RED : (pct < 60 ? C_AMBER : C_GREEN);
  if (charging) col = C_BLUE;
  M5.Display.drawRoundRect(x, y, 19, 9, 2, C_LINE);
  M5.Display.fillRect(x + 19, y + 3, 2, 3, C_LINE);
  int fillW = map(pct < 0 ? 0 : (pct > 100 ? 100 : pct), 0, 100, 0, 15);
  if (fillW > 0) M5.Display.fillRect(x + 2, y + 2, fillW, 5, col);
  if (charging) {
    M5.Display.drawLine(x + 10, y + 1, x + 8, y + 4, C_WHITE);
    M5.Display.drawFastHLine(x + 8, y + 4, 4, C_WHITE);
    M5.Display.drawLine(x + 11, y + 4, x + 9, y + 7, C_WHITE);
  }
}

static void drawIndexRail(int x, int y, int w, int h) {
  if (taskCount <= 1) {
    M5.Display.fillRoundRect(x, y, w, h, h / 2, C_LINE);
    return;
  }
  M5.Display.fillRoundRect(x, y, w, h, h / 2, C_LINE);
  int segW = max(3, w / taskCount);
  int pos = map(selected, 0, taskCount - 1, 0, w - segW);
  M5.Display.fillRoundRect(x + pos, y, segW, h, h / 2, C_BLUE);
}

static void drawMetricDots(int x, int y) {
  int r = 2;
  if (runCount > 0) {
    M5.Display.fillCircle(x, y, r, C_BLUE);
    x += 7;
  }
  if (waitCount > 0) {
    M5.Display.fillCircle(x, y, r, C_AMBER);
    x += 7;
  }
  if (attentionCount > 0 && waitCount == 0) {
    M5.Display.fillCircle(x, y, r, C_AMBER);
  }
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

// Task errored: a falling two-note buzz, clearly different from WAIT/DONE.
static void alertFail() {
  M5.Display.wakeup();
  applyDisplayBrightness();
  alertBeep2(ALERT_FAIL_HZ, 120, 60, ALERT_FAIL_HZ - 600, 220);
  alertVibrateHook();
}

// Compact status code for the RTC alert table. 0 is reserved for "never seen".
static char statusCode(const String& status) {
  if (status == "wait") return 'w';
  if (status == "run") return 'r';
  if (status == "fail") return 'f';
  if (status == "done") return 'd';
  if (status == "rec") return 'c';
  if (status == "idle") return 'i';
  return 'u';
}

static char prevStatusFor(const String& id) {
  if (!rtcPrevTasksValid || !id.length()) return 0;
  for (int i = 0; i < rtcPrevTaskCount; i++) {
    if (strncmp(rtcPrevTasks[i].id, id.c_str(), RTC_ID_LEN - 1) == 0) return rtcPrevTasks[i].status;
  }
  return 0;
}

// Diff the freshly fetched task list against the per-task status table from the
// previous refresh and pick the single most important transition to announce:
//   WAIT: task is waiting now and was not waiting before (incl. brand-new tasks)
//   FAIL: task failed now and was not failed before
//   DONE: task was running and is now done/recent (a turn completed)
// Then remember the current statuses for the next diff. Called from
// fetchTasks() on success only, so a failed fetch never masks a transition.
static void detectAlertEdges() {
  pendingAlert = ALERT_NONE;
  pendingAlertTask = -1;
  for (int i = 0; i < taskCount; i++) {
    char now = statusCode(tasks[i].status);
    char prev = prevStatusFor(tasks[i].id);
    AlertKind kind = ALERT_NONE;
    if (now == 'w' && prev != 'w') kind = ALERT_WAIT;
    else if (now == 'f' && prev != 'f') kind = ALERT_FAIL;
    else if ((now == 'd' || now == 'c') && prev == 'r') kind = ALERT_DONE;
    if (kind > pendingAlert) {
      pendingAlert = kind;
      pendingAlertTask = i;
    }
  }

  rtcPrevTaskCount = min(taskCount, MAX_TASKS);
  for (int i = 0; i < rtcPrevTaskCount; i++) {
    copyRtcField(rtcPrevTasks[i].id, sizeof(rtcPrevTasks[i].id), tasks[i].id);
    rtcPrevTasks[i].status = statusCode(tasks[i].status);
  }
  rtcPrevTasksValid = true;
}

// Play the alert decided by the last fetchTasks(), if any (one-shot).
static void updateAlerts() {
  AlertKind kind = pendingAlert;
  pendingAlert = ALERT_NONE;
  pendingAlertTask = -1;
  switch (kind) {
    case ALERT_WAIT:
#if ALERT_ON_WAIT
      alertWait();
#endif
      break;
    case ALERT_FAIL:
#if ALERT_ON_FAIL
      alertFail();
#endif
      break;
    case ALERT_DONE:
#if ALERT_ON_DONE
      alertDone();
#endif
      break;
    default:
      break;
  }
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
  snapshotPreviewActive = false;
}

static void copyRtcField(char* dst, size_t len, const String& value) {
  if (!dst || len == 0) return;
  size_t n = value.length();
  if (n >= len) n = len - 1;
  memcpy(dst, value.c_str(), n);
  dst[n] = '\0';
}

static void clearRtcTaskSnapshot() {
  memset(&rtcTaskSnapshot, 0, sizeof(rtcTaskSnapshot));
  snapshotPreviewActive = false;
}

static void saveRtcTaskSnapshot() {
  rtcTaskSnapshot.magic = RTC_TASK_SNAPSHOT_MAGIC;
  rtcTaskSnapshot.count = min(taskCount, RTC_SNAPSHOT_TASKS);
  rtcTaskSnapshot.selected = selected;
  if (rtcTaskSnapshot.selected >= rtcTaskSnapshot.count) rtcTaskSnapshot.selected = 0;
  rtcTaskSnapshot.total = totalCount;
  rtcTaskSnapshot.active = activeCount;
  rtcTaskSnapshot.attention = attentionCount;
  rtcTaskSnapshot.wait = waitCount;
  rtcTaskSnapshot.run = runCount;
  rtcTaskSnapshot.hidden = hiddenCount;
  for (int i = 0; i < rtcTaskSnapshot.count; i++) {
    RtcTaskSnapshotItem& out = rtcTaskSnapshot.items[i];
    AiTask& in = tasks[i];
    copyRtcField(out.id, sizeof(out.id), in.id);
    copyRtcField(out.source, sizeof(out.source), in.source);
    copyRtcField(out.title, sizeof(out.title), in.title);
    copyRtcField(out.status, sizeof(out.status), in.status);
    copyRtcField(out.subtitle, sizeof(out.subtitle), in.subtitle);
    copyRtcField(out.usage, sizeof(out.usage), in.usage);
    copyRtcField(out.device, sizeof(out.device), in.device);
    out.ageSec = in.ageSec;
    out.attention = in.attention;
  }
  for (int i = rtcTaskSnapshot.count; i < RTC_SNAPSHOT_TASKS; i++) {
    memset(&rtcTaskSnapshot.items[i], 0, sizeof(rtcTaskSnapshot.items[i]));
  }
}

static bool restoreRtcTaskSnapshot() {
  if (rtcTaskSnapshot.magic != RTC_TASK_SNAPSHOT_MAGIC) return false;
  int n = rtcTaskSnapshot.count;
  if (n < 0) n = 0;
  if (n > RTC_SNAPSHOT_TASKS) n = RTC_SNAPSHOT_TASKS;
  if (n > MAX_TASKS) n = MAX_TASKS;

  taskCount = n;
  totalCount = rtcTaskSnapshot.total;
  activeCount = rtcTaskSnapshot.active;
  attentionCount = rtcTaskSnapshot.attention;
  waitCount = rtcTaskSnapshot.wait;
  runCount = rtcTaskSnapshot.run;
  hiddenCount = rtcTaskSnapshot.hidden;
  selected = rtcTaskSnapshot.selected;
  if (selected < 0 || selected >= max(1, taskCount)) selected = 0;
  lastError = "";

  for (int i = 0; i < taskCount; i++) {
    RtcTaskSnapshotItem& in = rtcTaskSnapshot.items[i];
    AiTask& out = tasks[i];
    out.id = in.id;
    out.source = in.source;
    out.title = in.title;
    out.status = in.status;
    out.subtitle = in.subtitle;
    out.usage = in.usage;
    out.device = in.device;
    out.ageSec = in.ageSec;
    out.attention = in.attention;
  }
  snapshotPreviewActive = true;
  return true;
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
  if (s.indexOf("cursor") >= 0) return C_WHITE;
  if (s.indexOf("codex") >= 0) return C_BLUE;
  if (s.indexOf("claude") >= 0) return C_AMBER;
  if (s.indexOf("perplexity") >= 0) return C_WHITE;
  if (s.indexOf("gemini") >= 0) return C_BLUE;
  if (s.indexOf("kimi") >= 0) return C_WHITE;
  if (s.indexOf("workbuddy") >= 0) return C_WORKBUDDY;
  if (s.indexOf("grok") >= 0) return C_WHITE;
  if (s.indexOf("lovable") >= 0) return C_LOVABLE_RED;
  if (s.indexOf("manus") >= 0) return C_GREEN;
  if (s.indexOf("openclaw") >= 0 || s.indexOf("claw") >= 0) return C_RED;
  if (s.indexOf("cline") >= 0 || s.indexOf("roo") >= 0 || s.indexOf("kilo") >= 0) return C_WHITE;
  if (s.indexOf("copilot") >= 0) return C_WHITE;
  if (s.indexOf("qwen") >= 0) return C_BLUE;
  return C_GRAY;
}

static void drawAiSourceIcon(const String& source, int x, int y, int bg) {
  String s = source;
  s.toLowerCase();
  int c = sourceLogoColor(source);
  M5.Display.fillRect(x, y, 12, 12, bg);

  if (s.indexOf("cursor") >= 0) {
    // Isometric cube: hexagon outline, filled top face, center seams.
    M5.Display.drawLine(x + 6, y + 0, x + 11, y + 3, c);
    M5.Display.drawLine(x + 6, y + 0, x + 1, y + 3, c);
    M5.Display.drawFastVLine(x + 11, y + 3, 6, c);
    M5.Display.drawFastVLine(x + 1, y + 3, 6, c);
    M5.Display.drawLine(x + 11, y + 9, x + 6, y + 11, c);
    M5.Display.drawLine(x + 1, y + 9, x + 6, y + 11, c);
    M5.Display.fillTriangle(x + 6, y + 0, x + 11, y + 3, x + 6, y + 6, c);
    M5.Display.fillTriangle(x + 6, y + 0, x + 1, y + 3, x + 6, y + 6, c);
    M5.Display.drawFastVLine(x + 6, y + 6, 6, c);
    M5.Display.drawLine(x + 1, y + 3, x + 6, y + 6, c);
    M5.Display.drawLine(x + 11, y + 3, x + 6, y + 6, c);
  } else if (s.indexOf("codex") >= 0) {
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
  } else if (s.indexOf("kimi") >= 0) {
    M5.Display.drawFastVLine(x + 2, y + 1, 10, c);
    M5.Display.drawLine(x + 3, y + 6, x + 8, y + 1, c);
    M5.Display.drawLine(x + 3, y + 6, x + 9, y + 11, c);
    M5.Display.drawLine(x + 4, y + 6, x + 10, y + 11, c);
    M5.Display.fillCircle(x + 10, y + 1, 1, C_BLUE);
  } else if (s.indexOf("workbuddy") >= 0) {
    M5.Display.fillTriangle(x + 1, y + 4, x + 3, y + 0, x + 5, y + 4, c);
    M5.Display.fillTriangle(x + 7, y + 4, x + 9, y + 0, x + 11, y + 4, c);
    M5.Display.fillRoundRect(x + 1, y + 3, 10, 8, 3, c);
    M5.Display.fillCircle(x + 4, y + 6, 2, C_WHITE);
    M5.Display.fillCircle(x + 8, y + 6, 2, C_WHITE);
    M5.Display.drawPixel(x + 4, y + 6, C_BG);
    M5.Display.drawPixel(x + 8, y + 6, C_BG);
  } else if (s.indexOf("grok") >= 0) {
    M5.Display.drawCircle(x + 6, y + 6, 5, c);
    M5.Display.drawCircle(x + 6, y + 6, 4, c);
    M5.Display.drawLine(x + 1, y + 10, x + 10, y + 1, c);
    M5.Display.drawLine(x + 2, y + 11, x + 11, y + 2, c);
    M5.Display.fillRect(x + 1, y + 1, 3, 3, bg);
    M5.Display.drawLine(x + 8, y + 8, x + 11, y + 11, c);
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
  } else if (s.indexOf("cline") >= 0 || s.indexOf("roo") >= 0 || s.indexOf("kilo") >= 0) {
    // Robot head (Cline family: Cline / Roo Code / Kilo Code): antenna, visor, two eyes.
    M5.Display.drawFastVLine(x + 6, y + 0, 2, c);
    M5.Display.fillRoundRect(x + 1, y + 2, 10, 9, 2, c);
    M5.Display.fillRect(x + 3, y + 5, 2, 2, bg);
    M5.Display.fillRect(x + 7, y + 5, 2, 2, bg);
    M5.Display.drawFastHLine(x + 4, y + 9, 4, bg);
  } else if (s.indexOf("copilot") >= 0) {
    // Goggles: two lenses joined by a bridge, strap at the sides.
    M5.Display.drawFastHLine(x + 0, y + 5, 12, c);
    M5.Display.fillCircle(x + 3, y + 6, 3, c);
    M5.Display.fillCircle(x + 8, y + 6, 3, c);
    M5.Display.fillRect(x + 2, y + 5, 2, 2, bg);
    M5.Display.fillRect(x + 7, y + 5, 2, 2, bg);
  } else if (s.indexOf("qwen") >= 0) {
    // Ring with a tail (Q).
    M5.Display.drawCircle(x + 5, y + 5, 4, c);
    M5.Display.drawCircle(x + 5, y + 5, 3, c);
    M5.Display.drawLine(x + 7, y + 7, x + 11, y + 11, c);
    M5.Display.drawLine(x + 8, y + 7, x + 11, y + 10, c);
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
  int H = M5.Display.height();
  centerText(uiText("Connecting Wi-Fi", "连接 Wi-Fi"), H * 42 / 100, C_BLUE, &fonts::efontCN_16);
  centerText(uiText("Syncing tasks", "同步任务状态"), H * 62 / 100, C_GRAY, &fonts::efontCN_12);
  setBootStatus(status, C_GRAY);
}

// ---------------------------------------------------------------------------
// First-run setup: captive-portal Wi-Fi, then code pairing with the Mac Host.
// ---------------------------------------------------------------------------

static String deviceSuffix(bool upper) {
  uint64_t mac = ESP.getEfuseMac();
  char buf[8];
  snprintf(buf, sizeof(buf), upper ? "%04X" : "%04x", (unsigned)((mac >> 32) & 0xFFFF));
  return String(buf);
}

static String htmlEscape(const String& in) {
  String out;
  out.reserve(in.length() + 8);
  for (size_t i = 0; i < in.length(); i++) {
    char c = in[i];
    switch (c) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += c;
    }
  }
  return out;
}

static void drawWifiPortalScreen(const String& status) {
  bootScreenActive = true;
  bootStatusText = "";
  M5.Display.fillScreen(C_BG);
  int H = M5.Display.height();
  int qrSize = 92;
  int qrX = 6;
  int qrY = (H - 22 - qrSize) / 2 + 2;
  String qr = "WIFI:T:nopass;S:" + apSsid + ";;";
  M5.Display.qrcode(qr.c_str(), qrX, qrY, qrSize, 3);

  int x = qrX + qrSize + 10;
  M5.Display.setTextDatum(top_left);
  M5.Display.setFont(&fonts::efontCN_16);
  M5.Display.setTextColor(C_BLUE, C_BG);
  M5.Display.drawString(uiText("Wi-Fi Setup", "配置 Wi-Fi"), x, 8);
  M5.Display.setFont(&fonts::efontCN_12);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.drawString(uiText("1. Phone: join Wi-Fi", "1. 手机连接热点"), x, 32);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.drawString(apSsid, x + 8, 46);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.drawString(uiText("2. Pick your network", "2. 在弹出页面选网络"), x, 64);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.drawString(uiText("no popup? open 192.168.4.1", "没弹出? 打开 192.168.4.1"), x, 80);
  M5.Display.drawString(uiText("Scan the QR to join", "扫码即可加入热点"), x, 92);
  setBootStatus(status, C_AMBER);
}

static void drawPairScreen() {
  bootScreenActive = true;
  bootStatusText = "";
  M5.Display.fillScreen(C_BG);
  int W = M5.Display.width();
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::efontCN_16);
  M5.Display.setTextColor(C_BLUE, C_BG);
  M5.Display.drawString(uiText("Pair with Mac", "与 Mac 配对"), W / 2, 14);
  M5.Display.setFont(&fonts::Font7);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.drawString(pairCode, W / 2, 58);
  M5.Display.setFont(&fonts::efontCN_12);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.drawString(uiText("Enter this code on your Mac", "在 Mac 上输入这个配对码"), W / 2, 92);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.drawString(pairName + "  " + uiText("hold A: redo Wi-Fi", "长按A: 重配Wi-Fi"), W / 2, 106);
  setBootStatus(pairStatus, C_AMBER);
}

static void setPairStatus(const String& text, int color) {
  pairStatus = text;
  setBootStatus(text, color);
}

static String generatePairCode() {
  uint32_t n = 1000 + (esp_random() % 9000);   // 4 digits, no leading zero
  return String(n);
}

static const char PORTAL_PAGE[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TaskHub Setup</title>
<style>body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#0b0f14;color:#e8edf2;margin:0;padding:24px}
.card{max-width:420px;margin:0 auto;background:#141b24;border-radius:14px;padding:22px}
h1{font-size:20px;margin:0 0 4px}p{color:#9aa7b4;margin:6px 0 16px;font-size:14px}
label{display:block;font-size:13px;color:#9aa7b4;margin:14px 0 6px}
select,input{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border-radius:10px;border:1px solid #2a3542;background:#0b0f14;color:#fff}
button{width:100%;margin-top:20px;font-size:17px;padding:14px;border:0;border-radius:10px;background:#3b82f6;color:#fff}
.row{display:flex;gap:10px}.row label{flex:1;margin:0;display:flex;align-items:center;gap:6px;color:#e8edf2}
.row input{width:auto}small{color:#6b7885}#other{display:none}</style></head><body><div class="card">
<h1>TaskHub · StickS3</h1><p>Choose the Wi-Fi your Mac is on. / 选择你的 Mac 所在的 Wi-Fi。</p>
<form method="post" action="/save">
<label>Wi-Fi network / 网络 <a href="/scan" style="float:right;color:#3b82f6;text-decoration:none">rescan</a></label>
<select name="ssid" id="ssid" onchange="document.getElementById('other').style.display=this.value=='__other__'?'block':'none'">
%OPTIONS%<option value="__other__">Other… / 手动输入</option></select>
<input id="other" name="ssid_other" placeholder="SSID" autocapitalize="off" autocorrect="off">
<label>Password / 密码</label><input name="password" type="password" autocapitalize="off" autocorrect="off">
<label>Device language / 设备语言</label>
<div class="row"><label><input type="radio" name="lang" value="en" %EN%>English</label><label><input type="radio" name="lang" value="zh" %ZH%>中文</label></div>
<button type="submit">Connect / 连接</button></form>
<p style="margin-top:18px"><small>Developers: USB serial provisioning still works (taskhub-provision).</small></p>
</div></body></html>)HTML";

static const char PORTAL_WAIT_PAGE[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TaskHub Setup</title>
<style>body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#0b0f14;color:#e8edf2;margin:0;padding:24px}
.card{max-width:420px;margin:0 auto;background:#141b24;border-radius:14px;padding:22px}
h1{font-size:20px;margin:0 0 10px}p{color:#9aa7b4;font-size:15px;line-height:1.5}
.code{font-size:44px;letter-spacing:8px;text-align:center;margin:18px 0;color:#fff;font-weight:700}
a{color:#3b82f6}.ok{color:#22c55e}.bad{color:#ef4444}</style></head><body><div class="card">
<h1>TaskHub · StickS3</h1><div id="s"><p>Connecting to <b>%SSID%</b>… / 正在连接…</p></div>
<script>
function tick(){fetch('/status').then(r=>r.json()).then(j=>{var s=document.getElementById('s');
if(j.state=='connected'){s.innerHTML='<p class="ok">Connected to <b>'+j.ssid+'</b> ✓</p><p>Now on your Mac: install TaskHub Host, then type this code when it asks.<br>现在在 Mac 上安装 TaskHub Host，出现提示时输入这个配对码：</p><div class="code">'+j.code+'</div><p>The code is also on the StickS3 screen. / 配对码也显示在 StickS3 屏幕上。</p>';return;}
if(j.state=='failed'){s.innerHTML='<p class="bad">Could not join <b>'+j.ssid+'</b>. Wrong password? / 连接失败，密码是否正确？</p><p><a href="/">Try again / 重试</a></p>';return;}
setTimeout(tick,1500);}).catch(function(){setTimeout(tick,2000);});}
setTimeout(tick,1500);</script></div></body></html>)HTML";

static void portalScanNetworks() {
  // Blocking scan (~2s). Runs before the AP comes up and again on /scan.
  int n = WiFi.scanNetworks(false, false);
  String options;
  for (int i = 0; i < n && i < 20; i++) {
    String ssid = WiFi.SSID(i);
    if (!ssid.length()) continue;
    if (options.indexOf("value=\"" + htmlEscape(ssid) + "\"") >= 0) continue;   // dedupe BSSIDs
    options += "<option value=\"" + htmlEscape(ssid) + "\">" + htmlEscape(ssid);
    options += String(" (") + WiFi.RSSI(i) + " dBm)</option>";
  }
  WiFi.scanDelete();
  portalScanOptions = options;
}

static void portalSendPage() {
  String page = FPSTR(PORTAL_PAGE);
  page.replace("%OPTIONS%", portalScanOptions);
  page.replace("%EN%", uiZh() ? "" : "checked");
  page.replace("%ZH%", uiZh() ? "checked" : "");
  portalServer->sendHeader("Cache-Control", "no-store");
  portalServer->send(200, "text/html; charset=utf-8", page);
}

static void portalRedirectHome() {
  // Captive-portal probes (iOS hotspot-detect, Android generate_204, Windows
  // connecttest) all land here; a redirect to our page makes the OS pop it up.
  portalServer->sendHeader("Location", "http://192.168.4.1/", true);
  portalServer->send(302, "text/plain", "");
}

static void portalHandleSave() {
  String ssid = portalServer->arg("ssid");
  if (ssid == "__other__" || !ssid.length()) ssid = portalServer->arg("ssid_other");
  ssid.trim();
  if (!ssid.length()) {
    portalServer->send(400, "text/plain; charset=utf-8", "SSID required");
    return;
  }
  portalPendingSsid = ssid;
  portalPendingPassword = portalServer->arg("password");
  portalPendingLang = normalizeLang(portalServer->arg("lang"));
  cfgLang = portalPendingLang;   // so screens switch language right away
  portalStaConnecting = true;
  portalStaFailed = false;
  portalStaStartedAt = millis();
  WiFi.disconnect(false, false);
  delay(50);
  WiFi.begin(portalPendingSsid.c_str(), portalPendingPassword.c_str());
  drawWifiPortalScreen(uiText("Joining " + portalPendingSsid + "...", "正在连接 " + portalPendingSsid + "..."));

  String page = FPSTR(PORTAL_WAIT_PAGE);
  page.replace("%SSID%", htmlEscape(portalPendingSsid));
  portalServer->sendHeader("Cache-Control", "no-store");
  portalServer->send(200, "text/html; charset=utf-8", page);
}

static void portalHandleStatus() {
  JsonDocument doc;
  const char* state = "idle";
  if (portalStaConnecting) state = "connecting";
  else if (portalStaFailed) state = "failed";
  else if (WiFi.status() == WL_CONNECTED && cfgWifiReady) state = "connected";
  doc["state"] = state;
  doc["ssid"] = portalPendingSsid.length() ? portalPendingSsid : cfgWifiSsid;
  doc["ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "";
  doc["code"] = pairCode;
  doc["name"] = pairName;
  String body;
  serializeJson(doc, body);
  portalServer->sendHeader("Cache-Control", "no-store");
  portalServer->send(200, "application/json", body);
}

static void stopWifiPortal() {
  if (!portalActive) return;
  if (portalServer) { portalServer->stop(); delete portalServer; portalServer = nullptr; }
  if (portalDns) { portalDns->stop(); delete portalDns; portalDns = nullptr; }
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  portalActive = false;
  Serial.println("[task-monitor] setup: portal stopped");
}

static void startWifiPortal() {
  setupStage = SETUP_WIFI_PORTAL;
  apSsid = "TaskHub-" + deviceSuffix(true);
  WiFi.persistent(false);
  WiFi.mode(WIFI_AP_STA);
  WiFi.disconnect(false, false);
  delay(100);
  drawWifiPortalScreen(uiText("scanning networks...", "正在扫描网络..."));
  portalScanNetworks();
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1), IPAddress(255, 255, 255, 0));
  WiFi.softAP(apSsid.c_str());
  delay(100);

  portalDns = new DNSServer();
  portalDns->setErrorReplyCode(DNSReplyCode::NoError);
  portalDns->start(53, "*", WiFi.softAPIP());

  portalServer = new WebServer(80);
  portalServer->on("/", HTTP_GET, portalSendPage);
  portalServer->on("/scan", HTTP_GET, []() { portalScanNetworks(); portalSendPage(); });
  portalServer->on("/save", HTTP_POST, portalHandleSave);
  portalServer->on("/status", HTTP_GET, portalHandleStatus);
  portalServer->onNotFound(portalRedirectHome);
  portalServer->begin();
  portalActive = true;
  drawWifiPortalScreen(uiText("waiting for phone", "等待手机连接"));
  Serial.printf("[task-monitor] setup: AP %s up, portal at 192.168.4.1\n", apSsid.c_str());
}

static void startPairing() {
  setupStage = SETUP_PAIRING;
  if (!pairCode.length()) pairCode = generatePairCode();
  pairName = "StickS3-" + deviceSuffix(true);
  pairDeviceId = (cfgDeviceId == String(DEVICE_ID)) ? "sticks3-" + deviceSuffix(false) : cfgDeviceId;
  hubDiscovered = false;
  lastPairPollAt = 0;
  lastPairStaAttemptAt = 0;
  pairStatus = uiText("connecting Wi-Fi...", "连接 Wi-Fi...");
  drawPairScreen();
  Serial.printf("[task-monitor] setup: pairing as %s code=%s\n", pairDeviceId.c_str(), pairCode.c_str());
}

static void runPairing() {
  if (pairDone) return;
  if (WiFi.status() != WL_CONNECTED) {
    hubDiscovered = false;
    if (millis() - lastPairStaAttemptAt > PAIR_STA_RETRY_MS) {
      lastPairStaAttemptAt = millis();
      if (!portalActive) WiFi.mode(WIFI_STA);
      WiFi.begin(cfgWifiSsid.c_str(), cfgWifiPassword.c_str());
      setPairStatus(uiText("connecting Wi-Fi...", "连接 Wi-Fi..."), C_BLUE);
    }
    return;
  }
  if (millis() - lastPairPollAt < PAIR_POLL_MS) return;
  lastPairPollAt = millis();

  if (!hubDiscovered && !discoverHub(true)) {
    setPairStatus(uiText("looking for Mac Host...", "正在寻找 Mac Host..."), C_AMBER);
    return;
  }

  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(4000);
  String url = "http://" + hubHost + ":" + String(hubPort) + "/pair";
  if (!http.begin(url)) {
    hubDiscovered = false;
    return;
  }
  http.addHeader("Content-Type", "application/json");
  JsonDocument req;
  req["device_id"] = pairDeviceId;
  req["name"] = pairName;
  req["code"] = pairCode;
  req["version"] = TASKHUB_FW_VERSION;
  String body;
  serializeJson(req, body);
  int status = http.POST(body);
  if (status <= 0) {
    http.end();
    hubDiscovered = false;
    setPairStatus(uiText("Host lost, searching...", "Host 失联，重新寻找..."), C_AMBER);
    return;
  }
  JsonDocument resp;
  DeserializationError err = deserializeJson(resp, http.getString());
  http.end();
  if (err || !(bool)(resp["ok"] | false)) {
    String msg = resp["error"].as<String>();
    setPairStatus(msg.length() ? msg : uiText("pairing rejected", "配对被拒绝"), C_RED);
    return;
  }
  pairHostName = resp["host_name"].as<String>();
  String state = resp["status"].as<String>();
  if (state == "approved") {
    String token = resp["token"].as<String>();
    String host = resp["host"].as<String>();
    int port = resp["port"] | hubPort;
    if (!host.length()) host = hubHost;
    bool saved = saveRuntimeConfig(cfgWifiSsid, cfgWifiPassword, host, port, pairDeviceId, token,
                                   cfgLang, cfgVoiceAutoSend);
    if (!saved) {
      setPairStatus(uiText("save failed", "保存失败"), C_RED);
      return;
    }
    pairDone = true;
    M5.Display.fillScreen(C_BG);
    centerText(uiText("Paired!", "配对成功！"), M5.Display.height() * 40 / 100, C_GREEN, &fonts::efontCN_16);
    centerText(pairHostName.length() ? pairHostName : host, M5.Display.height() * 62 / 100, C_GRAY, &fonts::efontCN_12);
    Serial.printf("[task-monitor] setup: paired with %s (%s:%d), restarting\n", pairHostName.c_str(), host.c_str(), port);
    delay(1800);
    ESP.restart();
    return;
  }
  String hostLabel = pairHostName.length() ? pairHostName : hubHost;
  setPairStatus(uiText("Mac: " + hostLabel + "  - enter code", "Mac: " + hostLabel + "  请输入配对码"), C_GREEN);
}

static void runSetupMode() {
  // Serial provisioning status heartbeat (scripts/provision_sticks3.sh waits for it).
  if (millis() - lastSetupStatusAt > 3000) {
    lastSetupStatusAt = millis();
    sendSerialConfigStatus("taskhub.status", true, "setup required");
  }

  if (portalActive) {
    portalDns->processNextRequest();
    portalServer->handleClient();

    if (portalStaConnecting) {
      if (WiFi.status() == WL_CONNECTED) {
        portalStaConnecting = false;
        portalJoinedAt = millis();
        if (saveWifiConfig(portalPendingSsid, portalPendingPassword, portalPendingLang)) {
          Serial.printf("[task-monitor] setup: joined %s ip=%s\n", portalPendingSsid.c_str(), WiFi.localIP().toString().c_str());
          startPairing();
        }
      } else if (millis() - portalStaStartedAt > PORTAL_STA_JOIN_TIMEOUT_MS) {
        portalStaConnecting = false;
        portalStaFailed = true;
        WiFi.disconnect(false, false);
        drawWifiPortalScreen(uiText("join failed - retry on phone", "连接失败，请在手机上重试"));
      }
    }
    // Keep the AP a while after joining so the phone page can show the code,
    // then drop it: a lone STA is quieter and draws less.
    if (setupStage == SETUP_PAIRING && portalJoinedAt && millis() - portalJoinedAt > PORTAL_LINGER_AFTER_JOIN_MS) {
      stopWifiPortal();
    }
  }

  if (setupStage == SETUP_PAIRING) {
    runPairing();
    if (M5.BtnA.pressedFor(SETUP_RESET_HOLD_MS)) {
      Serial.println("[task-monitor] setup: BtnA held, clearing Wi-Fi");
      clearRuntimeConfig();
      delay(200);
      ESP.restart();
    }
  }
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
  int W = M5.Display.width();
  M5.Display.fillRect(0, 0, W, 22, C_BG);
  M5.Display.drawFastHLine(0, 21, W, C_DARK);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(top_left);
  drawTaskHubMiniMark(5, 3, C_BLUE);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.drawString("TaskHub", 26, 7);
  if (W > 170) drawMetricDots(76, 10);

  int battX = W - 25;
  drawBatteryGlyph(battX, 6, battPct, battCharging);
  int wifiX = battX - 12;
  int wifiCol = wifiOk ? C_GREEN : C_AMBER;
  M5.Display.fillCircle(wifiX, 14, 2, wifiCol);
  M5.Display.drawFastVLine(wifiX, 8, 4, wifiCol);
  M5.Display.drawFastHLine(wifiX - 2, 8, 5, wifiCol);

  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.setTextDatum(top_right);
  M5.Display.drawString(String(battPct) + "%", battX - 15, 7);
}

static void centerTextBg(const String& text, int y, int color, int bg, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextColor(color, bg);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString(text, M5.Display.width() / 2, y);
}

static void centerText(const String& text, int y, int color, const lgfx::IFont* font) {
  centerTextBg(text, y, color, C_BG, font);
}

static void drawMessage(const String& line1, const String& line2, int color) {
  M5.Display.fillScreen(C_BG);
  topBar();
  int H = M5.Display.height();
  centerText(line1, H * 43 / 100, color, &fonts::efontCN_16);
  centerText(line2, H * 65 / 100, C_GRAY, &fonts::efontCN_12);
}

// Portrait layout: a compact vertical list of several tasks, making use of the
// tall screen instead of stretching the single landscape card.
static void drawPortraitList() {
  int W = M5.Display.width();
  int H = M5.Display.height();
  int top = 25;
  int bottom = H - 15;
  int rowH = 42;
  int maxRows = (bottom - top) / rowH;
  if (maxRows < 1) maxRows = 1;
  int start = 0;
  if (selected >= maxRows) start = selected - maxRows + 1;

  for (int i = 0; i < maxRows; i++) {
    int idx = start + i;
    if (idx >= taskCount) break;
    AiTask& t = tasks[idx];
    int col = statusColor(t.status);
    bool sel = (idx == selected);
    int cy = top + i * rowH;
    int ch = rowH - 5;
    int bg = sel ? C_PANEL : C_BG;
    int x = 4;
    int w = W - 8;

    M5.Display.fillRoundRect(x, cy, w, ch, 6, bg);
    if (sel) M5.Display.drawRoundRect(x, cy, w, ch, 6, C_LINE);
    M5.Display.fillRoundRect(x, cy, 4, ch, 4, col);

    drawAiSourceIcon(t.source, x + 9, cy + 6, bg);
    drawStatusPill(t.status, x + 25, cy + 5, bg, true);
    M5.Display.setFont(&fonts::Font0);
    M5.Display.setTextDatum(top_right);
    M5.Display.setTextColor(C_GRAY, bg);
    M5.Display.drawString(ageLabel(t.ageSec), x + w - 7, cy + 7);

    String srcFit = fitText(displaySourceLabel(t), &fonts::Font0, W - 94);
    M5.Display.setTextDatum(top_left);
    M5.Display.setTextColor(C_GRAY, bg);
    M5.Display.drawString(srcFit, x + 62, cy + 7);

    String title = t.title.length() ? t.title : t.source;
    String titleFit = fitText(title, &fonts::efontCN_12, W - 24);
    M5.Display.setFont(&fonts::efontCN_12);
    M5.Display.setTextColor(C_WHITE, bg);
    M5.Display.setTextDatum(top_left);
    M5.Display.drawString(titleFit, x + 10, cy + 22);
  }

  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(bottom_left);
  M5.Display.setTextColor(C_GRAY, C_BG);
  String f = String(selected + 1) + "/" + String(taskCount);
  if (snapshotPreviewActive) f += " sync";
  if (waitCount > 0) f += " " + String(waitCount) + "w";
  if (hiddenCount > 0) f += " +" + String(hiddenCount);
  M5.Display.drawString(f, 5, H - 2);
  drawIndexRail(W - 53, H - 7, 34, 4);
  M5.Display.setTextDatum(bottom_right);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.drawString("A", W - 5, H - 2);
}

static void drawList() {
  M5.Display.fillScreen(C_BG);
  topBar();

  if (taskCount == 0) {
    int H = M5.Display.height();
    bool allHidden = !lastError.length() && hiddenCount > 0;
    int cardX = 8;
    int cardY = H * 28 / 100;
    int cardW = M5.Display.width() - 16;
    int cardH = H * 44 / 100;
    M5.Display.fillRoundRect(cardX, cardY, cardW, cardH, 7, C_CARD);
    M5.Display.drawRoundRect(cardX, cardY, cardW, cardH, 7, C_LINE);
    drawTaskHubMiniMark(cardX + 12, cardY + 10, lastError.length() ? C_RED : C_BLUE);
    centerTextBg(lastError.length() ? uiText("Cannot read tasks", "无法读取任务")
                                    : (allHidden ? uiText("Old tasks hidden", "旧任务已隐藏")
                                                 : uiText("No tasks", "暂无任务")),
                 H * 45 / 100, lastError.length() ? C_RED : C_WHITE, C_CARD, &fonts::efontCN_16);
    centerTextBg(lastError.length() ? lastError
                                    : (snapshotPreviewActive ? uiText("Updating...", "更新中...")
                                                             : (allHidden ? String(hiddenCount) + uiText(" hidden · auto refresh", " hidden · 会自动刷新")
                                                                          : uiText("Auto refresh", "会定时自动刷新"))),
                 H * 64 / 100, C_GRAY, C_CARD, &fonts::efontCN_12);
    centerText(uiText("BtnA refresh", "BtnA 刷新"), H * 90 / 100, C_GRAY, &fonts::efontCN_12);
    return;
  }

  if (selected >= taskCount) selected = 0;

  if (M5.Display.height() > M5.Display.width()) {
    drawPortraitList();
    return;
  }

  AiTask& t = tasks[selected];
  int col = statusColor(t.status);
  int screenW = M5.Display.width();
  int screenH = M5.Display.height();
  int cardX = 5;
  int cardY = 25;
  int cardW = screenW - 10;
  int cardH = screenH - 47;
  int contentX = cardX + 13;
  int contentW = cardW - 24;

  M5.Display.fillRoundRect(cardX, cardY, cardW, cardH, 7, C_CARD);
  M5.Display.drawRoundRect(cardX, cardY, cardW, cardH, 7, C_LINE);
  M5.Display.fillRoundRect(cardX, cardY, 5, cardH, 5, col);
  M5.Display.drawFastHLine(contentX, cardY + 31, contentW, C_DARK);

  drawStatusPill(t.status, contentX, cardY + 8, C_CARD);
  int sourceIconX = contentX + statusPillWidth(t.status) + 8;
  int sourceTextX = sourceIconX + 15;
  drawAiSourceIcon(t.source, sourceIconX, cardY + 9, C_CARD);
  drawFittedText(displaySourceLabel(t), sourceTextX, cardY + 9, screenW - sourceTextX - 56, C_GRAY, C_CARD, &fonts::Font0);

  M5.Display.setTextDatum(top_right);
  M5.Display.setTextColor(C_GRAY, C_CARD);
  M5.Display.drawString(ageLabel(t.ageSec), screenW - 16, cardY + 8);

  String title = t.title.length() ? t.title : t.source;
  drawWrappedText(title, contentX, cardY + 38, contentW, 17, 2, C_WHITE, C_CARD, &fonts::efontCN_16);

  String meta = t.subtitle.length() ? t.subtitle : t.usage;
  int metaY = cardY + cardH - 21;
  M5.Display.fillRoundRect(contentX - 3, metaY - 2, contentW + 6, 18, 4, C_PANEL);
  drawFittedText(meta, contentX, metaY, contentW, C_GRAY, C_PANEL, &fonts::efontCN_12);

  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(snapshotPreviewActive ? C_BLUE : (t.usage.length() ? C_AMBER : (attentionCount > 0 ? C_AMBER : C_GRAY)), C_BG);
  M5.Display.setTextDatum(bottom_left);
  String footerLeft = snapshotPreviewActive ? String(uiText("Updating...", "更新中..."))
                                            : (t.usage.length() ? t.usage : String(activeCount) + " active · " + String(attentionCount) + " alert");
  if (!snapshotPreviewActive && !t.usage.length() && waitCount > 0) footerLeft += " · " + String(waitCount) + " wait";
  if (!snapshotPreviewActive && !t.usage.length() && hiddenCount > 0) footerLeft += " · " + String(hiddenCount) + " hidden";
  M5.Display.drawString(fitText(footerLeft, &fonts::Font0, screenW - 92), 7, screenH - 4);

  drawIndexRail(screenW - 82, screenH - 8, 42, 4);

  M5.Display.setTextDatum(bottom_right);
  M5.Display.setTextColor(C_GRAY, C_BG);
  String footerRight = String(selected + 1) + "/" + String(taskCount) + " A";
  M5.Display.drawString(footerRight, screenW - 6, screenH - 4);
}

// Map the IMU gravity vector to a display rotation. Returns the current
// rotation when the device is near-flat (gravity mostly on Z), so a stick lying
// on a desk doesn't flip-flop.
static int rotationFromAccel() {
  float ax = 0, ay = 0, az = 0;
  if (!M5.Imu.getAccel(&ax, &ay, &az)) return displayRotation;
  if (fabsf(ax) < ROTATE_DEADZONE_G && fabsf(ay) < ROTATE_DEADZONE_G) return displayRotation;
#if ROTATE_DEBUG
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(C_AMBER, C_BG);
  char dbg[28];
  snprintf(dbg, sizeof(dbg), "x%+.1f y%+.1f r%d", ax, ay, displayRotation);
  M5.Display.drawString(dbg, 2, M5.Display.height() - 10);
#endif
  if (fabsf(ax) > fabsf(ay)) return (ax > 0) ? ROT_X_POS : ROT_X_NEG;
  return (ay > 0) ? ROT_Y_POS : ROT_Y_NEG;
}

// Poll the IMU (throttled) and apply a new rotation once it has held steady for
// ROTATE_STABLE_MS, then repaint. No-op when auto-rotate is off or no IMU.
static void updateAutoRotate() {
#if ENABLE_AUTO_ROTATE
  if (!M5.Imu.isEnabled()) return;
  uint32_t now = millis();
  if (now - lastRotatePollAt < ROTATE_POLL_MS) return;
  lastRotatePollAt = now;
  M5.Imu.update();
  int want = rotationFromAccel();
  if (want != pendingRotation) {
    pendingRotation = want;
    pendingRotationSince = now;
  }
  if (pendingRotation != displayRotation && now - pendingRotationSince >= ROTATE_STABLE_MS) {
    displayRotation = pendingRotation;
    M5.Display.setRotation(displayRotation);
    drawList();
  }
#endif
}

static bool fetchTasks() {
  lastError = "";
  String previousSelectedId = (taskCount > 0 && selected < taskCount) ? tasks[selected].id : "";
  bool previousHadWait = waitCount > 0;
  hostSyncing = false;
  nextSyncFollowupAt = 0;
  if (!ensureWifi()) {
    lastError = uiText("Wi-Fi failed", "Wi-Fi 失败");
    if (previousHadWait || snapshotPreviewActive) clearStaleWaitSnapshot();
    Serial.println("[task-monitor] fetch failed: wifi");
    return false;
  }

  discoverHub(false);

  String body;
  int code = -1;
  bool requestOpen = false;
  HTTPClient http;
  String url = apiBase() + "/tasks?format=stick&limit=" + String(MAX_TASKS);
  setBootStatus(uiText("sync...", "同步..."), C_BLUE);
  http.begin(url);
  requestOpen = true;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Token", cfgDeviceToken);
  code = http.GET();
  if (code != 200) {
    http.end();
    requestOpen = false;
    if (discoverHub(true)) {
      url = apiBase() + "/tasks?format=stick&limit=" + String(MAX_TASKS);
      http.begin(url);
      requestOpen = true;
      http.setTimeout(HTTP_TIMEOUT_MS);
      http.addHeader("X-Device-Token", cfgDeviceToken);
      code = http.GET();
    }
  }

  if (code != 200) {
    lastError = String("HTTP ") + String(code);
    setBootStatus(lastError, C_RED);
    if (previousHadWait || snapshotPreviewActive) clearStaleWaitSnapshot();
    Serial.printf("[task-monitor] fetch failed: http=%d url=%s\n", code, url.c_str());
    if (requestOpen) http.end();
    return false;
  }

  body = http.getString();
  http.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    lastError = uiText("JSON error", "JSON 错误");
    setBootStatus(lastError, C_RED);
    if (previousHadWait || snapshotPreviewActive) clearStaleWaitSnapshot();
    Serial.printf("[task-monitor] fetch failed: json=%s\n", err.c_str());
    return false;
  }

  totalCount = doc["count"] | 0;
  activeCount = doc["active"] | 0;
  attentionCount = doc["attention"] | 0;
  hostSyncing = doc["syncing"] | false;
  nextSyncFollowupAt = hostSyncing ? millis() + STALE_SYNC_FOLLOWUP_MS : 0;
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
  detectAlertEdges();
  Serial.printf("[task-monitor] fetch ok tasks=%d hidden=%d total=%d active=%d attention=%d wait=%d alert=%d syncing=%d wifi=%s ip=%s\n",
                taskCount, hiddenCount, totalCount, activeCount, attentionCount, waitCount, (int)pendingAlert,
                (int)hostSyncing, WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
  setBootStatus(uiText("ready", "就绪"), C_GREEN);
  if (taskCount == 0) {
    selected = 0;
  } else if (lastManualSelectAt != 0 && millis() - lastManualSelectAt < MANUAL_SELECTION_HOLD_MS) {
    int prev = findTaskById(previousSelectedId);
    if (prev >= 0) selected = prev;
    else if (selected >= taskCount) selected = taskCount - 1;
  } else if (pendingAlertTask >= 0 && pendingAlertTask < taskCount) {
    // Show the task that just changed, so the beep and the screen agree.
    selected = pendingAlertTask;
  } else {
    int priority = firstPriorityTask();
    selected = priority >= 0 ? priority : 0;
  }
  snapshotPreviewActive = false;
  saveRtcTaskSnapshot();
  return true;
}

static bool fetchTasksWithStartupRetry(uint32_t retryWindowMs) {
  uint32_t start = millis();
  int attempt = 0;
  while (true) {
    attempt++;
    if (attempt > 1) {
      setBootStatus(uiText("retry sync...", "重试同步..."), C_AMBER);
    }

    if (fetchTasks()) return true;

    if (retryWindowMs == 0 || millis() - start >= retryWindowMs) {
      return false;
    }

    uint32_t waitStart = millis();
    while (millis() - waitStart < FETCH_RETRY_DELAY_MS && millis() - start < retryWindowMs) {
      M5.update();
      handleSerialConfig();
      delay(50);
    }
  }
}

static bool openSelectedTask() {
  if (taskCount == 0 || selected >= taskCount) return false;
  if (!ensureWifi()) {
    lastError = uiText("Wi-Fi failed", "Wi-Fi 失败");
    return false;
  }
  AiTask& t = tasks[selected];

  discoverHub(false);

  HTTPClient http;
  String url = apiBase() + "/tasks/" + urlEncode(t.id) + "/open";
  http.begin(url);
  bool requestOpen = true;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Token", cfgDeviceToken);
  int code = http.POST("");
  if (code != 200) {
    http.end();
    requestOpen = false;
    if (discoverHub(true)) {
      url = apiBase() + "/tasks/" + urlEncode(t.id) + "/open";
      http.begin(url);
      requestOpen = true;
      http.setTimeout(HTTP_TIMEOUT_MS);
      http.addHeader("X-Device-Token", cfgDeviceToken);
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
  centerText(String(uiText("sleep ", "休眠 ")) + String(wakeSeconds / 60) + "m", M5.Display.height() / 2, C_GRAY, &fonts::Font0);
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
  drawMessage(uiText("Refreshing", "刷新中"), apiBase(), C_AMBER);
  bool ok = fetchTasksWithStartupRetry(wokeFromSleep ? WAKE_FETCH_RETRY_MS : BOOT_FETCH_RETRY_MS);
  updateBattery();
  updateAlerts();
  if (hasWaitingTasks()) {
    M5.Display.wakeup();
    applyDisplayBrightness();
    activeTimeoutMs = waitAttentionTimeoutMs();
#if ENABLE_DEEP_SLEEP
  } else if (activeTimeoutMs == UINT32_MAX || activeTimeoutMs > INTERACTIVE_TIMEOUT_MS) {
    activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
    lastInputAt = millis();
#endif
  }
  keepAwakeForHostSyncing();
  lastRefreshAt = millis();
  drawList();
  lastInputAt = millis();
  (void)ok;
}

#if ENABLE_VOICE
static void writeWavHeader(uint8_t* p, uint32_t pcmBytes) {
  uint32_t chunk = 36 + pcmBytes;
  uint32_t rate = VOICE_SAMPLE_RATE;
  uint32_t byteRate = rate * 2;  // mono, 16-bit
  memcpy(p, "RIFF", 4);
  p[4] = chunk; p[5] = chunk >> 8; p[6] = chunk >> 16; p[7] = chunk >> 24;
  memcpy(p + 8, "WAVEfmt ", 8);
  p[16] = 16; p[17] = 0; p[18] = 0; p[19] = 0;   // fmt chunk size
  p[20] = 1;  p[21] = 0;                          // PCM
  p[22] = 1;  p[23] = 0;                          // mono
  p[24] = rate; p[25] = rate >> 8; p[26] = rate >> 16; p[27] = rate >> 24;
  p[28] = byteRate; p[29] = byteRate >> 8; p[30] = byteRate >> 16; p[31] = byteRate >> 24;
  p[32] = 2;  p[33] = 0;                          // block align
  p[34] = 16; p[35] = 0;                          // bits per sample
  memcpy(p + 36, "data", 4);
  p[40] = pcmBytes; p[41] = pcmBytes >> 8; p[42] = pcmBytes >> 16; p[43] = pcmBytes >> 24;
}

static void drawVoiceRecordingUI(uint32_t sec) {
  M5.Display.fillScreen(C_BG);
  int W = M5.Display.width();
  int H = M5.Display.height();
  topBar();
  int cardX = 10;
  int cardY = H * 24 / 100;
  int cardW = W - 20;
  int cardH = H * 55 / 100;
  M5.Display.fillRoundRect(cardX, cardY, cardW, cardH, 8, C_CARD);
  M5.Display.drawRoundRect(cardX, cardY, cardW, cardH, 8, C_LINE);

  int cx = W / 2;
  int cy = cardY + 18;
  M5.Display.fillCircle(cx, cy, 11, C_RED);
  M5.Display.drawCircle(cx, cy, 15, C_LINE);
  centerTextBg(uiText("Recording", "录音中"), cardY + cardH * 44 / 100, C_RED, C_CARD, &fonts::efontCN_16);

  String target = taskCount > 0 && selected < taskCount ? displaySourceLabel(tasks[selected]) : uiText("front app", "前台 App");
  centerTextBg(fitText(target, &fonts::efontCN_12, cardW - 16), cardY + cardH * 66 / 100, C_GRAY, C_CARD, &fonts::efontCN_12);

  String action = cfgVoiceAutoSend ? uiText("release to send", "松手发送") : uiText("release to type", "松手输入");
  centerTextBg(String(sec) + "s · " + action, cardY + cardH * 84 / 100, C_WHITE, C_CARD, &fonts::Font0);
}

// Keep the mic's DMA slots fed so capture is gapless while BtnB is held.
static void pumpMic() {
  while (M5.Mic.isRecording() < 2 && voiceSamples + VOICE_MIC_CHUNK <= VOICE_MAX_SAMPLES) {
    if (!M5.Mic.record(voicePcm + voiceSamples, VOICE_MIC_CHUNK, VOICE_SAMPLE_RATE)) break;
    voiceSamples += VOICE_MIC_CHUNK;
  }
}

static void startVoiceRecording() {
  if (!voiceBuf) {
    voiceBuf = (uint8_t*)ps_malloc(VOICE_WAV_HEADER + VOICE_MAX_SAMPLES * 2);
    if (voiceBuf) {
      voicePcm = (int16_t*)(voiceBuf + VOICE_WAV_HEADER);
    } else {
      drawMessage(uiText("Voice unavailable", "语音不可用"), uiText("PSRAM alloc failed", "PSRAM 分配失败"), C_RED);
      delay(900); drawList(); return;
    }
  }
  if (!ensureWifi()) {
    drawMessage(uiText("Voice failed", "语音失败"), uiText("Wi-Fi offline", "Wi-Fi 未连接"), C_RED);
    delay(900); drawList(); return;
  }
  voiceSamples = 0;
  M5.Speaker.end();                 // free the shared I2S for the mic
  if (!M5.Mic.begin()) {
    M5.Speaker.begin();
    drawMessage(uiText("Mic start failed", "麦克风启动失败"), "", C_RED);
    delay(900); drawList(); return;
  }
  voiceRecording = true;
  voiceStartMs = millis();
  voiceLastUiSec = 999;             // force first UI draw
  lastInputAt = millis();
}

static void stopAndSendVoice() {
  voiceRecording = false;
  uint32_t t0 = millis();
  while (M5.Mic.isRecording() && millis() - t0 < 500) delay(5);  // drain queued chunks
  M5.Mic.end();
  M5.Speaker.begin();               // restore speaker for alert tones

  uint32_t samples = voiceSamples;
  if (samples < (uint32_t)VOICE_SAMPLE_RATE / 4) {   // < 0.25s
    drawMessage(uiText("Too short", "太短了"), uiText("Hold BtnB to talk", "按住 BtnB 说话"), C_AMBER);
    delay(900); drawList(); return;
  }
  uint32_t pcmBytes = samples * 2;
  writeWavHeader(voiceBuf, pcmBytes);

  drawMessage(cfgVoiceAutoSend ? uiText("Sending...", "发送中...") : uiText("Transcribing...", "转写中..."), "", C_BLUE);
  String tid = (taskCount > 0 && selected < taskCount) ? tasks[selected].id : "";
  String url = apiBase() + "/voice";
  String sep = "?";
  if (tid.length()) {
    url += sep + "task=" + urlEncode(tid);
    sep = "&";
  }
  if (cfgVoiceAutoSend) {
    url += sep + "enter=1";
  }

  HTTPClient http;
  http.begin(url);
  http.addHeader("X-Device-Token", cfgDeviceToken);
  http.addHeader("Content-Type", "audio/wav");
  http.setTimeout(VOICE_HTTP_TIMEOUT_MS);
  int code = http.POST(voiceBuf, VOICE_WAV_HEADER + pcmBytes);
  String resp = (code > 0) ? http.getString() : "";
  http.end();

  if (code == 200) {
    JsonDocument doc;
    String text = "";
    String injectError = "";
    bool injected = false;
    if (deserializeJson(doc, resp) == DeserializationError::Ok) {
      text = String((const char*)(doc["text"] | ""));
      injected = doc["injected"] | false;
      injectError = String((const char*)(doc["inject_error"] | ""));
    }
    if (text.length() == 0) {
      drawMessage(uiText("No speech heard", "没听清"), uiText("Try again", "再试一次"), C_AMBER);
    } else if (!injected) {
      if (injectError.indexOf("Accessibility") >= 0 || injectError.indexOf("accessibility") >= 0) {
        injectError = uiText("Check Accessibility", "检查辅助权限");
      } else if (injectError.length() > 34) {
        injectError = injectError.substring(0, 34);
      }
      drawMessage(uiText("Type failed", "输入失败"),
                  injectError.length() ? injectError : uiText("Check Accessibility", "检查辅助权限"),
                  C_RED);
    } else {
      drawMessage(injected ? (cfgVoiceAutoSend ? uiText("Sent", "已发送") : uiText("Typed", "已输入"))
                           : uiText("Transcribed", "已转写"),
                  text, injected ? C_GREEN : C_AMBER);
    }
  } else {
    drawMessage(uiText("Voice failed", "语音失败"), code > 0 ? String("HTTP ") + code : uiText("Cannot connect", "无法连接"), C_RED);
  }
  delay(1400);
  drawList();
}

// Called every loop: while BtnB is held, keep recording and update the timer;
// on release (or max length) stop and send.
static void updateVoiceRecording() {
  if (!voiceRecording) return;
  lastInputAt = millis();
  pumpMic();
  uint32_t elapsed = millis() - voiceStartMs;
  uint32_t sec = elapsed / 1000;
  if (sec != voiceLastUiSec) {
    voiceLastUiSec = sec;
    drawVoiceRecordingUI(sec);
  }
  if (!btnBStablePressed || elapsed >= (uint32_t)VOICE_MAX_SECONDS * 1000) {
    stopAndSendVoice();
  }
}
#endif  // ENABLE_VOICE

static void handleButtons() {
  // BtnA: click = next task (or refresh when the list is empty), hold = refresh.
  if (M5.BtnA.wasHold()) {
    refreshNow();
  } else if (M5.BtnA.wasClicked()) {
    lastInputAt = millis();
    if (taskCount > 0) {
      lastManualSelectAt = millis();
      selected = (selected + 1) % taskCount;
      drawList();
    } else {
      refreshNow();
    }
  }

  // BtnB: click = open the selected task on the Mac; hold = voice (hold-to-talk).
  bool bHold = btnBHoldEvent;
  bool bClick = btnBClickEvent;
  btnBHoldEvent = false;
  btnBClickEvent = false;

#if ENABLE_VOICE
  if (bHold && !voiceRecording) {
    startVoiceRecording();   // recording continues in updateVoiceRecording()
  }
#endif

  if (bClick && !voiceRecording) {
    lastInputAt = millis();
    drawMessage(uiText("Opening task", "打开任务"), taskCount ? tasks[selected].source : uiText("no task", "无任务"), C_BLUE);
    bool ok = openSelectedTask();
    drawMessage(ok ? uiText("Open request sent", "已发送打开请求") : uiText("Open failed", "打开失败"),
                ok ? uiText("Mac switches app", "Mac 会切到对应 App") : lastError,
                ok ? C_GREEN : C_RED);
    delay(900);
    drawList();
  }
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  displayRotation = 1;
  pendingRotation = 1;
#if ENABLE_AUTO_ROTATE
  if (M5.Imu.isEnabled()) {
    M5.Imu.update();
    int r0 = rotationFromAccel();
    displayRotation = r0;
    pendingRotation = r0;
    M5.Display.setRotation(r0);
  }
#endif
  pinMode((int)PIN_BTN_A, INPUT_PULLUP);
  pinMode((int)PIN_BTN_B, INPUT_PULLUP);
  M5.BtnA.setHoldThresh(350);
  M5.BtnB.setHoldThresh(600);
  Serial.begin(115200);
  updateBattery();
  applyPowerProfile();
  if (digitalRead((int)PIN_BTN_A) == LOW && digitalRead((int)PIN_BTN_B) == LOW) {
    clearRuntimeConfig();
    rtcHasCachedBssid = false;
    Serial.println("[task-monitor] runtime config cleared by boot buttons");
  } else {
    loadRuntimeConfig();
  }
  hubHost = cfgHubHost;
  hubPort = cfgHubPort;

  esp_sleep_wakeup_cause_t wakeCause = esp_sleep_get_wakeup_cause();
  wokeByTimer = wakeCause == ESP_SLEEP_WAKEUP_TIMER;
  wokeFromSleep = wakeCause == ESP_SLEEP_WAKEUP_TIMER || wakeCause == ESP_SLEEP_WAKEUP_EXT1;

  if (!cfgReady) {
    // First run (or config cleared): no deep sleep, fixed landscape, and the
    // captive portal / pairing flow. USB serial provisioning stays available.
    setupMode = true;
    M5.Display.setRotation(1);
    displayRotation = 1;
    pendingRotation = 1;
    activeTimeoutMs = UINT32_MAX;
    lastInputAt = millis();
    Serial.printf("[task-monitor] setup required: wifi=%d token=%d\n", (int)cfgWifiReady, (int)(cfgDeviceToken.length() > 0));
    if (cfgWifiReady) {
      WiFi.persistent(false);
      startPairing();
    } else {
      startWifiPortal();
    }
    sendSerialConfigStatus("taskhub.status", true, "setup required");
    lastSetupStatusAt = millis();
    return;
  }

  if (wokeFromSleep && restoreRtcTaskSnapshot()) {
    drawList();
  } else if (wokeFromSleep) {
    drawWakeSyncScreen("wifi...");
  } else {
    drawBootScreen("boot...");
  }

  bool ok = fetchTasksWithStartupRetry(wokeFromSleep ? WAKE_FETCH_RETRY_MS : BOOT_FETCH_RETRY_MS);
  updateBattery();
  updateAlerts();
#if ENABLE_DEEP_SLEEP
  activeTimeoutMs = hasWaitingTasks() ? waitAttentionTimeoutMs() : ((wokeByTimer && attentionCount == 0 && ok) ? QUIET_TIMER_TIMEOUT_MS : INTERACTIVE_TIMEOUT_MS);
#else
  activeTimeoutMs = UINT32_MAX;
#endif
  keepAwakeForHostSyncing();
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
  handleSerialConfig();

  if (setupMode) {
    static uint32_t lastSetupBattAt = 0;
    if (millis() - lastSetupBattAt > 2000) {
      lastSetupBattAt = millis();
      updateBattery();
    }
    runSetupMode();
    delay(10);
    return;
  }

  updateBtnBEdge();
  handleButtons();
#if ENABLE_VOICE
  if (voiceRecording) {
    updateVoiceRecording();   // pump mic + watch for release; owns the loop
    delay(5);
    return;
  }
#endif
  updateAutoRotate();

  static uint32_t lastBattAt = 0;
  if (millis() - lastBattAt > 2000) {
    lastBattAt = millis();
    int old = battPct;
    bool oldCharging = battCharging;
    bool oldWifi = wifiOk;
    updateBattery();
    wifiOk = WiFi.status() == WL_CONNECTED;
    if (old != battPct || oldCharging != battCharging || oldWifi != wifiOk) drawList();
  }

  if (hostSyncing && nextSyncFollowupAt != 0 && (int32_t)(millis() - nextSyncFollowupAt) >= 0) {
    bool ok = fetchTasks();
    updateBattery();
    updateAlerts();
    if (hasWaitingTasks()) {
      M5.Display.wakeup();
      applyDisplayBrightness();
      activeTimeoutMs = waitAttentionTimeoutMs();
#if ENABLE_DEEP_SLEEP
    } else if (activeTimeoutMs == UINT32_MAX || activeTimeoutMs > INTERACTIVE_TIMEOUT_MS) {
      activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
#endif
    }
    keepAwakeForHostSyncing();
    lastRefreshAt = millis();
    drawList();
    (void)ok;
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
      activeTimeoutMs = waitAttentionTimeoutMs();
#if ENABLE_DEEP_SLEEP
    } else if (activeTimeoutMs == UINT32_MAX || activeTimeoutMs > INTERACTIVE_TIMEOUT_MS) {
      activeTimeoutMs = INTERACTIVE_TIMEOUT_MS;
      lastInputAt = millis();
#endif
    }
    keepAwakeForHostSyncing();
    lastRefreshAt = millis();
    drawList();
    (void)ok;
  }

  if (millis() - lastInputAt > activeTimeoutMs) {
    enterDeepSleep();
  }
  delay(40);
}
