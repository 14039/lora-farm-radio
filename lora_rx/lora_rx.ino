// Feather M0 LoRa (RFM95) — Simple RX: receive JSON, append rssi_dbm, forward to Pi
// Libs: RadioHead (RH_RF95)

#include <SPI.h>
#include <RH_RF95.h>
#include <ctype.h>

// ------------ Build-time switches ------------
// MODE: "serial" (debug prints + forward) or "wireless" (forward only)
#define MODE "serial"

// ------------ RFM95 wiring and radio params ------------
#define RFM95_CS   8
#define RFM95_RST  4
#define RFM95_INT  3
#define RF95_FREQ_MHZ 915.0

// ------------ Addressing (must match TX) ------------
#define MY_ADDR   0x42   // receiver's node ID (TX's DEST_ADDR)

RH_RF95 rf95(RFM95_CS, RFM95_INT);

// ------------ Helpers for switches ------------
static bool isSerialMode() { return strcmp(MODE, "serial") == 0; }

// ------------ Radio reset ------------
static void hardResetRadio() {
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH); delay(10);
  digitalWrite(RFM95_RST, LOW);  delay(10);
  digitalWrite(RFM95_RST, HIGH); delay(10);
}

// ------------ Boot / power-on sequence ------------
static void powerOn() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}

  hardResetRadio();
  if (!rf95.init()) {
    // Blink forever if radio init fails
    while (1) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(100); }
  }

  rf95.setFrequency(RF95_FREQ_MHZ);
  rf95.setThisAddress(MY_ADDR);       // accept packets "to" me (or broadcast)
  // rf95.setModemConfig(RH_RF95::Bw125Cr45Sf128); // keep in sync w/ TX if you change it
  rf95.setModeRx();

  // Optional LED pulse on boot
  digitalWrite(LED_BUILTIN, HIGH); delay(40); digitalWrite(LED_BUILTIN, LOW);

  if (isSerialMode()) {
    Serial.print("RX ready @ "); Serial.print(RF95_FREQ_MHZ);
    Serial.print(" MHz  addr=0x"); Serial.println(MY_ADDR, HEX);
  }
}

// Emit original JSON with an appended rssi_dbm field, without other modifications
static void forwardJsonWithRssi(const char* json, int16_t rssiDbm) {
  // Find the last non-space '}' and inject before it; else, emit raw JSON
  size_t len = strlen(json);
  size_t end = len;
  while (end > 0 && isspace((unsigned char)json[end - 1])) end--;
  if (end > 0 && json[end - 1] == '}') {
    // Print everything up to before the closing brace
    Serial.write((const uint8_t*)json, end - 1);
    Serial.print(",\"rssi_dbm\":");
    Serial.print((int)rssiDbm);
    Serial.println("}");
  } else {
    // Fallback: emit as-is
    Serial.println(json);
  }
}

// ------------ Relay: receive and forward ------------
static void relayData() {
  if (!rf95.available()) { delay(1); return; }

  uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!rf95.recv(buf, &len)) { rf95.setModeRx(); return; }

  if (len >= sizeof(buf)) len = sizeof(buf) - 1;
  buf[len] = 0;                        // treat as C-string for emission
  const char* json = (const char*)buf;

  // RadioHead headers and link quality
  uint8_t from = rf95.headerFrom();
  uint8_t to   = rf95.headerTo();
  uint8_t id   = rf95.headerId();
  int16_t rssi = rf95.lastRssi();

  // Forward: original JSON + appended rssi_dbm
  forwardJsonWithRssi(json, rssi);

  // Optional compact debug line
  if (isSerialMode()) {
    Serial.print("# from=0x"); Serial.print(from, HEX);
    Serial.print(" to=0x");   Serial.print(to,   HEX);
    Serial.print(" id=");     Serial.print(id);
    Serial.print(" rssi=");   Serial.println(rssi);
  }

  // Quick LED blip = packet activity
  digitalWrite(LED_BUILTIN, HIGH); delay(15); digitalWrite(LED_BUILTIN, LOW);

  rf95.setModeRx(); // stay in RX after handling packet
}

void setup() {
  powerOn();
}

void loop() {
  relayData();
}
