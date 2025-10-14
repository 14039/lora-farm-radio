// Feather M0 LoRa (RFM95) — Minimal RX for JSON payloads w/ addressing
// Libs: RadioHead (RH_RF95)
// Matches TX that sends: {"net":165,"node":1,"seq":N,"ts":...,"vbat":X,"t":Y,"rh":Z}

#include <SPI.h>
#include <RH_RF95.h>

#define RFM95_CS   8
#define RFM95_RST  4
#define RFM95_INT  3
#define RF95_FREQ_MHZ 915.0

// --- Addressing: must match the transmitter code ---
#define MY_ADDR   0x42   // receiver's node ID (TX's DEST_ADDR)
#define TX_ADDR   0x01   // expected transmitter "from"
#define NET_ID    0xA5   // app-level net id inside JSON

RH_RF95 rf95(RFM95_CS, RFM95_INT);

void hardResetRadio() {
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH); delay(10);
  digitalWrite(RFM95_RST, LOW);  delay(10);
  digitalWrite(RFM95_RST, HIGH); delay(10);
}

// Tiny/brittle JSON helpers: pull int/float after a key like "seq" or "t"
static long jgetInt(const char* j, const char* key, long def=-1) {
  char pat[12]; snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char* p = strstr(j, pat); if (!p) return def;
  return strtol(p + strlen(pat), nullptr, 10);
}
static double jgetFloat(const char* j, const char* key, double def=NAN) {
  char pat[12]; snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char* p = strstr(j, pat); if (!p) return def;
  return strtod(p + strlen(pat), nullptr);
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}

  hardResetRadio();
  if (!rf95.init()) {
    while (1) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(100); }
  }

  rf95.setFrequency(RF95_FREQ_MHZ);
  rf95.setThisAddress(MY_ADDR);       // accept only packets "to" me (or broadcast)
  // rf95.setModemConfig(RH_RF95::Bw125Cr45Sf128); // keep in sync w/ TX if you change it
  rf95.setModeRx();

  Serial.print("RX ready @ "); Serial.print(RF95_FREQ_MHZ);
  Serial.print(" MHz  addr=0x"); Serial.println(MY_ADDR, HEX);
}

void loop() {
  if (!rf95.available()) { delay(1); return; }

  uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!rf95.recv(buf, &len)) return;

  if (len >= sizeof(buf)) len = sizeof(buf) - 1;
  buf[len] = 0;                        // treat as C-string for parsing
  const char* json = (const char*)buf;

  // RadioHead headers (already filtered by "to==MY_ADDR" unless broadcast)
  uint8_t from = rf95.headerFrom();
  uint8_t to   = rf95.headerTo();
  uint8_t id   = rf95.headerId();
  int16_t rssi = rf95.lastRssi();

  // Optional extra guards (cheap, easy to debug)
  if (from != TX_ADDR) {               // not our expected TX; ignore quietly
    rf95.setModeRx(); return;
  }
  long net = jgetInt(json, "net", -1);
  if (net != NET_ID) { rf95.setModeRx(); return; }

  // Pull a few fields (brittle but readable); others remain in JSON
  long   seq   = jgetInt(json, "seq", -1);
  double vbat  = jgetFloat(json, "vbat", NAN);
  double tC    = jgetFloat(json, "t", NAN);
  double rhPct = jgetFloat(json, "rh", NAN);

  // One-line log: seq, RSSI, vbat, t, rh, and raw JSON for eyeballing
  Serial.print("ok seq="); Serial.print(seq);
  Serial.print(" from=0x"); Serial.print(from, HEX);
  Serial.print(" rssi=");   Serial.print(rssi);
  Serial.print(" vbat=");   Serial.print(vbat, 3);
  Serial.print(" tC=");     Serial.print(isnan(tC) ? NAN : tC, 2);
  Serial.print(" rh=");     Serial.print(isnan(rhPct) ? NAN : rhPct, 2);
  Serial.print(" id=");     Serial.print(id);          // headerId mirrors low byte of TX seq
  Serial.print(" | ");      Serial.println(json);      // full payload for debug

  // Quick LED blip = packet activity
  digitalWrite(LED_BUILTIN, HIGH); delay(15); digitalWrite(LED_BUILTIN, LOW);

  rf95.setModeRx(); // stay in RX after handling packet
}
