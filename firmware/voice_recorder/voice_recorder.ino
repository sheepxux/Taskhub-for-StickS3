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
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>
#include <driver/rtc_io.h>
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

// ----------------------------------------------------------------- entries (from server)
// REVIEW shows today's real entries pulled from GET /api/v1/entries/today;
// titles/previews are whisper/LLM output that lives on the server, not here.
struct Entry { String time, title, preview, id, tag; };
static constexpr int MAX_ENTRIES = 24;
static Entry entries[MAX_ENTRIES];
static int  entryCount = 0;
static int  todayCount = 0;        // shown on IDLE (== fetched count)
static int  pendingSync = 0;       // local /rec/*.wav not yet uploaded
static bool wifiOk = false;
static int  battPct = 100;

static bool unreadNotif = true;   // one unread daily summary
static const char* notifTitle = "今天记了 7 条";
static const char* notifBody  = "产品点子(3) · 阅读笔记(2) · 待办(2)。详情已推送到 Telegram。";

// ----------------------------------------------------------------- runtime
static S        state = S::IDLE;
static uint32_t stateSince = 0;   // millis when current state entered
static bool     dirty = true;     // needs full redraw
static uint32_t recordStart = 0;  // RECORDING press time
static int      reviewIdx = 0;    // REVIEW cursor
static uint32_t reviewFetchAt = 0; // last fetchToday() while in REVIEW (auto-refresh throttle)
// Sticky-note "card on a board": a smaller card centred on a black board, with
// dot indicators below. Two card-sized sprites animate old-out / new-in.
static constexpr int CARD_W = 196, CARD_H = 92, CARD_X = 22, CARD_Y = 14;
static M5Canvas cardA(&M5.Display);   // outgoing card
static M5Canvas cardB(&M5.Display);   // incoming card
static bool     cardReady = false;

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

static String apiUrl(const String& path) {
  return String("http://") + SERVER_HOST + ":" + String((int)SERVER_PORT) + path;
}

// Pull today's entries from the server into entries[]/todayCount.
static void fetchToday() {
  if (!wifiUp()) return;
  HTTPClient http;
  http.begin(apiUrl("/api/v1/entries/today"));
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.GET();
  if (code == 200) {
    JsonDocument doc;
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok) {
      entryCount = 0;
      for (JsonObject o : doc.as<JsonArray>()) {
        if (entryCount >= MAX_ENTRIES) break;
        Entry& e = entries[entryCount];
        e.id      = o["id"].as<String>();
        e.title   = o["title"].as<String>();
        e.preview = o["preview"].as<String>();
        e.tag     = o["tag"].as<String>();
        String ts = o["recorded_at"].as<String>();   // 2026-05-28T23:04:51+08:00 → 23:04
        int t = ts.indexOf('T');
        e.time = (t >= 0 && (int)ts.length() >= t + 6) ? ts.substring(t + 1, t + 6) : ts;
        entryCount++;
      }
      todayCount = entryCount;
    }
  }
  http.end();
  Serial.printf("[net] fetchToday code=%d count=%d\n", code, entryCount);
}

static bool deleteEntry(const String& id) {
  if (!wifiUp() || id.length() == 0) return false;
  HTTPClient http;
  http.begin(apiUrl("/api/v1/entries/" + id));
  http.addHeader("X-Device-Token", DEVICE_TOKEN);
  int code = http.sendRequest("DELETE");
  http.end();
  Serial.printf("[net] delete %s code=%d\n", id.c_str(), code);
  return code == 200;
}

// ----------------------------------------------------------------- draw helpers
static void clearBg() { M5.Display.fillScreen(C_BG); }

static void centerText(const char* s, int y, int color, const lgfx::IFont* font) {
  M5.Display.setFont(font);
  M5.Display.setTextColor(color, C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString(s, M5.Display.width() / 2, y);
}

// Sticky-note category → colour (matches the multi-colour state encoding §5).
static int tagColor(const String& tag) {
  if (tag == "待办")   return C_AMBER;
  if (tag == "产品点子") return C_BLUE;
  if (tag == "阅读笔记") return C_GREEN;
  return C_GRAY;  // 其他
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

// Render entry[idx] as a colour-coded sticky note into the given card sprite.
static void renderCard(M5Canvas& spr, int idx) {
  Entry& e = entries[idx];
  int col = tagColor(e.tag);
  spr.fillScreen(C_BG);                          // black corners blend into the board
  spr.fillRoundRect(0, 0, CARD_W, CARD_H, 10, col);
  spr.setTextColor(C_BG, col);                   // dark ink on the colour

  spr.setFont(&fonts::efontCN_12);
  spr.setTextDatum(top_left);
  spr.drawString(e.tag, 10, 8);
  spr.setTextDatum(top_right);
  spr.drawString(e.time, CARD_W - 10, 8);
  spr.drawFastHLine(10, 26, CARD_W - 20, C_BG);

  spr.setFont(&fonts::efontCN_16);
  spr.setTextDatum(top_left);
  spr.drawString(e.title, 10, 32);

  spr.setFont(&fonts::efontCN_12);
  spr.setTextWrap(true);
  spr.setCursor(10, 56);
  spr.print(e.preview);
  spr.setTextWrap(false);
}

// Black board + position dots (current filled). Many entries → "i / n" text.
static void drawBoard(int idx) {
  const int W = M5.Display.width();
  M5.Display.fillScreen(C_BG);
  int dy = CARD_Y + CARD_H + 12;
  if (entryCount <= 8) {
    int gap = 14, totalW = (entryCount - 1) * gap;
    int x0 = (W - totalW) / 2;
    for (int i = 0; i < entryCount; i++)
      M5.Display.fillCircle(x0 + i * gap, dy, i == idx ? 4 : 2, i == idx ? C_WHITE : C_GRAY);
  } else {
    char s[16]; snprintf(s, sizeof(s), "%d / %d", idx + 1, entryCount);
    centerText(s, dy, C_GRAY, &fonts::efontCN_12);
  }
}

static void drawReview() {
  if (entryCount == 0 || !cardReady) {
    clearBg();
    centerText("今天还没有记录", M5.Display.height() / 2, C_GRAY, &fonts::efontCN_16);
    centerText("双击返回", 128, C_GRAY, &fonts::efontCN_12);
    return;
  }
  if (reviewIdx >= entryCount) reviewIdx = entryCount - 1;
  drawBoard(reviewIdx);
  renderCard(cardA, reviewIdx);
  cardA.pushSprite(CARD_X, CARD_Y);
}

// Slide from current to newIdx: dir>0 old exits left / new enters right (and vice versa).
static void slideTo(int newIdx, int dir) {
  stateSince = millis();                  // keep REVIEW awake while browsing
  if (!cardReady || entryCount == 0) { reviewIdx = newIdx; drawReview(); return; }
  const int W = M5.Display.width();
  renderCard(cardA, reviewIdx);           // outgoing
  renderCard(cardB, newIdx);              // incoming
  const int steps = 8;
  for (int i = 1; i <= steps; i++) {
    float p = (float)i / steps;
    int oldX = CARD_X - (int)(dir * W * p);
    int newX = CARD_X + (int)(dir * W * (1.0f - p));
    M5.Display.fillRect(0, CARD_Y, W, CARD_H, C_BG);   // clear only the card band
    cardA.pushSprite(oldX, CARD_Y);
    cardB.pushSprite(newX, CARD_Y);
    M5.delay(14);
  }
  reviewIdx = newIdx;
  drawReview();                           // settle: board + dots + final card
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
  fetchToday();              // refresh counts/list with what the server now has
  enter(S::IDLE);
}

// StickS3 buttons (active-low). Both are RTC-capable GPIOs on the ESP32-S3, so
// either can wake the chip from deep sleep via ext1.
static constexpr gpio_num_t PIN_BTN_A = GPIO_NUM_11;
static constexpr gpio_num_t PIN_BTN_B = GPIO_NUM_12;

// Real deep sleep (design §6): ~50µA vs always-on. Wakes on BtnA/BtnB press, or
// an hourly timer (NTP + notification pull). Wake = full reboot → setup() runs,
// so there is no "SLEEP" runtime state to return to. RESET always recovers.
static void enterDeepSleep() {
  Serial.println("[pwr] deep sleep");
  Serial.flush();
  M5.Display.sleep();                       // panel + backlight off
  const uint64_t mask = (1ULL << PIN_BTN_A) | (1ULL << PIN_BTN_B);
  esp_sleep_enable_ext1_wakeup_io(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  rtc_gpio_pullup_en(PIN_BTN_A);  rtc_gpio_pulldown_dis(PIN_BTN_A);  // idle HIGH, wake on LOW
  rtc_gpio_pullup_en(PIN_BTN_B);  rtc_gpio_pulldown_dis(PIN_BTN_B);
  esp_sleep_enable_timer_wakeup(3600ULL * 1000000ULL);  // hourly safety + pull
  esp_deep_sleep_start();                    // never returns
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
      else if (M5.BtnB.wasClicked()) { fetchToday(); reviewFetchAt = millis(); if (entryCount > 0) { reviewIdx = 0; enter(S::REVIEW); } }
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
      // BtnA is ALWAYS record — pressing it here jumps straight into recording.
      // BtnB: single = next (slide), double = return, hold = delete.
      if (M5.BtnA.wasPressed()) { recordStart = millis(); startRecording(); enter(S::RECORDING); }
      else if (M5.BtnB.wasHold()) { enter(S::DELETE_CONFIRM); }
      else if (M5.BtnB.wasDoubleClicked()) { enter(S::IDLE); }
      else if (M5.BtnB.wasSingleClicked()) { slideTo((reviewIdx + 1) % entryCount, +1); }
      break;

    case S::DELETE_CONFIRM:
      if (M5.BtnB.wasClicked()) {                  // a tap confirms; 2s timeout cancels
        if (reviewIdx < entryCount) deleteEntry(entries[reviewIdx].id);
        fetchToday();                              // re-pull authoritative list
        if (reviewIdx >= entryCount) reviewIdx = entryCount > 0 ? entryCount - 1 : 0;
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
    case S::IDLE:           enterDeepSleep(); break;   // real deep sleep; wakes via reboot
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
  // BtnB hold-threshold doubles as the multi-click decision window (M5Unified):
  // 450ms gives a snappy hold-to-delete and a comfortable double-click-to-return.
  M5.BtnB.setHoldThresh(450);

  cardA.setPsram(true); cardA.setColorDepth(16);
  cardB.setPsram(true); cardB.setColorDepth(16);
  cardReady = cardA.createSprite(CARD_W, CARD_H) && cardB.createSprite(CARD_W, CARD_H);
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

  switch (esp_sleep_get_wakeup_cause()) {     // why did we (re)boot?
    case ESP_SLEEP_WAKEUP_EXT1:  Serial.println("[pwr] woke: button"); break;
    case ESP_SLEEP_WAKEUP_TIMER: Serial.println("[pwr] woke: hourly timer"); break;
    default:                     Serial.println("[pwr] cold boot"); break;
  }
  Serial.printf("[StickS3] voice-recorder Step 6 up, seq=%u pending=%d\n", clientSeq, pendingSync);
  enter(S::IDLE);
}

void loop() {
  M5.update();
  if (state == S::IDLE) {                        // refresh status shown on the idle screen
    static bool fetchedOnce = false;
    bool w = wifiUp();
    int b = M5.Power.getBatteryLevel();
    if (w != wifiOk || b != battPct) { wifiOk = w; battPct = b; dirty = true; }
    if (w && !fetchedOnce) { fetchedOnce = true; fetchToday(); dirty = true; }  // populate count once online
  }
  if (state == S::REVIEW && wifiUp() && millis() - reviewFetchAt > 2500) {
    reviewFetchAt = millis();       // live-refresh so "(转录中)" resolves to the real title in place
    fetchToday();
    dirty = true;
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
