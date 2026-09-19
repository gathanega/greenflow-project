/*
 * GreenFlow — ESP32-C6-VROOM-1 traffic-light controller
 * ------------------------------------------------------
 * Polls the VPS for the latest computed light program (red/yellow/green
 * durations in seconds, matching the [CONTOHFORMATLALULINTAS] format),
 * then runs the RED -> GREEN -> YELLOW -> RED cycle locally against the
 * 5V LED traffic light module, so the intersection keeps cycling even if
 * WiFi/VPS is briefly unreachable (it falls back to the last known good
 * program, then to a hard-coded default).
 *
 * Board: ESP32-C6-VROOM-1
 * Module: LED Traffic Light Module 5V (3 channels: RED / YELLOW / GREEN)
 *
 * Wiring (adjust to your module — most 5V traffic-light modules take a
 * simple HIGH/LOW logic signal per channel, optionally through a
 * transistor/relay if the module draws more current than a GPIO can
 * source):
 *   RED    -> GPIO 4
 *   YELLOW -> GPIO 5
 *   GREEN  -> GPIO 6
 *   GND    -> GND (common ground with the ESP32-C6)
 *   5V     -> external 5V supply (do NOT power the LED module from the
 *             ESP32's 3V3 pin)
 *
 * Arduino IDE setup:
 *   - Boards manager: "esp32" by Espressif Systems (>= 3.0.0, needed for C6 support)
 *   - Board: "ESP32C6 Dev Module"
 *   - Library: "ArduinoJson" (install via Library Manager)
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---------------------- USER CONFIG ----------------------------------
const char* WIFI_SSID     = "ISI_NAMA_WIFI";
const char* WIFI_PASSWORD = "ISI_PASSWORD_WIFI";

const char* SERVER_HOST   = "your-vps-domain-or-ip";
const int   SERVER_PORT   = 8000;
const char* LOCATION_NAME = "Simpang Jl. Merdeka - Jl. Sudirman"; // must match the location used by the camera/CCTV feed for this intersection

const uint32_t POLL_INTERVAL_MS = 15000; // re-check the VPS every 15s (only applied at the *next* full cycle, so lights never jump mid-phase)

const int PIN_RED    = 4;
const int PIN_YELLOW = 5;
const int PIN_GREEN  = 6;

// Fallback / default program == [CONTOHFORMATLALULINTAS] baseline, used
// when the VPS has never been reached yet.
uint32_t redSec    = 180; // 03:00
uint32_t yellowSec = 30;  // 00:30
uint32_t greenSec  = 420; // 07:00
// -----------------------------------------------------------------------

uint32_t lastPollMs = 0;
uint32_t pendingRed = redSec, pendingYellow = yellowSec, pendingGreen = greenSec;

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(400);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? "\n[wifi] connected" : "\n[wifi] timeout, will retry");
}

// Fetches the latest program from the VPS into pendingRed/Yellow/Green.
// Does NOT apply it immediately — applied at the top of the next full
// cycle in loop(), so a mid-phase update never cuts a green phase short.
bool fetchLatestProgram() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    if (WiFi.status() != WL_CONNECTED) return false;
  }

  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/light/latest?location=" + LOCATION_NAME;
  http.begin(url);
  http.setTimeout(6000);

  int code = http.GET();
  if (code != 200) {
    Serial.printf("[poll] HTTP %d\n", code);
    http.end();
    return false;
  }

  String body = http.getString();
  http.end();

  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.printf("[poll] JSON parse error: %s\n", err.c_str());
    return false;
  }

  pendingRed    = doc["red_seconds"]    | redSec;
  pendingYellow = doc["yellow_seconds"] | yellowSec;
  pendingGreen  = doc["green_seconds"]  | greenSec;

  Serial.printf("[poll] new program -> R:%us Y:%us G:%us (status=%s)\n",
                pendingRed, pendingYellow, pendingGreen,
                doc["traffic_status"] | "?");
  return true;
}

void setLights(bool r, bool y, bool g) {
  digitalWrite(PIN_RED, r ? HIGH : LOW);
  digitalWrite(PIN_YELLOW, y ? HIGH : LOW);
  digitalWrite(PIN_GREEN, g ? HIGH : LOW);
}

// Blocks for `seconds`, but keeps polling for the next program in the
// background at POLL_INTERVAL_MS granularity so we never miss an update.
void holdPhase(uint32_t seconds) {
  uint32_t end = millis() + seconds * 1000UL;
  while ((int32_t)(end - millis()) > 0) {
    if (millis() - lastPollMs >= POLL_INTERVAL_MS) {
      fetchLatestProgram();
      lastPollMs = millis();
    }
    delay(200);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_YELLOW, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  setLights(true, false, false); // safe default: start on red

  connectWiFi();
  fetchLatestProgram();
  lastPollMs = millis();
}

void loop() {
  // Apply whatever program was fetched most recently, at the start of
  // this fresh cycle.
  redSec = pendingRed;
  yellowSec = pendingYellow;
  greenSec = pendingGreen;

  setLights(true, false, false);
  holdPhase(redSec);

  setLights(false, false, true);
  holdPhase(greenSec);

  setLights(false, true, false);
  holdPhase(yellowSec);
}
