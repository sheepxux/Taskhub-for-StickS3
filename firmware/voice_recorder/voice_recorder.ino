/*
 * StickS3 voice-recorder — Step 4 firmware skeleton.
 *
 * State machine + 6 screens + button handling, per voice-recorder-design.md
 * §4 §5 §6. NO recording, NO Wi-Fi yet — entries/notifications are fake, so the
 * whole UI and every state transition can be exercised on-device.
 *
 * Acceptance (Step 4): without recording, navigate all states and the screen
 * renders correctly.
 *
 * Board: M5Stack ESP32-S3 stick (M5Unified auto-detects the panel).
 * Lib:   M5Unified (pulls in M5GFX). Chinese glyphs via fonts::efontCN_*.
 */
#include <M5Unified.h>
#include <math.h>
#include <string.h>

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

  char dur[8];
  snprintf(dur, sizeof(dur), "0:%02u", (t / 1000) % 60);
  centerText(dur, 60, C_WHITE, &fonts::Font4);

  // 17 volume bars (fake jitter)
  int bars = 17, bw = 8, gap = 4, totalW = bars * bw + (bars - 1) * gap;
  int x0 = (W - totalW) / 2;
  for (int i = 0; i < bars; i++) {
    int h = 4 + (esp_random() % 18);
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
  centerText("已记 0:12", 96, C_WHITE, &fonts::efontCN_16);
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
static void handleButtons() {
  switch (state) {
    case S::SLEEP:
      if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
        M5.Display.wakeup();
        enter(unreadNotif ? S::NOTIFICATION : S::IDLE);  // pull-on-wake (design §6)
      }
      break;

    case S::IDLE:
      if (M5.BtnA.wasPressed()) { recordStart = millis(); enter(S::RECORDING); }
      else if (M5.BtnB.wasHold()) { enter(S::SYNCING); }
      else if (M5.BtnB.wasClicked()) { if (todayCount > 0) { reviewIdx = 0; enter(S::REVIEW); } }
      break;

    case S::RECORDING:
      if (M5.BtnA.wasReleased()) {
        uint32_t held = millis() - recordStart;
        if (held >= 150) {            // saved
          entryCount++; todayCount++; pendingSync++;
          enter(S::SAVED);
        } else {                       // too short → discard
          enter(S::IDLE);
        }
      }
      break;

    case S::REVIEW:
      if (M5.BtnB.wasHold()) { enter(S::IDLE); }
      else if (M5.BtnB.wasDoubleClicked()) { enter(S::DELETE_CONFIRM); }
      else if (M5.BtnB.wasClicked()) { reviewIdx = (reviewIdx + 1) % entryCount; dirty = true; }
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
    case S::SAVED:          enter(S::IDLE);   break;
    case S::REVIEW:         enter(S::IDLE);   break;
    case S::DELETE_CONFIRM: enter(S::REVIEW); break;
    case S::NOTIFICATION:   enter(S::IDLE);   break;
    case S::SYNCING:        pendingSync = 0; enter(S::IDLE); break;
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
  Serial.println("[StickS3] voice-recorder Step 4 skeleton up");
  enter(S::IDLE);
}

void loop() {
  M5.update();
  handleButtons();
  handleTimeouts();
  if (dirty || isAnimated(state)) {
    render();
    dirty = false;
  }
  delay(16);  // ~60 fps cap
}
