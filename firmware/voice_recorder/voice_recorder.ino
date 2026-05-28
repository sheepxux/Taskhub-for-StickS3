/*
 * StickS3 voice-recorder — Step 4 + 5a + 6 firmware.
 *
 * State machine + 6 screens + button handling, per voice-recorder-design.md
 * §4 §5 §6. Step 5a records ES8311 mic → 16 kHz mono PCM (WAV) to LittleFS.
 * Step 6 adds Wi-Fi (station + auto-reconnect), NTP time, and HTTP multipart
 * upload to the OpenClaw skill server. Opus (Step 5b) is deferred — WAV goes
 * end-to-end (the backend decodes by content via ffmpeg). Notifications are
 * still faked until Step 7.
 *
 * Wi-Fi / server / device config lives in secrets.h (gitignored; see
 * secrets.h.example). Step 6 uses plain HTTP on the home LAN, no TLS yet.
 *
 * Acceptance (Step 6): hold BtnA → record → release → auto-upload → within a
 * few seconds memory/voice/YYYY-MM-DD.md gains the transcribed entry.
 *
 * Board: M5Stack ESP32-S3 stick (M5Unified auto-detects the panel).
 * Lib:   M5Unified (pulls in M5GFX). Chinese glyphs via fonts::efontCN_*.
 */
#include <M5Unified.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <WiFi.h>
#include <time.h>
#include <math.h>
#include <string.h>
#include "secrets.h"

// ----------------------------------------------------------------- palette
// Multi-colour state encoding (design §5 / ADR-006).
static constexpr int C_WHITE = TFT_WHITE;   // primary text
static constexpr int C_GREEN = TFT_GREEN;   // synced / saved
static constexpr int C_AMBER = TFT_ORANGE;  // active / pending / todo
static constexpr int C_RED   = TFT_RED;     // destructive
static constexpr int C_BLUE  = 0x5BDF;      // notification (light blue)
static constexpr int C_GRAY  = TFT_DARKGREY;
static constexpr int C_BG    = TFT_BLACK;

// ----------------------------------------------------------------- state
enum class S {
  SLEEP,           // stand-in for DEEP_SLEEP (no real esp_deep_sleep so USB stays up)
  IDLE,
  RECORDING,
  SAVED,
  REVIEW,
  DELETE_CONFIRM,
  SYNCING,
  NOTIFICATION,
};

// State timeouts (ms); 0 = no auto timeout. (design §6 table)
static uint32_t timeoutMs(S s) {
  switch (s) {
    case S::IDLE:           return 8000;
    case S::SAVED:          return 1500;
    case S::REVIEW:         return 30000;
    case S::DELETE_CONFIRM: return 2000;
    case S::NOTIFICATION:   return 20000;
    case S::SYNCING:        return 2000;   // skeleton: fake sync completes in 2s
    default:                return 0;      // SLEEP / RECORDING: no timeout
  }
}

// ----------------------------------------------------------------- fake data
struct Entry { const char* time; const char* title; const char* preview; };
static Entry entries[] = {
  {"14:32", "咖啡馆灵感", "想到一个产品点子——把本地大模型跑在路由器上，家里设备共享一个隐私推理节点。"},
  {"16:05", "读 Bret Victor", "Inventing on Principle 那段关于即时反馈的论述，creator 要直接看到结果。"},
  {"18:20", "买咖啡豆", "明天记得买咖啡豆，埃塞俄比亚水洗那款喝完了。"},
};
static int entryCount = 3;
static int todayCount = 3;        // shown on IDLE
static int pendingSync = 1;       // 待同步条数
static bool wifiOk = true;
static int battPct = 87;

static bool unreadNotif = true;   // one unread daily summary
static const char* notifTitle = "今天记了 7 条";
static const char* notifBody  = "产品点子(3) · 阅读笔记(2) · 待办(2)。详情已推送到 Telegram。";

// ----------------------------------------------------------------- runtime
static S        state = S::IDLE;
static uint32_t stateSince = 0;   // millis when current state entered
static bool     dirty = true;     // needs full redraw
static uint32_t recordStart = 0;  // RECORDING press time
static int      reviewIdx = 0;    // REVIEW cursor

static void enter(S s) {
  state = s;
  stateSince = millis();
  dirty = true;
}

static uint32_t elapsed() { return millis() - stateSince; }

// ----------------------------------------------------------------- recording (Step 5a)
// ES8311 mic → 16 kHz mono 16-bit PCM, streamed to a WAV on LittleFS.
// Double-buffered capture (M5.Mic.record is async): we queue recBuf[recQ] and,
// once a call reports a completed buffer, flush recBuf[recW] (2 behind) to file.
static constexpr uint32_t REC_SR    = 16000;
static constexpr size_t   REC_CHUNK = 512;          // samples per buffer (~32 ms)
static constexpr int      REC_NBUF  = 4;
static constexpr int      VOL_BARS  = 17;           // matches UI bar count

static int16_t   recBuf[REC_NBUF][REC_CHUNK];
static int       recQ = 2, recW = 0;                // queue / write indices, offset 2
static File      recFile;
static char      recPath[32];
static bool      recActive = false;
static uint32_t  recSamples = 0;
static uint32_t  recDurationMs = 0;
static int       volBars[VOL_BARS] = {0};           // recent peak levels 0..100
static int       volHead = 0;
static Preferences prefs;
static uint32_t  clientSeq = 0;

// (re)write the 44-byte canonical PCM WAV header for the given data length.
static void writeWavHeader(File& f, uint32_t dataBytes) {
  const uint16_t ch = 1, bits = 16, pcm = 1;
  const uint32_t sr = REC_SR;
  const uint32_t byteRate = sr * ch * bits / 8;
  const uint16_t blockAlign = ch * bits / 8;
  const uint32_t fmtLen = 16, riff = 36 + dataBytes;
  f.seek(0);
  f.write((const uint8_t*)"RIFF", 4); f.write((const uint8_t*)&riff, 4);
  f.write((const uint8_t*)"WAVE", 4);
  f.write((const uint8_t*)"fmt ", 4); f.write((const uint8_t*)&fmtLen, 4);
  f.write((const uint8_t*)&pcm, 2);   f.write((const uint8_t*)&ch, 2);
  f.write((const uint8_t*)&sr, 4);    f.write((const uint8_t*)&byteRate, 4);
  f.write((const uint8_t*)&blockAlign, 2); f.write((const uint8_t*)&bits, 2);
  f.write((const uint8_t*)"data", 4); f.write((const uint8_t*)&dataBytes, 4);
}

// ISO 8601 (+08:00) from NTP-synced clock; empty until time is set.
static bool timeSynced() { return time(nullptr) > 1700000000; }  // ~2023-11
static String isoNow() {
  time_t t = time(nullptr);
  struct tm tmv;
  localtime_r(&t, &tmv);
  char buf[40];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S+08:00", &tmv);
  return String(buf);
}

static void startRecording() {
  clientSeq++;
  prefs.putUInt("seq", clientSeq);
  snprintf(recPath, sizeof(recPath), "/rec/%04u.wav", clientSeq);
  recFile = LittleFS.open(recPath, FILE_WRITE);
  recActive = (bool)recFile;
  recSamples = 0;
  recQ = 2; recW = 0;
  memset(volBars, 0, sizeof(volBars)); volHead = 0;
  if (recActive) {
    uint8_t hdr[44] = {0};
    recFile.write(hdr, sizeof(hdr));   // placeholder, finalized on stop
  }
  M5.Speaker.end();                    // mic & speaker are mutually exclusive
  M5.Mic.begin();
  Serial.printf("[rec] start %s micEnabled=%d fileOk=%d\n",
                recPath, (int)M5.Mic.isEnabled(), (int)recActive);
}

static void pollRecording() {
  if (!recActive || !M5.Mic.isEnabled()) return;
  if (M5.Mic.record(recBuf[recQ], REC_CHUNK, REC_SR)) {
    int16_t* done = recBuf[recW];
    recFile.write((const uint8_t*)done, REC_CHUNK * sizeof(int16_t));
    recSamples += REC_CHUNK;
    int peak = 0;
    for (size_t i = 0; i < REC_CHUNK; i++) { int a = abs(done[i]); if (a > peak) peak = a; }
    volBars[volHead] = (peak * 100) / 32768;
    volHead = (volHead + 1) % VOL_BARS;
    recQ = (recQ + 1) % REC_NBUF;
    recW = (recW + 1) % REC_NBUF;
  }
}

// Finalize: drain queue, write real header, close. Discards file if !save.
static void stopRecording(bool save) {
  while (M5.Mic.isRecording()) { M5.delay(1); }
  M5.Mic.end();
  M5.Speaker.begin();
  if (!recActive) { recDurationMs = 0; return; }
  uint32_t dataBytes = recSamples * sizeof(int16_t);
  writeWavHeader(recFile, dataBytes);
  recFile.close();
  recActive = false;
  recDurationMs = (uint32_t)((uint64_t)recSamples * 1000 / REC_SR);
  if (!save) {
    LittleFS.remove(recPath);
    Serial.printf("[rec] discard %s\n", recPath);
  } else {
    // Sidecar with capture time + duration, read back at upload so recorded_at
    // reflects when it was spoken, not when it syncs. Lines: ISO ts, duration_ms.
    char metaPath[40];
    snprintf(metaPath, sizeof(metaPath), "/rec/%04u.meta", clientSeq);
    File m = LittleFS.open(metaPath, FILE_WRITE);
    if (m) {
      m.println(timeSynced() ? isoNow() : String(""));
      m.println(recDurationMs);
      m.close();
    }
    Serial.printf("[rec] saved %s bytes=%u dur=%ums\n", recPath, dataBytes, recDurationMs);
  }
}

// ----------------------------------------------------------------- networking (Step 6)
static bool wifiUp() { return WiFi.status() == WL_CONNECTED; }

static void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  configTzTime("CST-8", "pool.ntp.org", "time.apple.com", "ntp.aliyun.com");  // +08:00 wall clock
}

static String baseName(String n) {
  int s = n.lastIndexOf('/');
  return s >= 0 ? n.substring(s + 1) : n;     // LittleFS .name() varies: full path vs basename
}

static int countPending() {
  File dir = LittleFS.open("/rec");
  if (!dir) return 0;
  int n = 0;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    if (baseName(f.name()).endsWith(".wav")) n++;
    f.close();
  }
  dir.close();
  return n;
}

// POST one WAV as multipart/form-data; returns true on HTTP 200, then deletes it.
static bool uploadOne(const String& wavName) {
  String base = wavName.substring(0, wavName.lastIndexOf('.'));   // "0001"
  String wavPath = "/rec/" + wavName, metaPath = "/rec/" + base + ".meta";

  File f = LittleFS.open(wavPath, FILE_READ);
  if (!f) return false;
  size_t fsize = f.size();

  String isoTs; uint32_t durMs = 0;
  File m = LittleFS.open(metaPath, FILE_READ);
  if (m) { isoTs = m.readStringUntil('\n'); isoTs.trim(); durMs = m.readStringUntil('\n').toInt(); m.close(); }
  if (isoTs.length() == 0) isoTs = timeSynced() ? isoNow() : "1970-01-01T00:00:00+08:00";

  WiFiClient client;
  if (!client.connect(SERVER_HOST, SERVER_PORT)) { f.close(); return false; }
  client.setTimeout(10000);

  const String B = "----sticks3boundary7e3f";
  auto fld = [&](const char* name, const String& val) {
    return "--" + B + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n" + val + "\r\n";
  };
  String head = fld("recorded_at", isoTs) + fld("device_id", DEVICE_ID)
              + fld("duration_ms", String(durMs)) + fld("client_seq", String(base.toInt()))
              + "--" + B + "\r\nContent-Disposition: form-data; name=\"audio\"; filename=\""
              + base + ".wav\"\r\nContent-Type: audio/wav\r\n\r\n";
  String tail = "\r\n--" + B + "--\r\n";
  size_t contentLen = head.length() + fsize + tail.length();

  client.printf("POST /api/v1/entries HTTP/1.1\r\n");
  client.printf("Host: %s:%d\r\n", SERVER_HOST, (int)SERVER_PORT);
  client.printf("X-Device-Token: %s\r\n", DEVICE_TOKEN);
  client.printf("Content-Type: multipart/form-data; boundary=%s\r\n", B.c_str());
  client.printf("Content-Length: %u\r\n", (unsigned)contentLen);
  client.print("Connection: close\r\n\r\n");
  client.print(head);
  uint8_t buf[1024];
  while (f.available()) { size_t n = f.read(buf, sizeof(buf)); client.write(buf, n); }
  f.close();
  client.print(tail);

  uint32_t t0 = millis();
  while (!client.available() && millis() - t0 < 10000) delay(10);
  String status = client.readStringUntil('\n');   // "HTTP/1.1 200 OK"
  client.stop();

  bool ok = status.indexOf(" 200") > 0;
  Serial.printf("[net] upload %s -> %s\n", wavPath.c_str(), status.c_str());
  if (ok) { LittleFS.remove(wavPath); LittleFS.remove(metaPath); }
  return ok;
}

// Upload every pending WAV (cap 16/pass); returns count still pending afterward.
static int uploadAll() {
  if (!wifiUp()) return countPending();
  File dir = LittleFS.open("/rec");
  if (!dir) return 0;
  String names[16]; int cnt = 0;
  for (File f = dir.openNextFile(); f && cnt < 16; f = dir.openNextFile()) {
    String n = baseName(f.name());
    if (n.endsWith(".wav")) names[cnt++] = n;
    f.close();
  }
  dir.close();
  for (int i = 0; i < cnt; i++) uploadOne(names[i]);
  return countPending();
}

// ----------------------------------------------------------------- draw helpers
static void clearBg() { M5.Display.fillScreen(C_BG); }

static void centerText(const char* s, int y, int color, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextColor(color, C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString(s, M5.Display.width() / 2, y);
}

// ----------------------------------------------------------------- screens
static void drawIdle() {
  clearBg();
  const int W = M5.Display.width();
  // top bar: time + wifi + battery
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.setTextDatum(top_left);
  M5.Display.drawString("14:32", 6, 6);
  char bat[8]; snprintf(bat, sizeof(bat), "%d%%", battPct);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.setTextDatum(top_right);
  M5.Display.drawString(bat, W - 6, 6);
  M5.Display.setTextColor(wifiOk ? C_GREEN : C_AMBER, C_BG);
  M5.Display.drawString(wifiOk ? "wifi" : "x", W - 34, 6);

  // center: big count + label
  char cnt[8]; snprintf(cnt, sizeof(cnt), "%d", todayCount);
  centerText(cnt, 64, C_WHITE, &fonts::Font7);
  centerText("今日已记", 104, C_GRAY, &fonts::efontCN_16);

  // bottom: sync status
  if (pendingSync > 0) {
    char p[24]; snprintf(p, sizeof(p), "待同步 %d 条", pendingSync);
    M5.Display.fillCircle(70, 126, 3, C_AMBER);
    centerText(p, 126, C_AMBER, &fonts::efontCN_12);
  } else {
    M5.Display.fillCircle(86, 126, 3, C_GREEN);
    centerText("已同步", 126, C_GREEN, &fonts::efontCN_12);
  }
}

static void drawRecording() {
  // dynamic: redraw each frame for timer + volume bars
  clearBg();
  const int W = M5.Display.width();
  centerText("● 录音中", 14, C_AMBER, &fonts::efontCN_14);

  // pulsing ring
  uint32_t t = elapsed();
  int r = 26 + (int)(6 * (1 + sinf(t / 250.0f)));
  M5.Display.drawCircle(W / 2, 60, r, C_AMBER);
  M5.Display.drawCircle(W / 2, 60, r + 6, C_GRAY);

  // timer driven by samples actually captured, not wall clock
  uint32_t secs = recSamples / REC_SR;
  char dur[8];
  snprintf(dur, sizeof(dur), "%u:%02u", secs / 60, secs % 60);
  centerText(dur, 60, C_WHITE, &fonts::Font4);

  // 17 volume bars from real mic peak history (oldest → newest)
  int bars = VOL_BARS, bw = 8, gap = 4, totalW = bars * bw + (bars - 1) * gap;
  int x0 = (W - totalW) / 2;
  for (int i = 0; i < bars; i++) {
    int lvl = volBars[(volHead + i) % VOL_BARS];
    int h = 4 + (lvl * 18) / 100;
    int x = x0 + i * (bw + gap);
    M5.Display.fillRect(x, 112 - h, bw, h, C_AMBER);
  }
  centerText("松开停止", 128, C_GRAY, &fonts::efontCN_12);
}

static void drawSaved() {
  clearBg();
  const int W = M5.Display.width();
  // bouncy check (simple scale by elapsed)
  int r = 22;
  M5.Display.fillCircle(W / 2, 56, r, C_GREEN);
  // checkmark
  int cx = W / 2, cy = 56;
  M5.Display.drawLine(cx - 10, cy + 1, cx - 3, cy + 8, C_BG);
  M5.Display.drawLine(cx - 3, cy + 8, cx + 11, cy - 8, C_BG);
  M5.Display.drawLine(cx - 10, cy + 2, cx - 3, cy + 9, C_BG);
  M5.Display.drawLine(cx - 3, cy + 9, cx + 11, cy - 7, C_BG);
  char saved[16];
  snprintf(saved, sizeof(saved), "已记 %u:%02u",
           recDurationMs / 60000, (recDurationMs / 1000) % 60);
  centerText(saved, 96, C_WHITE, &fonts::efontCN_16);
  if (pendingSync > 0) {
    char p[24]; snprintf(p, sizeof(p), "待同步 %d 条", pendingSync);
    centerText(p, 122, C_AMBER, &fonts::efontCN_12);
  }
}

static void drawReview() {
  clearBg();
  const int W = M5.Display.width();
  Entry& e = entries[reviewIdx];
  // top: index + time
  char idx[16]; snprintf(idx, sizeof(idx), "%d / %d", reviewIdx + 1, entryCount);
  M5.Display.setFont(&fonts::Font0);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.setTextDatum(top_left);
  M5.Display.drawString(idx, 6, 6);
  M5.Display.setTextDatum(top_right);
  char ttl[24]; snprintf(ttl, sizeof(ttl), "今天 %s", e.time);
  M5.Display.drawString(ttl, W - 6, 6);

  // title
  M5.Display.setFont(&fonts::efontCN_24);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.setTextDatum(top_left);
  M5.Display.drawString(e.title, 8, 26);

  // preview (wrap)
  M5.Display.setFont(&fonts::efontCN_14);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.setTextWrap(true);
  M5.Display.setCursor(8, 58);
  M5.Display.print(e.preview);
  M5.Display.setTextWrap(false);

  centerText("BtnB 上一条 · 长按退出", 128, C_GRAY, &fonts::efontCN_12);
}

static void drawDeleteConfirm() {
  clearBg();
  const int W = M5.Display.width();
  // warning triangle
  int cx = W / 2;
  M5.Display.fillTriangle(cx, 14, cx - 14, 38, cx + 14, 38, C_RED);
  M5.Display.setTextColor(C_WHITE, C_RED);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setFont(&fonts::Font2);
  M5.Display.drawString("!", cx, 33);

  centerText("再次双击删除", 60, C_WHITE, &fonts::efontCN_16);
  centerText("这条无法恢复", 84, C_GRAY, &fonts::efontCN_12);

  // linear receding progress bar (2s)
  float frac = 1.0f - (float)elapsed() / (float)timeoutMs(S::DELETE_CONFIRM);
  if (frac < 0) frac = 0;
  int bw = (int)((W - 20) * frac);
  M5.Display.fillRect(10, 104, bw, 6, C_RED);
  M5.Display.drawRect(10, 104, W - 20, 6, C_GRAY);
  centerText("2 秒内无操作则取消", 126, C_GRAY, &fonts::efontCN_12);
}

static void drawSyncing() {
  clearBg();
  centerText("↑ 同步中", 50, C_AMBER, &fonts::efontCN_16);
  int dots = (elapsed() / 400) % 4;
  char d[8] = "";
  for (int i = 0; i < dots; i++) strcat(d, ".");
  centerText(d, 80, C_AMBER, &fonts::Font4);
}

static void drawNotification() {
  clearBg();
  const int W = M5.Display.width();
  M5.Display.fillCircle(16, 14, 5, C_BLUE);  // bell stand-in
  M5.Display.setFont(&fonts::efontCN_12);
  M5.Display.setTextColor(C_BLUE, C_BG);
  M5.Display.setTextDatum(top_left);
  M5.Display.drawString("晚间总结 · 22:00", 28, 8);

  M5.Display.setFont(&fonts::efontCN_16);
  M5.Display.setTextColor(C_WHITE, C_BG);
  M5.Display.drawString(notifTitle, 8, 34);

  M5.Display.setFont(&fonts::efontCN_14);
  M5.Display.setTextColor(C_GRAY, C_BG);
  M5.Display.setTextWrap(true);
  M5.Display.setCursor(8, 62);
  M5.Display.print(notifBody);
  M5.Display.setTextWrap(false);

  centerText("BtnB 标记已读", 128, C_GRAY, &fonts::efontCN_12);
}

static void drawSleep() {
  clearBg();
  centerText("按键唤醒", M5.Display.height() / 2, C_GRAY, &fonts::efontCN_12);
}

static void render() {
  switch (state) {
    case S::SLEEP:          drawSleep();          break;
    case S::IDLE:           drawIdle();           break;
    case S::RECORDING:      drawRecording();      break;
    case S::SAVED:          drawSaved();          break;
    case S::REVIEW:         drawReview();         break;
    case S::DELETE_CONFIRM: drawDeleteConfirm();  break;
    case S::SYNCING:        drawSyncing();        break;
    case S::NOTIFICATION:   drawNotification();   break;
  }
}

// ----------------------------------------------------------------- transitions
// Show the syncing screen, then upload pending entries (blocking — clips are
// small), update the pending count, and return to IDLE.
static void runSync() {
  enter(S::SYNCING);
  render();
  pendingSync = uploadAll();
  enter(S::IDLE);
}

static void handleButtons() {
  switch (state) {
    case S::SLEEP:
      if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
        M5.Display.wakeup();
        enter(unreadNotif ? S::NOTIFICATION : S::IDLE);  // pull-on-wake (design §6)
      }
      break;

    case S::IDLE:
      if (M5.BtnA.wasPressed()) { recordStart = millis(); startRecording(); enter(S::RECORDING); }
      else if (M5.BtnB.wasHold()) { if (wifiUp()) runSync(); }
      else if (M5.BtnB.wasClicked()) { if (todayCount > 0) { reviewIdx = 0; enter(S::REVIEW); } }
      break;

    case S::RECORDING:
      if (M5.BtnA.wasReleased()) {
        uint32_t held = millis() - recordStart;
        bool save = held >= 150;
        stopRecording(save);
        if (save) {                    // saved
          entryCount++; todayCount++;
          pendingSync = countPending();
          enter(S::SAVED);
        } else {                       // too short → discard
          enter(S::IDLE);
        }
      }
      break;

    case S::REVIEW:
      // wasSingleClicked (not wasClicked) waits out the double-click window so the
      // first tap of a double-click no longer advances the entry. (design ADR-005)
      if (M5.BtnB.wasHold()) { enter(S::IDLE); }
      else if (M5.BtnB.wasDoubleClicked()) { enter(S::DELETE_CONFIRM); }
      else if (M5.BtnB.wasSingleClicked()) { reviewIdx = (reviewIdx + 1) % entryCount; dirty = true; }
      break;

    case S::DELETE_CONFIRM:
      if (M5.BtnB.wasDoubleClicked()) {           // confirm delete
        // remove entries[reviewIdx]
        for (int i = reviewIdx; i < entryCount - 1; i++) entries[i] = entries[i + 1];
        entryCount--; if (entryCount < 0) entryCount = 0;
        if (reviewIdx >= entryCount) reviewIdx = entryCount > 0 ? entryCount - 1 : 0;
        if (todayCount > 0) todayCount--;
        enter(entryCount > 0 ? S::REVIEW : S::IDLE);
      }
      break;

    case S::NOTIFICATION:
      if (M5.BtnB.wasClicked()) { unreadNotif = false; enter(S::IDLE); }
      break;

    case S::SYNCING:
      break;  // auto-times out
    default: break;
  }
}

static void handleTimeouts() {
  uint32_t to = timeoutMs(state);
  if (to == 0 || elapsed() < to) return;
  switch (state) {
    case S::IDLE:           M5.Display.sleep(); enter(S::SLEEP); break;
    // After showing the saved tick, auto-upload if Wi-Fi is up; else stay pending.
    case S::SAVED:          if (wifiUp() && pendingSync > 0) runSync(); else enter(S::IDLE); break;
    case S::REVIEW:         enter(S::IDLE);   break;
    case S::DELETE_CONFIRM: enter(S::REVIEW); break;
    case S::NOTIFICATION:   enter(S::IDLE);   break;
    default: break;
  }
}

// states whose screens animate and must redraw every frame
static bool isAnimated(S s) {
  return s == S::RECORDING || s == S::DELETE_CONFIRM || s == S::SYNCING;
}

// ----------------------------------------------------------------- arduino
void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  M5.Display.setBrightness(160);
  M5.BtnA.setHoldThresh(150);   // push-to-talk threshold (design §4)
  M5.BtnB.setHoldThresh(600);
  Serial.begin(115200);

  if (!LittleFS.begin(true)) {  // format on first boot if unmounted
    Serial.println("[fs] LittleFS mount FAILED");
  } else {
    LittleFS.mkdir("/rec");
    Serial.printf("[fs] LittleFS ok, total=%u used=%u\n",
                  (unsigned)LittleFS.totalBytes(), (unsigned)LittleFS.usedBytes());
  }
  prefs.begin("vrec", false);
  clientSeq = prefs.getUInt("seq", 0);
  pendingSync = countPending();

  wifiBegin();

  Serial.printf("[StickS3] voice-recorder Step 6 up, seq=%u pending=%d\n", clientSeq, pendingSync);
  enter(S::IDLE);
}

void loop() {
  M5.update();
  if (state == S::IDLE) {                        // refresh status shown on the idle screen
    bool w = wifiUp();
    int b = M5.Power.getBatteryLevel();
    if (w != wifiOk || b != battPct) { wifiOk = w; battPct = b; dirty = true; }
  }
  if (state == S::RECORDING) pollRecording();   // drain mic before release check
  handleButtons();
  handleTimeouts();
  if (dirty || isAnimated(state)) {
    render();
    dirty = false;
  }
  delay(16);  // ~60 fps cap
}
