import threading
import time
import logging
from flask import Flask, jsonify, request, render_template, send_from_directory
from datetime import datetime, timedelta, timezone
import json
import signal
import sys
import paho.mqtt.client as mqtt
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask_cors import CORS
import requests
from threading import Lock

# ----------------------- CONFIG -----------------------
app = Flask(__name__)
CORS(app)

BROKER = "10.1.1.55"
PORT = 1883
TOPIC_PATTERN = "sensor/#"  # semua sensor
TOPIC_PREDICT = "predict/pub"
TOPIC_PREDICT_RESULT = "predict/result"

# TimescaleDB / PostgreSQL config
DB_CONFIG = {
    "dbname": "sensor_data",
    "user": "postgres",
    "password": "p4tk@vedc",
    "host": "localhost",
    "port": 5432
}

DB_POOL_MINCONN = 1
DB_POOL_MAXCONN = 10
db_pool = None

latest_data = {}

# -------------------- CONFIGURATIONS FOR ROBUSTNESS --------------------
MAX_BUCKETS_PER_SENSOR = 100  # Limit bucket per sensor to prevent memory leak
FLUSH_INTERVAL_SEC = 15  # Interval for checking matured buckets
CLEANUP_INTERVAL_MIN = 5  # Cleanup old buckets every 5 minutes
MAX_RETRY_DB = 3  # Max retries for DB operations
MQTT_RECONNECT_DELAY_SEC = 5  # Delay before MQTT reconnect
BUCKET_CUTOFF_SEC = 10  # Flush buckets older than 10 seconds
OLD_BUCKET_THRESHOLD_HOURS = 1  # Delete buckets older than 1 hour

TARIF_PER_KWH = 1500.0
PPJ = 0.05  # 5% (adjust to 0.1 if meant 10%)

# -------------------- LOGGING SETUP --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('server_pzem.log')  # Log to file for persistence
    ]
)
logger = logging.getLogger(__name__)

# -------------------- THREAD SAFETY --------------------
db_write_lock = threading.Lock()
MODEL = None
MODEL_LOCK = threading.Lock()

# --------------------- DATABASE HELPERS ------------------------
def init_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = ThreadedConnectionPool(
                DB_POOL_MINCONN, DB_POOL_MAXCONN,
                dbname=DB_CONFIG["dbname"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                host=DB_CONFIG["host"],
                port=DB_CONFIG["port"],
                connect_timeout=10  # Timeout for connections
            )
            logger.info("DB pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}")
            sys.exit(1)

def get_conn():
    if db_pool is None:
        init_db_pool()
    try:
        return db_pool.getconn()
    except Exception as e:
        logger.error(f"Failed to get DB connection: {e}")
        raise

def put_conn(conn):
    if db_pool:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            logger.warning(f"Failed to put back DB connection: {e}")

def query_db_pg(query, args=(), one=False):
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    except Exception as e:
        logger.error(f"DB query error: {e}")
        raise
    finally:
        put_conn(conn)

def execute_db_pg(query, args=()):
    for attempt in range(MAX_RETRY_DB):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(query, args)
            conn.commit()
            return
        except Exception as e:
            logger.warning(f"DB execute attempt {attempt+1} failed: {e}")
            conn.rollback()
            if attempt < MAX_RETRY_DB - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
        finally:
            cur.close()
            put_conn(conn)
    logger.error("DB execute failed after all retries.")

# ------------------- MQTT / SENSOR LOGIC -------------------
def get_sensor_id_from_topic(topic: str):
    try:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "sensor":
            return None
        building_code, sensor_name = parts[1], parts[2]
        row = query_db_pg("""
            SELECT s.id FROM sensors s
            JOIN buildings b ON s.building_id = b.id
            WHERE b.code = %s AND s.name = %s LIMIT 1
        """, (building_code, sensor_name), one=True)
        return row["id"] if row else None
    except Exception as e:
        logger.error(f"Error get_sensor_id_from_topic: {e}")
        return None

def save_sensor_data(sensor_id: int, data: dict):
    required = ('tegangan', 'arus', 'daya', 'energi', 'frekuensi', 'biaya', 'pf', 'peak_voltage', 'peak_current')  # Tambahan: peak fields
    if not all(k in data for k in required):
        logger.warning(f"Incomplete data for sensor {sensor_id}, skipping save.")
        return

    ts = data.get('force_timestamp', datetime.utcnow().replace(tzinfo=timezone.utc))

    for attempt in range(MAX_RETRY_DB):
        with db_write_lock:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO sensor_readings
                    (sensor_id, timestamp, voltage, current, power, energy, frequency, cost, power_factor, peak_voltage, peak_current)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sensor_id, timestamp) DO UPDATE SET
                      voltage = EXCLUDED.voltage, current = EXCLUDED.current,
                      power = EXCLUDED.power, energy = EXCLUDED.energy,
                      frequency = EXCLUDED.frequency, cost = EXCLUDED.cost,
                      power_factor = EXCLUDED.power_factor,
                      peak_voltage = EXCLUDED.peak_voltage, peak_current = EXCLUDED.peak_current
                """, (
                    sensor_id, ts,
                    float(data['tegangan']), float(data['arus']), float(data['daya']),
                    float(data['energi']), float(data['frekuensi']), float(data['biaya']),
                    float(data['pf']), float(data['peak_voltage']), float(data['peak_current'])
                ))
                conn.commit()
                logger.info(f"Data saved for sensor {sensor_id} @ {ts}")
                return
            except Exception as e:
                logger.warning(f"Save attempt {attempt+1} failed for sensor {sensor_id}: {e}")
                conn.rollback()
                if attempt < MAX_RETRY_DB - 1:
                    time.sleep(2 ** attempt)
            finally:
                cur.close()
                put_conn(conn)
    logger.error(f"Failed to save data for sensor {sensor_id} after retries.")

# ------------------- AGGREGATION BUFFER (TIME-BUCKETED) -------------------
agg_buffer = {}
agg_lock = threading.Lock()

def accumulate_sensor_data(sensor_id: int, data: dict):
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    bucket_time = now.replace(second=0, microsecond=0)
    bucket_ts = bucket_time.timestamp()

    with agg_lock:
        if sensor_id not in agg_buffer:
            agg_buffer[sensor_id] = {}

        # Enforce bucket limit
        if len(agg_buffer[sensor_id]) >= MAX_BUCKETS_PER_SENSOR:
            # Remove oldest bucket (FIFO)
            oldest_key = min(agg_buffer[sensor_id].keys())
            del agg_buffer[sensor_id][oldest_key]
            logger.warning(f"Bucket limit exceeded for sensor {sensor_id}, removed oldest bucket.")

        if bucket_ts not in agg_buffer[sensor_id]:
            agg_buffer[sensor_id][bucket_ts] = {
                "sums": {k: 0.0 for k in ['tegangan', 'arus', 'daya', 'energi', 'frekuensi', 'biaya', 'pf']},
                "count": 0,
                "timestamp_obj": bucket_time,
                "peak_voltage": float('-inf'),  # Tambahan: Peak voltage
                "peak_current": float('-inf')   # Tambahan: Peak current
            }

        buf = agg_buffer[sensor_id][bucket_ts]

        try:
            daya = float(data.get('daya', 0.0))
            energi_wh = daya * (5.0 / 3600.0)
            energi_kwh = energi_wh / 1000.0
            biaya = energi_kwh * TARIF_PER_KWH * (1 + PPJ)

            # Akumulasi sums
            for k in ['tegangan', 'arus', 'daya', 'frekuensi', 'pf']:
                buf['sums'][k] += float(data.get(k, 0.0))
            buf['sums']['energi'] += energi_kwh
            buf['sums']['biaya'] += biaya
            buf['count'] += 1

            # Tambahan: Update peak voltage dan current
            voltage = float(data.get('tegangan', 0.0))
            current = float(data.get('arus', 0.0))
            buf['peak_voltage'] = max(buf['peak_voltage'], voltage)
            buf['peak_current'] = max(buf['peak_current'], current)

            logger.debug(f"Accumulated data for sensor {sensor_id}, bucket {bucket_time.strftime('%H:%M')}, count: {buf['count']}, peak_v: {buf['peak_voltage']}, peak_c: {buf['peak_current']}")
        except Exception as e:
            logger.error(f"Error accumulating data for sensor {sensor_id}: {e}")

def flush_buffer_buckets(sensor_id: int):
    now_ts = time.time()
    cutoff_time = now_ts - BUCKET_CUTOFF_SEC

    with agg_lock:
        if sensor_id not in agg_buffer:
            return

        bucket_keys = list(agg_buffer[sensor_id].keys())
        for b_key in bucket_keys:
            if b_key > cutoff_time:
                continue

            buf = agg_buffer[sensor_id][b_key]
            count = buf['count']
            if count > 0:
                sums = buf['sums']
                payload = {
                    "tegangan": sums['tegangan'] / count,
                    "arus": sums['arus'] / count,
                    "daya": sums['daya'] / count,
                    "energi": sums['energi'],
                    "frekuensi": sums['frekuensi'] / count,
                    "biaya": sums['biaya'],
                    "pf": sums['pf'] / count,
                    "force_timestamp": buf['timestamp_obj'],
                    # Tambahan: Peak values
                    "peak_voltage": buf['peak_voltage'],
                    "peak_current": buf['peak_current']
                }
                try:
                    save_sensor_data(sensor_id, payload)
                except Exception as e:
                    logger.error(f"Flush error for sensor {sensor_id}: {e}")
            del agg_buffer[sensor_id][b_key]

def cleanup_old_buckets():
    """Remove buckets older than threshold to free memory."""
    cutoff_ts = time.time() - (OLD_BUCKET_THRESHOLD_HOURS * 3600)
    with agg_lock:
        for sensor_id in list(agg_buffer.keys()):
            old_keys = [k for k in agg_buffer[sensor_id] if k < cutoff_ts]
            for k in old_keys:
                del agg_buffer[sensor_id][k]
                logger.info(f"Cleaned old bucket for sensor {sensor_id} @ {datetime.fromtimestamp(k)}")
            if not agg_buffer[sensor_id]:
                del agg_buffer[sensor_id]

def flush_worker():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: [flush_buffer_buckets(sid) for sid in list(agg_buffer.keys())],
        trigger=IntervalTrigger(seconds=FLUSH_INTERVAL_SEC),
        id='flush_job',
        name='Flush matured buckets'
    )
    scheduler.add_job(
        func=cleanup_old_buckets,
        trigger=IntervalTrigger(minutes=CLEANUP_INTERVAL_MIN),
        id='cleanup_job',
        name='Cleanup old buckets'
    )
    scheduler.start()
    logger.info("Flush worker and cleanup scheduler started.")

# ---------------------- MQTT HANDLER -------------------
def handle_sensor_message(sensor_id: int, data: dict):
    try:
        accumulate_sensor_data(sensor_id, data)
    except Exception as e:
        logger.error(f"Failed to handle sensor message: {e}")

def handle_message(topic: str, data: dict, client: mqtt.Client):
    latest_data[topic] = data
    sensor_id = get_sensor_id_from_topic(topic)
    if sensor_id:
        handle_sensor_message(sensor_id, data)
    elif topic == TOPIC_PREDICT:
        pass
    else:
        logger.debug(f"Unrecognized topic: {topic}")

# ---------------------- MQTT CALLBACK ------------------
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT Broker")
        client.subscribe([(TOPIC_PATTERN, 0), (TOPIC_PREDICT, 0)])
    else:
        logger.warning(f"MQTT connect failed with rc: {rc}, retrying in {MQTT_RECONNECT_DELAY_SEC}s")
        time.sleep(MQTT_RECONNECT_DELAY_SEC)
        client.reconnect()

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        data = json.loads(payload)
        handle_message(msg.topic, data, client)
    except Exception as e:
        logger.error(f"Error in on_message: {e}")

def start_mqtt(loop_forever=False):
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(BROKER, PORT, 60)
        client.loop_start()
        logger.info("MQTT client started.")
        return client
    except Exception as e:
        logger.error(f"Failed to start MQTT: {e}")
        return None

# ------------------------ GET BUILDINGS & SENSORS ------------------------
def get_buildings_with_sensors():
    try:
        rows = query_db_pg("""
            SELECT b.id as building_id, b.name as building_name, b.code as building_code,
                   s.id as sensor_id, s.name as sensor_name
            FROM buildings b LEFT JOIN sensors s ON b.id = s.building_id
            ORDER BY b.id;
        """)
        buildings = {}
        for row in rows:
            building_name = row['building_name']
            if building_name not in buildings:
                buildings[building_name] = {
                    'building_id': row['building_id'],
                    'building_code': row['building_code'],
                    'sensors': []
                }
            if row['sensor_id']:
                buildings[building_name]['sensors'].append({
                    'sensor_id': row['sensor_id'],
                    'sensor_name': row['sensor_name'],
                    'topic': f"sensor/{row['building_code']}/{row['sensor_name']}"
                })
        return buildings
    except Exception as e:
        logger.error(f"Error getting buildings: {e}")
        return {}

# ------------------------ BACKEND DASHBOARD PUSAT ------------------------
# ======================== REALTIME DATA ========================
@app.route("/")
def index_page():
    """Render halaman dashboard"""
    return render_template("view_mode.html")

@app.route("/realtime")
def get_realtime():
    now = datetime.utcnow().replace(tzinfo=timezone.utc)

    # Awal dan akhir bulan (UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0)
    month_end = next_month - timedelta(seconds=1)

    buildings_data = get_buildings_with_sensors()
    departments = []

    total_energy_all = 0.0
    total_cost_all = 0.0

    for building_name, info in buildings_data.items():
        phases = {}
        total_energy = 0.0
        total_cost = 0.0

        for sensor in info['sensors']:
            topic = f"sensor/{info['building_code']}/{sensor['sensor_name']}"
            sensor_data = latest_data.get(topic)

            # Tentukan fase (ambil char terakhir bila r/s/t)
            phase_key = sensor['sensor_name'][-1].lower() if sensor['sensor_name'][-1].lower() in ['r', 's', 't'] else sensor['sensor_name']

            # Default nilai jika belum ada data MQTT
            if not sensor_data:
                sensor_data = {"tegangan": 0, "arus": 0, "daya": 0, "energi": 0}

            phases[phase_key] = {
                "voltage": float(sensor_data.get("tegangan", 0.0)),
                "current": float(sensor_data.get("arus", 0.0)),
                "power": float(sensor_data.get("daya", 0.0)),
                "energy": float(sensor_data.get("energi", 0.0))
            }

            # Ambil data bulanan dari DB (gunakan timestamp BETWEEN)
            row = query_db_pg("""
                SELECT 
                    SUM(energy) as total_energy,
                    SUM(cost) as total_cost
                FROM sensor_readings
                WHERE sensor_id = %s
                  AND timestamp BETWEEN %s AND %s
            """, (
                sensor['sensor_id'],
                month_start,
                month_end
            ), one=True)

            if row:
                total_energy += float(row["total_energy"] or 0)
                total_cost += float(row["total_cost"] or 0)

        departments.append({
            "id": info['building_code'],
            "name": building_name,
            "phases": phases,
            "total": {
                "total_energy": total_energy,
                "total_cost": total_cost
            }
        })

        total_energy_all += total_energy
        total_cost_all += total_cost

    summary = {
        "overall_energy": round(total_energy_all, 4),
        "overall_cost": round(total_cost_all, 0),
        "month": now.strftime("%B"),
        "year": now.year
    }

    return jsonify({
        "success": True,
        "timestamp": now.isoformat(),
        "departments": departments,
        "summary": summary
    })

# ------------------------ DASHBOARD ADMIN API ------------------------
@app.route("/admin")
def admin_page():
    return render_template("realtime_fetch.html")

@app.route("/dashboard-admin", methods=["GET"])
def get_dashboard():
    field = request.args.get("field")

    building_stats = {}
    buildings_data = get_buildings_with_sensors()

    for building_name, info in buildings_data.items():
        for sensor in info['sensors']:
            topic = f"sensor/{info['building_code']}/{sensor['sensor_name']}"
            sensor_data = latest_data.get(topic)

            if not sensor_data:
                continue

            if building_name not in building_stats:
                building_stats[building_name] = {
                    "sums": {},
                    "count": 0
                }

            stats = building_stats[building_name]
            stats["count"] += 1

            if field:
                if field.lower() in ["energi", "energy"]:
                    daya_val = float(sensor_data.get("daya", 0) or 0)
                    energi_val = (daya_val * 3) / 3600 / 1000
                    stats["sums"]["daya"] = stats["sums"].get("daya", 0) + daya_val
                    stats["sums"]["energi"] = stats["sums"].get("energi", 0) + energi_val
                else:
                    val = float(sensor_data.get(field, 0) or 0)
                    stats["sums"][field] = stats["sums"].get(field, 0) + val
            else:
                for k, v in sensor_data.items():
                    try:
                        stats["sums"][k] = stats["sums"].get(k, 0) + float(v or 0)
                    except Exception:
                        pass

    results = {}
    for building_name, stats in building_stats.items():
        count = stats["count"]
        if count > 0:
            daya_total = stats["sums"].get("daya", 0.0)

            averaged = {}
            for k, v in stats["sums"].items():
                key = k.lower()
                if key in ["daya", "power"]:
                    averaged[k] = round(daya_total, 3)
                elif key in ["energi", "energy"]:
                    energi_val = (daya_total * 3) / 3600 / 1000
                    if 0 < energi_val < 0.001:
                        averaged[k] = float(f"{energi_val:.7e}")
                    else:
                        averaged[k] = round(energi_val, 6)
                else:
                    averaged[k] = round(v / count, 3)

            results[building_name] = averaged
        else:
            results[building_name] = None

    return jsonify(results)

# ======================== ENERGY USAGE ========================
@app.route("/index/energy-usage")
def energy_usage():
    buildings_data = get_buildings_with_sensors()
    datasets = []
    labels = []

    for building_name, info in buildings_data.items():
        energy_per_hour = {}

        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']

            # --- Ganti continuous view dengan agregasi manual ---
            rows = query_db_pg("""
                SELECT 
                    to_char(time_bucket('1 hour', timestamp), 'HH24:MI') AS jam,
                    SUM(energy) AS total_energy
                FROM sensor_readings
                WHERE sensor_id = %s
                GROUP BY 1
                ORDER BY 1 ASC
                LIMIT 60
            """, (sensor_id,))

            for row in rows:
                jam = row["jam"]
                energi = float(row["total_energy"] or 0)
                energy_per_hour[jam] = energy_per_hour.get(jam, 0) + energi

        sorted_times = sorted(energy_per_hour.keys())
        usage = [energy_per_hour[j] for j in sorted_times]

        datasets.append({
            "building": building_name,
            "usage": usage
        })

        if not labels and sorted_times:
            labels = sorted_times

    return jsonify({
        "labels": labels,
        "datasets": datasets
    })


# ======================== PIE CHART ========================
@app.route("/index/energy-pie")
def energy_pie():
    period = request.args.get('period', 'minggu')

    end_date = datetime.utcnow().replace(tzinfo=timezone.utc)
    if period == 'minggu':
        start_date = end_date - timedelta(days=7)
        period_label = "Minggu Ini"
    else:
        start_date = end_date - timedelta(days=30)
        period_label = "Bulan Ini"

    labels = []
    values = []
    total_energy = 0.0

    buildings_data = get_buildings_with_sensors()

    for building_name, info in buildings_data.items():
        building_total = 0.0

        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']

            # --- Query langsung tanpa view ---
            row = query_db_pg("""
                SELECT SUM(energy) AS total
                FROM sensor_readings
                WHERE sensor_id = %s AND timestamp BETWEEN %s AND %s
            """, (sensor_id, start_date, end_date), one=True)

            if row and row["total"] is not None:
                building_total += float(row["total"])

        labels.append(building_name)
        values.append(building_total)
        total_energy += building_total

    return jsonify({
        "labels": labels,
        "values": values,
        "period": period,
        "period_label": period_label,
        "total_energy": total_energy,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    })


# ======================== STATS ========================
@app.route("/index/stats")
def get_stats():
    period = request.args.get('period', 'minggu')

    end_date = datetime.utcnow().replace(tzinfo=timezone.utc)
    if period == 'minggu':
        start_date = end_date - timedelta(days=7)
    else:
        start_date = end_date - timedelta(days=30)

    total_energy = 0.0
    total_cost = 0.0

    buildings_data = get_buildings_with_sensors()

    for building_name, info in buildings_data.items():
        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']

            # --- Query langsung tanpa view ---
            row = query_db_pg("""
                SELECT SUM(energy) AS energy, SUM(cost) AS cost
                FROM sensor_readings
                WHERE sensor_id = %s AND timestamp BETWEEN %s AND %s
            """, (sensor_id, start_date, end_date), one=True)

            if row:
                total_energy += float(row["energy"] or 0)
                total_cost += float(row["cost"] or 0)

    return jsonify({
        "period": period,
        "total_energy": total_energy,
        "total_cost": total_cost,
        "energy_formatted": f"{total_energy:,.4f} kWh",
        "cost_formatted": f"IDR {total_cost:,.0f}",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    })

# ------------------------ BACKEND DIGITAL TWIN ------------------------
# IP Address Pico
PICO_IP = "192.168.56.52"
PICO2_IP = "192.168.56.231"
# ======================== API PANEL ATAS ========================
@app.route("/pico1")
def pico1_dashboard():
    return render_template("digital_twin/dashboard.html")

# Proxy endpoint untuk fetch data dari Pico 1
@app.route("/api/pico1/data")
def get_pico1_data():
    try:
        response = requests.get(f"http://{PICO_IP}/data", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to fetch data from Pico"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# Proxy endpoint untuk kontrol relay Pico 1
@app.route("/api/pico1/relay/<device>/<int:state>")
def control_pico1_relay(device, state):
    try:
        response = requests.get(f"http://{PICO_IP}/relay/{device}/{state}", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to control relay"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500
    
# ======================== API PANEL 9 RELAY ========================
@app.route("/pico2")
def pico2_dashboard():
    return render_template("digital_twin/pzem6l24.html")

# Proxy endpoint untuk fetch data dari Pico 2
@app.route("/api/pico2/data")
def get_pico2_data():
    try:
        response = requests.get(f"http://{PICO2_IP}/data", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to fetch data from Pico 2"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# Proxy endpoint untuk kontrol relay Pico 2
@app.route("/api/pico2/relay/<device>/<int:state>")
def control_pico2_relay(device, state):
    try:
        response = requests.get(f"http://{PICO2_IP}/relay/{device}/{state}", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to control relay"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# Serve static files
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# ======================== API DIGITAL TWIN RESTROOM ========================
sensor_data_restroom = {
    'suhu': 0,
    'kelembapan': 0,
    'cahaya': 0,
    'gas': 0,
    'last_updated': 0
}
data_lock = Lock()

# =========================
# ESP32 → FLASK
# =========================
@app.route('/api/update_sensor', methods=['POST'])
def update_sensor():
    data = request.get_json()

    if not data:
        return jsonify({"status": "error", "message": "JSON kosong"}), 400

    with data_lock:
        sensor_data_restroom.update({
            'suhu': float(data.get('suhu', 0)),
            'kelembapan': float(data.get('kelembapan', 0)),
            'cahaya': float(data.get('cahaya', 0)),
            'gas': float(data.get('gas', 0)),
            'last_updated': time.time()
        })

    print("DATA MASUK:", sensor_data_restroom)
    return jsonify({"status": "success"})

# =========================
# FRONTEND → FLASK
# =========================
@app.route('/get_sensor_data')
def get_sensor_data():
    with data_lock:
        status = "online" if time.time() - sensor_data_restroom['last_updated'] < 30 else "offline"
        return jsonify({
            "status": status,
            **sensor_data_restroom
        })

# =========================
# ROUTES RESTROOM
# =========================
from digital_twin_restroom.routes import dt

app.register_blueprint(dt)


# ------------------------ MAIN STARTUP ------------------------
if __name__ == '__main__':
    # init pool
    init_db_pool()

    mqtt_client = start_mqtt(loop_forever=False)
    # start flush worker (flush every 60 seconds)
    threading.Thread(target=flush_worker, daemon=True).start()

    # Run Flask
    app.run(host="0.0.0.0", port=80, debug=True, use_reloader=False)