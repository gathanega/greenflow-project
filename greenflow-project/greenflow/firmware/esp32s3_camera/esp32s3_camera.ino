/*
 * GreenFlow — ESP32-S3-VROOM-1 + OV3660 camera node
 * -------------------------------------------------
 * MODE A of the ingestion pipeline: this board captures a JPEG frame from
 * the OV3660 camera every CAPTURE_INTERVAL_MS and POSTs it to the VPS.
 * (MODE B — pulling frames from an existing online CCTV API instead of a
 * physical camera — is handled entirely on the server side in server/app.py,
 * so nothing on this board needs to change to switch modes; just don't
 * deploy this board for a location that uses Mode B.)
 *
 * Board:   ESP32-S3-VROOM-1 (needs PSRAM enabled — it's required to hold
 *          JPEG frame buffers at higher resolutions)
 * Camera:  OV3660
 *
 * Arduino IDE setup:
 *   - Boards manager: "esp32" by Espressif Systems (>= 3.0.0)
 *   - Board: "ESP32S3 Dev Module"
 *   - PSRAM: "OPI PSRAM" (or QSPI, depending on your module)
 *   - Partition scheme: "Huge APP (3MB No OTA/1MB SPIFFS)" or similar
 *   - Library: none extra needed — uses the bundled "esp_camera" driver
 *     that ships with the ESP32 Arduino core.
 *
 * Wiring: use the standard OV3660/OV2640 camera pin mapping for your
 * specific ESP32-S3 camera board (e.g. Freenove ESP32-S3-WROOM CAM, or
 * ESP32-S3-EYE). The pins below match the common "ESP32-S3-EYE"/generic
 * ESP32-S3 camera breakout pinout — CHECK YOUR BOARD'S SILKSCREEN/DATASHEET
 * and adjust the CAM_PIN_* defines if they differ.
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>

// ---------------------- USER CONFIG ----------------------------------
const char* WIFI_SSID     = "Wifihome";
const char* WIFI_PASSWORD = "tangankanantangankiri";

// VPS ingestion endpoint. device_id must be unique per camera/location and
// must match the device_id you register in supabase/schema.sql.
const char* SERVER_HOST   = "163.61.58.23";     // no http://
const int   SERVER_PORT   = 80;                        // FastAPI/uvicorn port (behind nginx use 80/443)
const char* DEVICE_ID     = "esp32-malang-1";
const char* LOCATION_NAME = "MalangESP32";

const uint32_t CAPTURE_INTERVAL_MS = 5000;  // send a frame every 5s
// -----------------------------------------------------------------------

// ---- OV3660 pin map (generic ESP32-S3 camera board) --------------------
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4
#define CAM_PIN_SIOC    5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13
// -------------------------------------------------------------------------

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = CAM_PIN_D0; config.pin_d1 = CAM_PIN_D1;
  config.pin_d2 = CAM_PIN_D2; config.pin_d3 = CAM_PIN_D3;
  config.pin_d4 = CAM_PIN_D4; config.pin_d5 = CAM_PIN_D5;
  config.pin_d6 = CAM_PIN_D6; config.pin_d7 = CAM_PIN_D7;
  config.pin_xclk = CAM_PIN_XCLK;
  config.pin_pclk = CAM_PIN_PCLK;
  config.pin_vsync = CAM_PIN_VSYNC;
  config.pin_href = CAM_PIN_HREF;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_pwdn = CAM_PIN_PWDN;
  config.pin_reset = CAM_PIN_RESET;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size = FRAMESIZE_SVGA;   // 800x600 — good balance for vehicle detection
    config.jpeg_quality = 12;             // lower = better quality, bigger file
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
  } else {
    config.frame_size = FRAMESIZE_VGA;
    config.jpeg_quality = 15;
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[camera] init failed: 0x%x\n", err);
    return false;
  }

  // OV3660-specific tweaks for better daylight/outdoor traffic footage
  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_vflip(s, 1);
    s->set_brightness(s, 1);
    s->set_saturation(s, 0);
  }
  return true;
}

void connectWiFi() {
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.printf("\n[wifi] connected, IP=%s\n", WiFi.localIP().toString().c_str());
}

bool sendFrame(camera_fb_t* fb) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT +
               "/api/ingest/esp32/" + DEVICE_ID +
               "?location=" + LOCATION_NAME;

  http.begin(url);
  http.addHeader("Content-Type", "image/jpeg");
  http.setTimeout(8000);

  int code = http.POST(fb->buf, fb->len);
  if (code > 0) {
    Serial.printf("[send] HTTP %d, %u bytes\n", code, fb->len);
  } else {
    Serial.printf("[send] failed: %s\n", http.errorToString(code).c_str());
  }
  http.end();
  return code == 200;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  if (!initCamera()) {
    Serial.println("[fatal] camera init failed, halting");
    while (true) delay(1000);
  }
  connectWiFi();
}

void loop() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[camera] frame capture failed");
    delay(1000);
    return;
  }

  sendFrame(fb);
  esp_camera_fb_return(fb);

  delay(CAPTURE_INTERVAL_MS);
}
