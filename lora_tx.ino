// Feather M0 LoRa (RFM95 @ 915 MHz) — JSON TX with SHT31 + low power
// HW: Feather M0 LoRa (RFM95)  | Pins: CS=8, RST=4, INT=3  | VBAT on A7
// Libs: RadioHead (RH_RF95), Adafruit_SleepyDog, Adafruit_SHT31

#include <SPI.h>
#include <Wire.h>
#include <RH_RF95.h>
#include <Adafruit_SleepyDog.h>
#include <Adafruit_SHT31.h>

#define NAME "test-tx"


// ------------ RFM95 wiring (Feather M0 LoRa default) ------------
#define RFM95_CS   8
#define RFM95_RST  4
#define RFM95_INT  3

// ------------ Radio params ------------
#define RF95_FREQ_MHZ   915.0     // US ISM band
#define RF95_TX_DBM     10        // 5–13 for bring-up; lower saves power
#define SEND_INTERVAL_MS 15000UL  // extend for battery operation (e.g., 60_000+)

// ------------ Addressing (RadioHead headers) ------------
#define MY_ADDR    0x01           // this transmitter's node ID
#define DEST_ADDR  0x42           // receiver's node ID
#define NET_ID     0xA5           // app-level network ID (for debugging)

RH_RF95 rf95(RFM95_CS, RFM95_INT);
Adafruit_SHT31 sht31 = Adafruit_SHT31();

static uint32_t seq = 0;

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

void setup() {
  // Optional debug UART. Comment out in final deployment.
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}

  // I2C sensor
  Wire.begin();
  if (!sht31.begin(0x44)) { // most SHT31-D boards default to 0x44
    // Try the alternate address
    if (!sht31.begin(0x45)) {
      if (Serial) Serial.println("SHT31 not found");
    }
  }
  sht31.heater(false); // ensure heater is off

  // Radio init
  hardResetRadio();
  if (!rf95.init()) {
    pinMode(LED_BUILTIN, OUTPUT);
    if (Serial) Serial.println("RFM95 init failed");
    while (1) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(100); }
  }

  rf95.setFrequency(RF95_FREQ_MHZ);
  rf95.setTxPower(RF95_TX_DBM, false); // false => PA_BOOST path on RFM95
  // Default modem: Bw125Cr45Sf128 (SF7). For long range (slower), consider:
  // rf95.setModemConfig(RH_RF95::Bw125Cr48Sf4096); // SF12

  // Addressing & filtering
  rf95.setThisAddress(MY_ADDR);
  rf95.setHeaderFrom(MY_ADDR);
  // Non-promiscuous by default: will *transmit* regardless; RX will filter.

  // Optional: LED pulse on boot
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH); delay(40); digitalWrite(LED_BUILTIN, LOW);
}

void loop() {
  // --- Read sensors ---
  float tempC = NAN, rh = NAN;
  // Adafruit_SHT31 triggers single measurement internally; returns NAN on error
  tempC = sht31.readTemperature();
  rh    = sht31.readHumidity();

  // Battery
  const float vbat = readVBAT();

  // Timestamp
  const uint32_t ts = millis();

  // --- Build compact JSON payload ---
  // Example:
  // {"net":165,"node":1,"seq":123,"ts":456789,"vbat":4.12,"t":23.45,"rh":56.78}
  char json[160];
  int n = snprintf(
    json, sizeof(json),
    "{\"net\":%u,\"node\":%u,\"name\":\"%s\",\"seq\":%lu,\"ts\":%lu,\"vbat\":%.3f,"
    "\"t\":%s,\"rh\":%s}",
    (unsigned)NET_ID,
    (unsigned)MY_ADDR,
    NAME,
    (unsigned long)seq,
    (unsigned long)ts,
    vbat,
    isnan(tempC) ? "nxull" : ([](float v){ static char b[16]; snprintf(b,sizeof(b),"%.2f",v); return b; })(tempC),
    isnan(rh)    ? "null" : ([](float v){ static char b[16]; snprintf(b,sizeof(b),"%.2f",v); return b; })(rh)
  );
  if (n < 0 || n >= (int)sizeof(json)) {
    // Fallback small packet if we ever overflow (shouldn't at this size)
    strcpy(json, "{\"err\":\"pkt_ovf\"}");
  }

  // --- Address headers (RadioHead) ---
  rf95.setHeaderTo(DEST_ADDR);
  rf95.setHeaderId((uint8_t)(seq & 0xFF));  // wraps every 256 packets
  rf95.setHeaderFlags(0x00);                // no special flags

  // --- Transmit ---
  rf95.send((uint8_t*)json, strlen(json));
  rf95.waitPacketSent();


  // Blink LED on each transmission
  digitalWrite(LED_BUILTIN, HIGH); delay(20); digitalWrite(LED_BUILTIN, LOW);


  if (Serial) {
    Serial.print("TX "); Serial.print(seq);
    Serial.print(" -> "); Serial.print(DEST_ADDR, HEX);
    Serial.print(" | "); Serial.println(json);
  }

  seq++;

  // --- Low power between sends ---
  rf95.sleep();                 // sleep the radio first
  sleepFor(SEND_INTERVAL_MS);   // deep sleep MCU (RTC)
}