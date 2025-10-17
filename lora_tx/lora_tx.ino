// Feather M0 LoRa (RFM95 @ 915 MHz) — JSON TX with SHT31 or Moisture + low power
// HW: Feather M0 LoRa (RFM95)  | Pins: CS=8, RST=4, INT=3  | VBAT on A7 | Moisture on A1
// Libs: RadioHead (RH_RF95), Adafruit_SleepyDog, Adafruit_SHT31

#include <SPI.h>
#include <Wire.h>
#include <RH_RF95.h>
#include <Adafruit_SleepyDog.h>
#include <Adafruit_SHT31.h>



// ------------ Sensor identity ------------
#define SENSOR_ID 1
#define NAME "dev-moisture"

// Optional fixed GPS coordinates for sensor registration (set to NAN if unknown)
#define GPS_LATITUDE   44.839602
#define GPS_LONGITUDE -122.777543


// ------------ Build-time switches ------------
// MODE: "dev" (print + wireless) or "prod" (wireless only)
#define MODE "dev"
// LED:  "on" to blink LED on transmit in dev, no LEDs in prod
#define LED_MODE "on"
// SENSOR: "temp" for SHT31-D, "moisture" for capacitive moisture on A1
#define SENSOR "moisture"


// Here's what the output looks like:
// TX 0 -> 42 | {"net":165,"sensor_id":1,"name":"test-tx","sequence":0,"millis_since_boot":1981,"battery_v":4.271,"temperature_c":null,"humidity_pct":null,"capacitance_val":3016,"gps_lat":37.4219999,"gps_long":-122.0840575}
//TX 1 -> 42 | {"net":165,"sensor_id":1,"name":"test-tx","sequence":1,"millis_since_boot":2223,"battery_v":4.279,"temperature_c":null,"humidity_pct":null,"capacitance_val":3013,"gps_lat":37.4219999,"gps_long":-122.0840575}

// ------------ RFM95 wiring (Feather M0 LoRa default) ------------
#define RFM95_CS   8
#define RFM95_RST  4
#define RFM95_INT  3

// Moisture analog input
#define MOISTURE_PIN A1

// ------------ Radio params ------------
#define RF95_FREQ_MHZ   915.0     // US ISM band
#define RF95_TX_DBM     10        // 5–13 for bring-up; lower saves power
#define SEND_INTERVAL_MS 15000UL  // extend for battery operation (e.g., 60_000+)

// ------------ Addressing (RadioHead headers) ------------
#define MY_ADDR    ((uint8_t)(SENSOR_ID & 0xFF)) // derive 1-byte radio address
#define DEST_ADDR  0x42           // receiver's node ID
#define NET_ID     0xA5           // app-level network ID (for debugging)

RH_RF95 rf95(RFM95_CS, RFM95_INT);
Adafruit_SHT31 sht31 = Adafruit_SHT31();

static uint32_t seq = 0;

// ------------ Helpers for switches ------------
static bool isDevMode() { return strcmp(MODE, "dev") == 0; }
static bool isLedOn()      { return strcmp(LED_MODE, "on") == 0; }
static bool useTempSensor(){ return strcmp(SENSOR, "temp") == 0; }
static bool useMoistureSensor(){ return strcmp(SENSOR, "moisture") == 0; }

// ------------ LED helpers ------------
static void doubleFlashLed() {
  digitalWrite(LED_BUILTIN, HIGH); delay(30); digitalWrite(LED_BUILTIN, LOW); delay(30);
  digitalWrite(LED_BUILTIN, HIGH); delay(30); digitalWrite(LED_BUILTIN, LOW);
}

// Feather M0 battery sense: A7 with 2:1 divider to 3.3V ADC ref
static float readVBAT() {
  analogReadResolution(12); // 0..4095 on SAMD21
  uint16_t raw = analogRead(A7);
  // Vbat = raw/4095 * 3.3V * 2 (divider)
  return (raw * (3.3f / 4095.0f) * 2.0f);
}

static void hardResetRadio() {
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH); delay(10);
  digitalWrite(RFM95_RST, LOW);  delay(10);
  digitalWrite(RFM95_RST, HIGH); delay(10);
}

static void sleepFor(uint32_t ms) {
  while (ms > 0) {
    int s = Watchdog.sleep(ms);
    if (s <= 0) break;
    ms -= s;
  }
}

// ------------ App organization: power on / measure / transmit ------------
static void powerOn() {
  // Optional debug UART (dev only to save power in prod)
  if (isDevMode()) {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}
  }

  // Sensors
  if (useTempSensor()) {
    Wire.begin();
    if (!sht31.begin(0x44)) {
      if (!sht31.begin(0x45)) {
    if (isDevMode()) Serial.println("SHT31 not found");
      }
    }
    sht31.heater(false);
  } else if (useMoistureSensor()) {
    analogReadResolution(12);
    pinMode(MOISTURE_PIN, INPUT);
  }

  // Radio init
  hardResetRadio();
  if (!rf95.init()) {
    pinMode(LED_BUILTIN, OUTPUT);
    if (isDevMode()) Serial.println("RFM95 init failed");
    if (isDevMode()) {
      while (1) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(100); }
    } else {
      while (1) { delay(1000); }
    }
  }

  rf95.setFrequency(RF95_FREQ_MHZ);
  // Lower TX power in prod to save energy (tune as needed)
  rf95.setTxPower(isDevMode() ? RF95_TX_DBM : 5, false); // PA_BOOST
  // rf95.setModemConfig(RH_RF95::Bw125Cr48Sf4096); // For long range if needed

  // Addressing & filtering
  rf95.setThisAddress(MY_ADDR);
  rf95.setHeaderFrom(MY_ADDR);

  // Optional: LED pulse on boot (dev only)
  pinMode(LED_BUILTIN, OUTPUT);
  if (isDevMode()) {
    digitalWrite(LED_BUILTIN, HIGH); delay(40); digitalWrite(LED_BUILTIN, LOW);
  }
}

static void measure(char* json, size_t jsonSize) {
  // Common readings
  const float vbat = readVBAT();
  const uint32_t millisSinceBoot = millis();

  // Sensor readings
  float tempC = NAN, rh = NAN;
  int moisture = -1;

  if (useTempSensor()) {
    tempC = sht31.readTemperature();
    rh    = sht31.readHumidity();
  } else if (useMoistureSensor()) {
    analogReadResolution(12);
    moisture = analogRead(MOISTURE_PIN);
  }

  // Prepare field tokens (either numeric string or null)
  char tempBuf[16]; const char* temperatureTok = "null";
  char rhBuf[16];   const char* humidityTok    = "null";
  char mBuf[16];    const char* capacitanceTok = "null";
  if (!isnan(tempC)) { snprintf(tempBuf, sizeof(tempBuf), "%.2f", tempC); temperatureTok = tempBuf; }
  if (!isnan(rh))    { snprintf(rhBuf,  sizeof(rhBuf),  "%.2f", rh);  humidityTok    = rhBuf; }
  if (moisture >= 0) { snprintf(mBuf,   sizeof(mBuf),   "%d",   moisture); capacitanceTok = mBuf; }

  // Build compact JSON
  // {"net":165,"sensor_id":1,"name":"x","sequence":1,"millis_since_boot":1,"battery_v":4.12,
  //  "temperature_c":..,"humidity_pct":..,"capacitance_val":..,"gps_lat":..,"gps_long":..}
  int n = snprintf(
    json, jsonSize,
    "{\"net\":%u,\"sensor_id\":%u,\"name\":\"%s\",\"sequence\":%lu,\"millis_since_boot\":%lu,\"battery_v\":%.3f,\"temperature_c\":%s,\"humidity_pct\":%s,\"capacitance_val\":%s,\"gps_lat\":%.7f,\"gps_long\":%.7f}",
    (unsigned)NET_ID,
    (unsigned)SENSOR_ID,
    NAME,
    (unsigned long)seq,
    (unsigned long)millisSinceBoot,
    vbat,
    temperatureTok,
    humidityTok,
    capacitanceTok,
    (double)GPS_LATITUDE,
    (double)GPS_LONGITUDE
  );
  if (n < 0 || (size_t)n >= jsonSize) {
    strcpy(json, "{\"err\":\"pkt_ovf\"}");
  }
}

static void transmit(const char* json) {
  // Address headers (RadioHead)
  rf95.setHeaderTo(DEST_ADDR);
  rf95.setHeaderId((uint8_t)(seq & 0xFF));
  rf95.setHeaderFlags(0x00);

  // Send
  rf95.send((const uint8_t*)json, strlen(json));
  rf95.waitPacketSent();

  // LED activity indication: dev only; no LEDs in prod
  if (isDevMode() && isLedOn()) {
    doubleFlashLed();
  }

  // Optional serial log (dev only)
  if (isDevMode()) {
    Serial.print("TX "); Serial.print(seq);
    Serial.print(" -> "); Serial.print(DEST_ADDR, HEX);
    Serial.print(" | "); Serial.println(json);
  }
}

void setup() {
  powerOn();
}

void loop() {
  char json[256];
  measure(json, sizeof(json));
  transmit(json);
  seq++;
  rf95.sleep();
  sleepFor(SEND_INTERVAL_MS);
}