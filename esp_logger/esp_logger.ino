/*
 * esp_logger.ino
 * ESP8266 NodeMCU — Логер лінії з TCP-відправкою
 *
 * Залежності (встановити через Arduino Library Manager):
 *   - ESP8266WiFi        (вбудована в ESP8266 core)
 *   - WiFiManager        by tzapu   >= 2.0.17
 *   - ArduinoJson        by Benoit Blanchon >= 7.x
 *   - NTPClient          by Fabrice Weinberg >= 3.2.1
 *
 * Плата: NodeMCU 1.0 (ESP-12E Module)
 * GPIO:  D5 (GPIO14) ← dry contact / reed switch (INPUT_PULLUP, inverted)
 *
 * Формат JSON → server.py:
 *   {"type":"line_start","ts":1710000000,"cycle":42,"buffered":true,"device":"esp-logger-scl"}
 *   {"type":"line_stop", "ts":1710000060,"cycle":42,"dur":60.0,"buffered":false,"device":"esp-logger-scl"}
 *   {"type":"heartbeat","ts":1710000120,"uptime":3600,"buf":0,"device":"esp-logger-scl"}
 *   {"type":"boot",     "ts":1710000000,"buf_after_reboot":3,"device":"esp-logger-scl"}
 */

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiManager.h>
#include <ArduinoJson.h>
#include <NTPClient.h>
#include <WiFiUdp.h>
#include <EEPROM.h>

// Forward declarations
void handleLineChange(bool newState);
void syncOneEvent();

// ============================================================
// Налаштування — змінюй тут
// ============================================================
#define DEVICE_ID        "esp-logger-test"
char SERVER_HOST[40] = "192.168.31.38";   // IP ПК з server.py (може змінюватись через WiFiManager)
#define SERVER_PORT      5555
#define GPIO_LINE        D5                // GPIO14
#define DEBOUNCE_MS      200
#define SYNC_INTERVAL_MS 10000UL           // 10 с між відправками буфера
#define HEARTBEAT_MS     60000UL           // 1 хв
#define SPEED_UPDATE_MS  5000UL            // 5 с оновлення швидкості (тільки коли лінія працює)
#define RECONNECT_MS     5000UL            // затримка між спробами TCP
#define BUFFER_SIZE      10
#define EEPROM_SIZE      512

// ============================================================
// EEPROM Layout
// Зберігаємо circular buffer і метадані
// ============================================================
#define EEPROM_MAGIC        0xA5           // маркер валідності
#define ADDR_MAGIC          0              // 1 байт
#define ADDR_BUF_COUNT      1              // 1 байт
#define ADDR_BUF_INDEX      2              // 1 байт
#define ADDR_OVERFLOW       3              // 2 байти (uint16)
#define ADDR_CYCLE_COUNT    5              // 4 байти (uint32)
#define ADDR_LAST_TS        9              // 4 байти
#define ADDR_LAST_STATE     13             // 1 байт
#define ADDR_LINE_STATE_GPIO 14           // 1 байт — останній відомий стан GPIO
#define ADDR_EVENTS_START   20             // 10 × 9 байт = 90 байт

// Структура одного запису в буфері (9 байт)
struct BufEvent {
    uint32_t ts;       // Unix timestamp
    bool     state;    // true = start, false = stop
    float    dur;      // тривалість (тільки для stop)
};  // sizeof = 9 байт

// ============================================================
// Globals
// ============================================================
WiFiUDP       ntpUDP;
NTPClient     timeClient(ntpUDP, "pool.ntp.org", 10800, 60000); // UTC+3 Kyiv

WiFiClient    tcpClient;
WiFiManager   wifiManager;

// Стан GPIO
volatile bool lineStateRaw  = false;
bool          lineState      = false;
unsigned long lastDebounceMs = 0;

// Таймери
unsigned long lastSyncMs      = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long lastReconnectMs = 0;
unsigned long lastSpeedUpdateMs = 0;  // таймер оновлення швидкості

// Буфер (RAM-дзеркало EEPROM)
BufEvent ramBuf[BUFFER_SIZE];
int      bufCount    = 0;
int      bufIndex    = 0;
uint16_t bufOverflow = 0;
uint32_t cycleCount  = 0;

// Дедуплікація
uint32_t lastBufferedTs    = 0;
bool     lastBufferedState = false;

// Boot state change flag
bool bootStateChanged = false;

// Поточний цикл
uint32_t lineStartTs = 0;

// Останній відомий стан лінії (збережений в EEPROM)
bool lastKnownLineState = false;

// ============================================================
// EEPROM helpers
// ============================================================
void eepromWriteUint32(int addr, uint32_t val) {
    EEPROM.put(addr, val);
}
uint32_t eepromReadUint32(int addr) {
    uint32_t val;
    EEPROM.get(addr, val);
    return val;
}
void eepromWriteEvent(int slot, const BufEvent& e) {
    int addr = ADDR_EVENTS_START + slot * sizeof(BufEvent);
    EEPROM.put(addr, e);
}
BufEvent eepromReadEvent(int slot) {
    BufEvent e;
    int addr = ADDR_EVENTS_START + slot * sizeof(BufEvent);
    EEPROM.get(addr, e);
    return e;
}
void eepromSaveState() {
    EEPROM.write(ADDR_MAGIC,    EEPROM_MAGIC);
    EEPROM.write(ADDR_BUF_COUNT, (uint8_t)bufCount);
    EEPROM.write(ADDR_BUF_INDEX, (uint8_t)bufIndex);
    EEPROM.put(ADDR_OVERFLOW,   bufOverflow);
    EEPROM.put(ADDR_CYCLE_COUNT, cycleCount);
    EEPROM.put(ADDR_LAST_TS,    lastBufferedTs);
    EEPROM.write(ADDR_LAST_STATE, lastBufferedState ? 1 : 0);
    EEPROM.write(ADDR_LINE_STATE_GPIO, lineState ? 1 : 0);
    EEPROM.commit();
}
void eepromLoad() {
    if (EEPROM.read(ADDR_MAGIC) != EEPROM_MAGIC) {
        Serial.println("[EEPROM] Перший запуск — очищуємо");
        bufCount = 0; bufIndex = 0; bufOverflow = 0; cycleCount = 0;
        lastBufferedTs = 0; lastBufferedState = false;
        eepromSaveState();
        return;
    }
    bufCount         = EEPROM.read(ADDR_BUF_COUNT);
    bufIndex         = EEPROM.read(ADDR_BUF_INDEX);
    EEPROM.get(ADDR_OVERFLOW,    bufOverflow);
    EEPROM.get(ADDR_CYCLE_COUNT, cycleCount);
    EEPROM.get(ADDR_LAST_TS,     lastBufferedTs);
    lastBufferedState   = EEPROM.read(ADDR_LAST_STATE) != 0;
    lastKnownLineState  = EEPROM.read(ADDR_LINE_STATE_GPIO) != 0;
    for (int i = 0; i < BUFFER_SIZE; i++) ramBuf[i] = eepromReadEvent(i);
    Serial.printf("[EEPROM] Завантажено: buf=%d idx=%d overflow=%d cycle=%u\n",
        bufCount, bufIndex, bufOverflow, cycleCount);
}

// ============================================================
// Buffer
// ============================================================
void bufferPush(uint32_t ts, bool state, float dur) {
    // Дедуплікація
    if (ts == lastBufferedTs && state == lastBufferedState) {
        Serial.printf("[DEDUP] Дублікат проігноровано ts=%u state=%s\n",
            ts, state ? "start" : "stop");
        return;
    }

    int slot = bufIndex % BUFFER_SIZE;
    ramBuf[slot] = {ts, state, dur};
    eepromWriteEvent(slot, ramBuf[slot]);

    bufIndex = (bufIndex + 1) % BUFFER_SIZE;
    if (bufCount < BUFFER_SIZE) {
        bufCount++;
    } else {
        bufOverflow++;
        Serial.printf("[BUF] Переповнення! Перезаписую слот %d\n", slot);
    }

    lastBufferedTs    = ts;
    lastBufferedState = state;

    eepromSaveState();
    Serial.printf("[BUF] Записано [%d]: ts=%u state=%s dur=%.1f | у буфері: %d/10\n",
        slot, ts, state ? "start" : "stop", dur, bufCount);
}

// Повертає найстаріший запис з FIFO (без видалення)
BufEvent bufferPeek() {
    // oldest slot = (bufIndex - bufCount + BUFFER_SIZE) % BUFFER_SIZE
    int oldestSlot = ((bufIndex - bufCount) % BUFFER_SIZE + BUFFER_SIZE) % BUFFER_SIZE;
    return ramBuf[oldestSlot];
}

void bufferPop() {
    if (bufCount > 0) {
        bufCount--;
        eepromSaveState();
    }
}

// ============================================================
// TCP
// ============================================================
bool tcpConnect() {
    if (tcpClient.connected()) return true;
    unsigned long now = millis();
    if (now - lastReconnectMs < RECONNECT_MS) return false;
    lastReconnectMs = now;

    Serial.printf("[TCP] Підключення до %s:%d...\n", SERVER_HOST, SERVER_PORT);
    if (tcpClient.connect(SERVER_HOST, SERVER_PORT)) {
        Serial.println("[TCP] Підключено!");
        return true;
    }
    Serial.println("[TCP] Не вдалося підключитися.");
    return false;
}

bool tcpSendJson(const JsonDocument& doc) {
    if (!tcpConnect()) return false;
    String payload;
    serializeJson(doc, payload);
    payload += "\n";
    size_t written = tcpClient.print(payload);
    if (written == 0) {
        Serial.println("[TCP] Помилка надсилання — розриваємо з'єднання");
        tcpClient.stop();
        return false;
    }
    Serial.printf("[TCP] → %s", payload.c_str());
    return true;
}

// ============================================================
// Відправка події з буфера
// ============================================================
void syncOneEvent() {
    if (bufCount == 0) return;
    if (!tcpConnect())  return;

    BufEvent e = bufferPeek();
    JsonDocument doc;
    doc["device"]   = DEVICE_ID;
    doc["ts"]       = e.ts;
    // buffered=true означає, що ми відправляємо "з накопичення" (backlog),
    // а не миттєву подію, яка просто була тимчасово в буфері.
    doc["buffered"] = (bufCount > 1);

    if (e.state) {
        doc["type"]  = "line_start";
        doc["cycle"] = cycleCount;
    } else {
        doc["type"]  = "line_stop";
        doc["cycle"] = cycleCount;
        doc["dur"]   = serialized(String(e.dur, 1));
    }

    if (tcpSendJson(doc)) {
        bufferPop();
        Serial.printf("[SYNC] Відправлено, залишилось у буфері: %d\n", bufCount);
    }
}

// sendLiveEvent() — видалено (використовується buffer-first через syncOneEvent)

// Heartbeat
void sendHeartbeat() {
    if (!tcpConnect()) return;

    JsonDocument doc;
    doc["type"]   = "heartbeat";
    doc["device"] = DEVICE_ID;
    doc["ts"]     = (uint32_t)timeClient.getEpochTime();
    doc["uptime"] = millis() / 1000;
    doc["buf"]    = bufCount;
    doc["rssi"]   = WiFi.RSSI();

    tcpSendJson(doc);
}

// Speed update (тільки коли лінія працює)
void sendSpeedUpdate(float speed) {
    if (!tcpConnect()) return;

    JsonDocument doc;
    doc["type"]   = "speed_update";
    doc["device"] = DEVICE_ID;
    doc["ts"]     = (uint32_t)timeClient.getEpochTime();
    doc["speed"]  = serialized(String(speed, 2));
    doc["cycle"]  = cycleCount;

    tcpSendJson(doc);
    Serial.printf("[SPEED] Оновлення: %.2f цикл=#%u\n", speed, cycleCount);
}

// Boot-подія
void sendBoot(int bufAfterReboot) {
    if (!tcpConnect()) return;

    JsonDocument doc;
    doc["type"]             = "boot";
    doc["device"]           = DEVICE_ID;
    doc["ts"]               = (uint32_t)timeClient.getEpochTime();
    doc["buf_after_reboot"] = bufAfterReboot;
    doc["version"]          = "3.0-tcp";

    tcpSendJson(doc);
}

// ============================================================
// GPIO interrupt (IRAM_ATTR — виконується з RAM)
// ============================================================
IRAM_ATTR void onLineChange() {
    lineStateRaw = (digitalRead(GPIO_LINE) == LOW); // inverted: LOW = contact closed
    lastDebounceMs = millis();
}

// ============================================================
// NTP
// ============================================================
uint32_t getTimestamp() {
    uint32_t t = (uint32_t)timeClient.getEpochTime();
    return (t > 1000000000UL) ? t : 0; // 0 якщо час не синхронізований
}

// ============================================================
// WiFiManager callback — викликається коли AP підняте
// ============================================================
void onWiFiManagerAP(WiFiManager* wm) {
    Serial.println("[WiFiManager] AP запущено: ESP-Logger-Setup");
    Serial.printf("[WiFiManager] IP: %s\n", WiFi.softAPIP().toString().c_str());
}

// ============================================================
// Setup
// ============================================================
void setup() {
    Serial.begin(115200);
    delay(100);
    Serial.println("\n\n=== ESP Logger v3.0 (TCP) ===");

    // EEPROM
    EEPROM.begin(EEPROM_SIZE);
    eepromLoad();
    // Санітарна перевірка після завантаження з EEPROM
    if (bufCount > BUFFER_SIZE) bufCount = 0;
    if (bufIndex >= BUFFER_SIZE) bufIndex = 0;

    int bufAtBoot = bufCount;
    Serial.printf("[Boot] Подій у буфері після ребуту: %d\n", bufAtBoot);

    // GPIO
    pinMode(GPIO_LINE, INPUT_PULLUP);
    bool currentGpioState = (digitalRead(GPIO_LINE) == LOW);
    lineState = currentGpioState;
    lineStateRaw = currentGpioState;
    attachInterrupt(digitalPinToInterrupt(GPIO_LINE), onLineChange, CHANGE);
    Serial.printf("[GPIO] D5 ініціалізовано, поточний стан: %s\n",
        lineState ? "ACTIVE" : "IDLE");
    Serial.printf("[GPIO] Збережений стан: %s\n",
        lastKnownLineState ? "ACTIVE" : "IDLE");

    // BOOT STATE CHECK:
    // Якщо поточний стан GPIO збігається з останнім збереженим —
    // нічого не сталося поки ESP був вимкнений, не тригеримо подію.
    // Якщо не збігається — стан змінився поки ESP був offline,
    // записуємо подію з поточним станом.
    if (currentGpioState != lastKnownLineState) {
        Serial.printf("[Boot] Стан змінився під час offline: %s → %s\n",
            lastKnownLineState ? "ACTIVE" : "IDLE",
            currentGpioState   ? "ACTIVE" : "IDLE");
        bootStateChanged = true;
    } else {
        Serial.println("[Boot] Стан не змінився — подія не потрібна");
    }

    // WiFiManager
    wifiManager.setAPCallback(onWiFiManagerAP);
    wifiManager.setConfigPortalTimeout(90);    // 90 с на налаштування, потім ребут
    wifiManager.setMinimumSignalQuality(10);
    wifiManager.setConnectTimeout(20);

    // Додаємо власне поле — IP сервера
    WiFiManagerParameter paramServerIP("server_ip", "IP сервера (ПК)",
                                        SERVER_HOST, 40);
    WiFiManagerParameter paramServerPort("server_port", "Порт сервера",
                                          String(SERVER_PORT).c_str(), 6);
    wifiManager.addParameter(&paramServerIP);
    wifiManager.addParameter(&paramServerPort);
    
    // Отримуємо значення з форми
    const char* serverIpValue = paramServerIP.getValue();
    const char* serverPortValue = paramServerPort.getValue();
    
    if (serverIpValue && strlen(serverIpValue) > 0) {
        strncpy((char*)SERVER_HOST, serverIpValue, 40);
        Serial.printf("[WiFiManager] Оновлено IP сервера: %s\n", serverIpValue);
    }
    if (serverPortValue && strlen(serverPortValue) > 0) {
        int newPort = atoi(serverPortValue);
        if (newPort > 0 && newPort <= 65535) {
            // SERVER_PORT є const, тому просто логіруємо
            Serial.printf("[WiFiManager] Порт сервера з форми: %s (поточний: %d)\n", 
                          serverPortValue, SERVER_PORT);
        }
    }

    if (!wifiManager.autoConnect("ESP-Logger-Setup")) {
        Serial.println("[WiFi] Не підключено — ребут");
        delay(3000);
        ESP.restart();
    }

    Serial.printf("[WiFi] Підключено! IP: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("[WiFi] RSSI: %d dBm\n", WiFi.RSSI());

    // NTP
    timeClient.begin();
    Serial.print("[NTP] Синхронізація...");
    for (int i = 0; i < 10; i++) {
        if (timeClient.update()) break;
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[NTP] Час: %s (ts=%u)\n",
        timeClient.getFormattedTime().c_str(),
        (uint32_t)timeClient.getEpochTime());

    // Boot-подія (після підключення до сервера)
    delay(500);
    sendBoot(bufAtBoot);

    // Якщо стан лінії змінився поки ESP був offline — тепер тригеримо
    if (bootStateChanged) {
        Serial.println("[Boot] Відправляємо подію зміни стану після ребуту");
        handleLineChange(lineState);
    }
}

// ============================================================
// Loop
// ============================================================
void loop() {
    unsigned long now = millis();

    // ── NTP підтримка ──────────────────────────────────────
    timeClient.update();

    // ── Debounce GPIO ──────────────────────────────────────
    if (now - lastDebounceMs >= DEBOUNCE_MS) {
        bool stable = lineStateRaw;
        if (stable != lineState) {
            lineState = stable;
            handleLineChange(lineState);
        }
    }

    // ── Sync буфера (FIFO, 1 подія за інтервал) ───────────
    if (now - lastSyncMs >= SYNC_INTERVAL_MS) {
        lastSyncMs = now;
        syncOneEvent();
    }

    // ── Heartbeat ──────────────────────────────────────────
    if (now - lastHeartbeatMs >= HEARTBEAT_MS) {
        lastHeartbeatMs = now;
        sendHeartbeat();
    }

    // ── Speed update (тільки коли лінія працює) ───────────
    if (lineState && now - lastSpeedUpdateMs >= SPEED_UPDATE_MS) {
        lastSpeedUpdateMs = now;
        // Тут має бути логіка отримання актуальної швидкості
        // Наприклад, з аналізатора струму або датчика швидкості
        // Для прикладу генеруємо випадкове значення
        float currentSpeed = 1.5 + random(0, 100) / 100.0; // 1.5-2.5
        sendSpeedUpdate(currentSpeed);
    }

    // ── WiFi watchdog ──────────────────────────────────────
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] Втрачено з'єднання, ребут через 10с...");
        delay(10000);
        ESP.restart();
    }
}

// ============================================================
// Обробка зміни стану лінії
// ============================================================
void handleLineChange(bool newState) {
    uint32_t ts = getTimestamp();
    if (ts == 0) {
        // Час не синхронізований — використовуємо uptime як fallback
        ts = millis() / 1000;
        Serial.println("[WARN] NTP не синхронізовано, використовую uptime як ts");
    }

    float dur = 0.0f;

    if (newState) {
        // Лінія запущена
        lineStartTs = ts;
        cycleCount++;
        Serial.printf("[LINE] START ts=%u цикл=#%u\n", ts, cycleCount);
    } else {
        // Лінія зупинена
        if (lineStartTs > 0) {
            dur = (float)(ts - lineStartTs);
        }
        Serial.printf("[LINE] STOP ts=%u тривалість=%.1f с\n", ts, dur);
    }

    // Buffer-first: завжди пишемо в буфер
    bufferPush(ts, newState, dur);

    // Якщо TCP доступний — відразу намагаємося відправити
    // (синк-інтервал також підчистить якщо не вдалося)
    if (tcpClient.connected()) {
        syncOneEvent();
    }
}
