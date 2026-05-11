cat > ~/iaq/dashboard_server.py << 'EOF'
#!/usr/bin/env python3
"""
IAQ_Monitor Dashboard — Flask backend.
Now with outdoor air-quality + weather pull from Open-Meteo (no API key needed).
"""

import json
import os
import socket
import time
import urllib.request
from flask import Flask, render_template, jsonify, request

BASE_DIR = "/home/hansela/iaq"
LATEST_FILE = f"{BASE_DIR}/latest.json"
HISTORY_FILE = f"{BASE_DIR}/history.json"
THRESHOLDS_FILE = f"{BASE_DIR}/thresholds.json"

DEFAULT_THRESHOLDS = {
    "temp_c": 28, "humidity": 70, "co_ppm": 50, "co2_ppm": 1000,
    "nh3_ppm": 25, "voc_ppm": 10, "benzene_ppm": 1, "smoke_ppm": 50,
    "pm1_0": 25, "pm2_5": 35, "pm10": 50
}

# KSU Marietta campus, where G-205 lives
KSU_LAT = 33.9408
KSU_LON = -84.5208
KSU_LOCATION_NAME = "KSU Marietta · G-205"

_outdoor_cache = {"data": None, "fetched_at": 0}
OUTDOOR_CACHE_TTL = 600  # seconds

WMO_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Foggy",
    51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Rain", 63: "Rain", 65: "Heavy rain",
    71: "Snow", 73: "Snow", 75: "Heavy snow",
    80: "Showers", 81: "Showers", 82: "Heavy showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm"
}

def aqi_label(aqi):
    if aqi is None:
        return "—"
    if aqi <= 50:   return "Good"
    if aqi <= 100:  return "Moderate"
    if aqi <= 150:  return "Unhealthy for sensitive"
    if aqi <= 200:  return "Unhealthy"
    if aqi <= 300:  return "Very unhealthy"
    return "Hazardous"

def fetch_outdoor():
    """Fetch + cache outdoor weather + air quality. Returns dict, or {} on failure."""
    now = time.time()
    cached = _outdoor_cache["data"]
    if cached and (now - _outdoor_cache["fetched_at"]) < OUTDOOR_CACHE_TTL:
        return cached
    try:
        aq_url = (
            "https://air-quality-api.open-meteo.com/v1/air-quality"
            f"?latitude={KSU_LAT}&longitude={KSU_LON}"
            "&current=us_aqi,pm2_5,pm10,carbon_monoxide"
        )
        wx_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={KSU_LAT}&longitude={KSU_LON}"
            "&current=temperature_2m,relative_humidity_2m,weather_code"
            "&temperature_unit=fahrenheit"
        )
        with urllib.request.urlopen(aq_url, timeout=5) as r:
            aq = json.loads(r.read())
        with urllib.request.urlopen(wx_url, timeout=5) as r:
            wx = json.loads(r.read())

        wx_cur = wx.get("current", {})
        aq_cur = aq.get("current", {})
        wcode = wx_cur.get("weather_code")

        data = {
            "location": KSU_LOCATION_NAME,
            "temp_f": wx_cur.get("temperature_2m"),
            "humidity": wx_cur.get("relative_humidity_2m"),
            "weather_code": wcode,
            "weather_label": WMO_CODES.get(wcode, "—"),
            "pm2_5": aq_cur.get("pm2_5"),
            "pm10": aq_cur.get("pm10"),
            "co": aq_cur.get("carbon_monoxide"),
            "us_aqi": aq_cur.get("us_aqi"),
            "aqi_label": aqi_label(aq_cur.get("us_aqi")),
            "fetched_at": now
        }
        _outdoor_cache["data"] = data
        _outdoor_cache["fetched_at"] = now
        return data
    except Exception as e:
        print(f"[outdoor] fetch failed: {e}")
        # Return stale cache if we have one, else empty dict
        return cached if cached else {}

app = Flask(__name__, template_folder=f"{BASE_DIR}/templates")

def safe_load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return default

def atomic_write_json(filepath, data):
    tmp = filepath + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filepath)

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/latest')
def api_latest():
    return jsonify(safe_load_json(LATEST_FILE, default={}))

@app.route('/api/history')
def api_history():
    return jsonify(safe_load_json(HISTORY_FILE, default=[]))

@app.route('/api/outdoor')
def api_outdoor():
    return jsonify(fetch_outdoor())

@app.route('/api/thresholds', methods=['GET', 'POST'])
def api_thresholds():
    if request.method == 'POST':
        new_thresholds = request.get_json()
        if not isinstance(new_thresholds, dict):
            return jsonify({"error": "Invalid payload"}), 400
        current = safe_load_json(THRESHOLDS_FILE, default=dict(DEFAULT_THRESHOLDS))
        for k, v in new_thresholds.items():
            try:
                current[k] = float(v)
            except (TypeError, ValueError):
                continue
        atomic_write_json(THRESHOLDS_FILE, current)
        return jsonify(current)
    return jsonify(safe_load_json(THRESHOLDS_FILE, default=dict(DEFAULT_THRESHOLDS)))

if __name__ == '__main__':
    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "<pi-ip>"
    print(f"\nDashboard running at:")
    print(f"  http://localhost:5000  (on the Pi)")
    print(f"  http://{ip}:5000  (from any device on your WiFi)\n")
    print(f"Outdoor data: {KSU_LOCATION_NAME} (cached every {OUTDOOR_CACHE_TTL}s)\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
EOF
