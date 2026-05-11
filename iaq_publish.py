cat > ~/iaq/iaq_publish.py << 'EOF'
#!/usr/bin/env python3
"""
IAQ_Monitor — Publish script with dashboard cache + threshold-driven alerts.
"""

import json
import math
import os
import time
import board
import busio
import digitalio
import serial
import adafruit_dht
import adafruit_mcp3xxx.mcp3008 as MCP
from adafruit_mcp3xxx.analog_in import AnalogIn
from collections import deque
from datetime import datetime
from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient

# ---------- Paths and AWS Config ----------
BASE_DIR = "/home/hansela/iaq"
CERT_PATH = f"{BASE_DIR}/cert/"
THRESHOLDS_FILE = f"{BASE_DIR}/thresholds.json"
LATEST_FILE = f"{BASE_DIR}/latest.json"
HISTORY_FILE = f"{BASE_DIR}/history.json"
MQ135_BASELINE_FILE = f"{BASE_DIR}/mq135_baseline.json"

HOST = "a37i9u2rcho5ek-ats.iot.us-east-1.amazonaws.com"
PORT = 8883
CLIENT_ID = "IAQ_Monitor"
TOPIC_DATA = "IAQ_Monitor/data"

POLL_INTERVAL_SEC = 30
HISTORY_WINDOW_SEC = 3600  # 1 hour rolling window
HISTORY_MAX_ENTRIES = HISTORY_WINDOW_SEC // POLL_INTERVAL_SEC + 10

# ---------- Default thresholds ----------
DEFAULT_THRESHOLDS = {
    "temp_c": 28,
    "humidity": 70,
    "co_ppm": 50,
    "co2_ppm": 1000,
    "nh3_ppm": 25,
    "voc_ppm": 10,
    "benzene_ppm": 1,
    "smoke_ppm": 50,
    "pm1_0": 25,
    "pm2_5": 35,
    "pm10": 50
}

# ---------- MQ-135 calibration constants ----------
MQ135_RL = 10.0
MQ135_BASELINE_DURATION = 60
MQ135_PRESENCE_THRESHOLD = 0.05
MQ135_CURVES = {
    "co2_ppm":     {"m": -2.769, "b": 2.602},
    "nh3_ppm":     {"m": -2.273, "b": 0.717},
    "voc_ppm":     {"m": -3.181, "b": 1.137},
    "benzene_ppm": {"m": -3.448, "b": 1.176},
    "smoke_ppm":   {"m": -2.944, "b": 1.491},
}

# ---------- Helpers ----------

def atomic_write_json(filepath, data):
    """Write JSON atomically — write to temp file, then rename."""
    tmp = filepath + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filepath)

def load_thresholds():
    """Load thresholds from disk; create with defaults if missing."""
    if not os.path.exists(THRESHOLDS_FILE):
        atomic_write_json(THRESHOLDS_FILE, DEFAULT_THRESHOLDS)
        return dict(DEFAULT_THRESHOLDS)
    try:
        with open(THRESHOLDS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return dict(DEFAULT_THRESHOLDS)

def compute_alerts(payload, thresholds):
    """Return list of alert codes for any threshold breaches."""
    alerts = []
    for field, threshold in thresholds.items():
        value = payload.get(field)
        if value is None:
            continue
        try:
            if float(value) > float(threshold):
                alerts.append(f"{field}_high")
        except (TypeError, ValueError):
            continue
    return alerts

def update_history(latest):
    """Append latest reading to rolling history file."""
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(latest)
    if len(history) > HISTORY_MAX_ENTRIES:
        history = history[-HISTORY_MAX_ENTRIES:]
    atomic_write_json(HISTORY_FILE, history)

# ---------- Sensor Setup ----------
print("[Init] Setting up sensors...")
dht = adafruit_dht.DHT11(board.D4)

spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)
cs = digitalio.DigitalInOut(board.D8)
mcp = MCP.MCP3008(spi, cs)
mq7_channel = AnalogIn(mcp, MCP.P0)
mq135_channel = AnalogIn(mcp, MCP.P1)

pms_serial = serial.Serial('/dev/serial0', baudrate=9600, timeout=2)

# ---------- MQ-135 helpers ----------

def mq135_read_voltage():
    return mq135_channel.voltage * 2.0

def mq135_is_present(v):
    return v > MQ135_PRESENCE_THRESHOLD

def mq135_voltage_to_rs(v, vcc=5.0):
    if v <= 0.001:
        return float('inf')
    return ((vcc - v) / v) * MQ135_RL

def mq135_th_correction(t, h):
    if t is None or h is None:
        return 1.0
    if h < 65:
        f = 1.397 - 0.0185 * t + 0.00007 * (t ** 2)
    else:
        f = 1.297 - 0.0163 * t + 0.00005 * (t ** 2)
    return max(0.5, min(1.5, f))

def mq135_load_baseline():
    if not os.path.exists(MQ135_BASELINE_FILE):
        return None
    try:
        with open(MQ135_BASELINE_FILE, 'r') as f:
            return json.load(f).get('r0')
    except Exception:
        return None

def mq135_save_baseline(r0):
    atomic_write_json(MQ135_BASELINE_FILE, {
        "r0": r0,
        "calibrated_at": datetime.now().isoformat()
    })
    print(f"[Init] MQ-135 baseline saved: r0 = {r0:.2f} kOhms")

def mq135_calibrate_baseline():
    print(f"[Init] MQ-135 detected, no baseline file found.")
    print(f"[Init] Capturing {MQ135_BASELINE_DURATION}s clean-air baseline.")
    print(f"[Init] !!! Make sure the sensor is in clean indoor air !!!")
    time.sleep(3)
    rs_samples = []
    start = time.time()
    while time.time() - start < MQ135_BASELINE_DURATION:
        v = mq135_read_voltage()
        rs = mq135_voltage_to_rs(v)
        if rs != float('inf'):
            rs_samples.append(rs)
        time.sleep(1)
    if not rs_samples:
        print("[Init] Baseline capture failed")
        return None
    r0 = sum(rs_samples) / len(rs_samples)
    mq135_save_baseline(r0)
    return r0

def mq135_compute_gases(v, r0, t, h):
    if r0 is None or v <= MQ135_PRESENCE_THRESHOLD:
        return {gas: 0 for gas in MQ135_CURVES.keys()}
    rs = mq135_voltage_to_rs(v) * mq135_th_correction(t, h)
    if rs <= 0 or r0 <= 0:
        return {gas: 0 for gas in MQ135_CURVES.keys()}
    ratio = rs / r0
    if ratio <= 0:
        return {gas: 0 for gas in MQ135_CURVES.keys()}
    log_ratio = math.log10(ratio)
    gases = {}
    for gas_name, curve in MQ135_CURVES.items():
        log_ppm = curve['m'] * log_ratio + curve['b']
        ppm = round(10 ** log_ppm, 2)
        gases[gas_name] = max(0, min(10000, ppm))
    return gases

# ---------- Other sensor reads ----------

def read_dht11():
    for _ in range(3):
        try:
            t = dht.temperature
            h = dht.humidity
            if t is not None and h is not None:
                return float(t), float(h)
        except RuntimeError:
            time.sleep(0.5)
    return None, None

def read_mq7():
    raw = mq7_channel.value
    voltage = mq7_channel.voltage
    aout_voltage = voltage * 2
    return {
        "raw": raw,
        "aout_voltage": round(aout_voltage, 3),
        "co_ppm": 0
    }

def read_pms7003():
    while True:
        b = pms_serial.read(1)
        if b == b'\x42':
            b2 = pms_serial.read(1)
            if b2 == b'\x4d':
                break
        if not b:
            return None
    rest = pms_serial.read(30)
    if len(rest) < 30:
        return None
    frame = b'\x42\x4d' + rest
    expected = frame[-2] * 256 + frame[-1]
    actual = sum(frame[:-2])
    if expected != actual:
        return None
    return {
        "pm1_0": frame[10] * 256 + frame[11],
        "pm2_5": frame[12] * 256 + frame[13],
        "pm10":  frame[14] * 256 + frame[15]
    }

# ---------- MQ-135 init ----------
print("[Init] Checking MQ-135 status...")
time.sleep(2)
initial_v = mq135_read_voltage()
mq135_r0 = mq135_load_baseline()
if mq135_is_present(initial_v):
    print(f"[Init] MQ-135 detected (AO = {initial_v:.3f}V)")
    if mq135_r0 is None:
        mq135_r0 = mq135_calibrate_baseline()
    else:
        print(f"[Init] Loaded MQ-135 baseline: r0 = {mq135_r0:.2f} kOhms")
else:
    print(f"[Init] MQ-135 not detected (AO = {initial_v:.3f}V)")

# ---------- AWS IoT setup ----------
print(f"[Init] Connecting to AWS IoT...")
client = AWSIoTMQTTClient(CLIENT_ID)
client.configureEndpoint(HOST, PORT)
client.configureCredentials(
    f"{CERT_PATH}RootCA1.pem",
    f"{CERT_PATH}IAQ_Monitor-private.pem.key",
    f"{CERT_PATH}IAQ_Monitor-cert.pem.crt"
)
client.configureAutoReconnectBackoffTime(1, 32, 20)
client.configureOfflinePublishQueueing(-1)
client.configureDrainingFrequency(2)
client.configureConnectDisconnectTimeout(10)
client.configureMQTTOperationTimeout(5)
client.connect()
print("[Init] Connected to AWS IoT Core")

# ---------- Main loop ----------
print(f"\n[Run] Publishing every {POLL_INTERVAL_SEC}s. Ctrl+C to stop.\n")
sequence = 0

try:
    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        thresholds = load_thresholds()  # Re-read every cycle for live updates

        temp_c, humidity = read_dht11()
        mq7 = read_mq7()
        mq135_v = mq135_read_voltage()
        mq135_present = mq135_is_present(mq135_v)
        mq135_gases = mq135_compute_gases(mq135_v, mq135_r0, temp_c, humidity)
        pm = read_pms7003()

        payload = {
            "sequence": sequence,
            "timestamp": timestamp,
            "device_status": "running",
            "temp_c": temp_c,
            "humidity": humidity,
            "co_ppm": mq7["co_ppm"],
            "mq7_raw_voltage": mq7["aout_voltage"],
            "co2_ppm": mq135_gases["co2_ppm"],
            "nh3_ppm": mq135_gases["nh3_ppm"],
            "voc_ppm": mq135_gases["voc_ppm"],
            "benzene_ppm": mq135_gases["benzene_ppm"],
            "smoke_ppm": mq135_gases["smoke_ppm"],
            "mq135_raw_voltage": round(mq135_v, 3),
            "mq135_present": mq135_present,
            "pm1_0": pm["pm1_0"] if pm else None,
            "pm2_5": pm["pm2_5"] if pm else None,
            "pm10":  pm["pm10"]  if pm else None
        }

        payload["device_alerts"] = compute_alerts(payload, thresholds)
        payload["thresholds_active"] = thresholds

        # Publish to AWS
        client.publish(TOPIC_DATA, json.dumps(payload), 1)

        # Update local cache for dashboard
        atomic_write_json(LATEST_FILE, payload)
        update_history(payload)

        alert_str = f" ALERTS: {payload['device_alerts']}" if payload['device_alerts'] else ""
        print(f"[{sequence}] published.{alert_str}")

        sequence += 1
        time.sleep(POLL_INTERVAL_SEC)

except KeyboardInterrupt:
    print("\n[Shutdown] Disconnecting...")
    client.disconnect()
    print("[Shutdown] Done.")
EOF
