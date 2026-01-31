# server_pzem_timescale.py
import threading
import time
from flask import Flask, jsonify, request, render_template
from datetime import datetime, timedelta, timezone
import json
import paho.mqtt.client as mqtt
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# ----------------------- CONFIG -----------------------
app = Flask(__name__)

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

# connection pool (Thread-safe)
DB_POOL_MINCONN = 1
DB_POOL_MAXCONN = 10
db_pool = None

# Menyimpan data terakhir dari setiap topic
latest_data = {}

# -------------------- THREAD SAFETY --------------------
db_write_lock = threading.Lock()
MODEL = None
MODEL_LOCK = threading.Lock()

# --------------------- DATABASE HELPERS ------------------------
def init_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = ThreadedConnectionPool(
            DB_POOL_MINCONN, DB_POOL_MAXCONN,
            dbname=DB_CONFIG["dbname"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"]
        )

def get_conn():
    """Get connection from pool; caller must put it back via putconn."""
    if db_pool is None:
        init_db_pool()
    return db_pool.getconn()

def put_conn(conn):
    if db_pool:
        db_pool.putconn(conn)

def query_db_pg(query, args=(), one=False):
    """
    Run SELECT query and return rows as list of dicts (RealDictCursor).
    """
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, args)
        rows = cur.fetchall()
        cur.close()
        return (rows[0] if rows else None) if one else rows
    finally:
        put_conn(conn)

def execute_db_pg(query, args=()):
    """
    Run INSERT/UPDATE/DDL. Returns None. Uses connection commit.
    """
    # Use a dedicated connection with commit
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, args)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("DB Error:", e)
    finally:
        cur.close()
        put_conn(conn)

# ------------------- MQTT / SENSOR LOGIC -------------------
def get_sensor_id_from_topic(topic: str):
    """Parse topic sensor/<building_code>/<sensor_name> -> sensor_id"""
    try:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "sensor":
            return None
        building_code, sensor_name = parts[1], parts[2]

        row = query_db_pg("""
            SELECT s.id
            FROM sensors s
            JOIN buildings b ON s.building_id = b.id
            WHERE b.code = %s AND s.name = %s
            LIMIT 1
        """, (building_code, sensor_name), one=True)

        return row["id"] if row else None
    except Exception as e:
        print("Error get_sensor_id_from_topic:", e)
        return None

def save_sensor_data(sensor_id: int, data: dict):
    """Simpan data sensor ke tabel sensor_readings (hypertable)"""
    required = ('tegangan', 'arus', 'daya', 'frekuensi', 'tanggal', 'pf','biaya', 'energi')
    if not all(k in data for k in required):
        raise ValueError("Data sensor tidak lengkap saat save_sensor_data")

    # Use UTC timestamp; sensor timestamp field in DB is TIMESTAMPTZ
    ts = datetime.utcnow().replace(tzinfo=timezone.utc)

    with db_write_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sensor_readings
                (sensor_id, timestamp, voltage, current, power, energy, frequency, cost, power_factor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sensor_id, timestamp) DO UPDATE
                  SET voltage = EXCLUDED.voltage,
                      current = EXCLUDED.current,
                      power = EXCLUDED.power,
                      energy = EXCLUDED.energy,
                      frequency = EXCLUDED.frequency,
                      cost = EXCLUDED.cost,
                      power_factor = EXCLUDED.power_factor
            """, (
                sensor_id,
                ts,
                float(data['tegangan']),
                float(data['arus']),
                float(data['daya']),
                float(data['energi']),
                float(data['frekuensi']),
                float(data['biaya']),
                float(data['pf'])
            ))
            conn.commit()
            cur.close()
            print(f"Data disimpan untuk sensor_id {sensor_id}: {data}")
        finally:
            put_conn(conn)

# ------------------- AGGREGATION BUFFER -------------------
agg_buffer = {}
agg_lock = threading.Lock()

INTERVAL_SEC = 10
TARIF_PER_KWH = 1500.0
PPJ = 0.05  # 10%

def accumulate_sensor_data(sensor_id: int, data: dict):
    """Akumulasi data sensor sampai di-flush worker"""
    with agg_lock:
        if sensor_id not in agg_buffer:
            agg_buffer[sensor_id] = {
                "sums": {k: 0.0 for k in ['tegangan', 'arus', 'daya', 'frekuensi', 'pf', 'energi', 'biaya']},
                "count": 0
            }

        buf = agg_buffer[sensor_id]

        try:
            daya = float(data.get('daya', 0.0))
        except Exception:
            daya = 0.0

        # energi in kWh for INTERVAL_SEC seconds: daya (W) * seconds / 3600 / 1000
        energi_wh = daya * (INTERVAL_SEC / 3600.0)
        energi_kwh = energi_wh / 1000.0
        biaya = energi_kwh * TARIF_PER_KWH * (1 + PPJ)

        for k in ['tegangan', 'arus', 'daya', 'frekuensi', 'pf']:
            try:
                v = float(data.get(k, 0.0))
            except Exception:
                v = 0.0
            buf['sums'][k] += v

        buf['sums']['energi'] += energi_kwh
        buf['sums']['biaya'] += biaya
        buf['count'] += 1

        print(f"Akumulasi sementara sensor_id {sensor_id}: samples={buf['count']} sums={buf['sums']}")

def flush_buffer(sensor_id: int):
    """Flush buffer untuk sensor tertentu ke database"""
    with agg_lock:
        if sensor_id not in agg_buffer:
            return
        buf = agg_buffer[sensor_id]
        count = buf.get('count', 0)
        if count == 0:
            return
        sums = buf['sums']
        payload = {
            "tegangan": sums['tegangan'] / count,
            "arus": sums['arus'] / count,
            "daya": sums['daya'],          # total daya samples sum (as previously)
            "energi": sums['energi'],      # accumulated kWh
            "frekuensi": sums['frekuensi'] / count,
            "biaya": sums['biaya'],
            "tanggal": datetime.utcnow().replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "pf": sums['pf'] / count
        }
        # reset buffer
        agg_buffer[sensor_id] = {
            "sums": {k: 0.0 for k in ['tegangan', 'arus', 'daya', 'energi', 'frekuensi', 'biaya', 'pf']},
            "count": 0
        }

    save_sensor_data(sensor_id, payload)
    print(f"Buffer sensor_id {sensor_id} diflush ke DB (count={count}).")

def flush_worker(interval: int = 60):
    while True:
        time.sleep(interval)
        for sensor_id in list(agg_buffer.keys()):
            try:
                flush_buffer(sensor_id)
            except Exception as e:
                print("Error saat flush_buffer:", e)

# ---------------------- MQTT HANDLER -------------------
def handle_sensor_message(sensor_id: int, data: dict):
    try:
        accumulate_sensor_data(sensor_id, data)
    except Exception as e:
        print("Gagal akumulasi data sensor:", e)

def handle_message(topic: str, data: dict, client: mqtt.Client):
    latest_data[topic] = data
    sensor_id = get_sensor_id_from_topic(topic)
    if sensor_id:
        handle_sensor_message(sensor_id, data)
    elif topic == TOPIC_PREDICT:
        # handle predict topic if needed
        pass
    else:
        print("Topik tidak dikenali:", topic)

# ---------------------- MQTT CALLBACK ------------------
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT Broker")
        client.subscribe([(TOPIC_PATTERN, 0), (TOPIC_PREDICT, 0)])
    else:
        print("MQTT connect failed with rc:", rc)

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        data = json.loads(payload)
        handle_message(msg.topic, data, client)
    except Exception as e:
        print("Error di on_message:", e)

def start_mqtt(loop_forever=False):
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_start()
    return client

# ------------------------ GET BUILDINGS & SENSORS ------------------------
def get_buildings_with_sensors():
    rows = query_db_pg("""
        SELECT b.id as building_id, b.name as building_name, b.code as building_code,
               s.id as sensor_id, s.name as sensor_name
        FROM buildings b
        LEFT JOIN sensors s ON b.id = s.building_id
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

# ------------------------ MAIN STARTUP ------------------------
def seed_if_empty():
    """Optional helper to seed buildings & sensors if empty (safe)."""
    # check
    row = query_db_pg("SELECT COUNT(*) AS cnt FROM buildings;", one=True)
    if row and int(row['cnt']) > 0:
        print("Seeder skipped: buildings already exist.")
        return

    building_data = [
        ("Departement Pusat", "department1"),
        ("Departement Mesin", "department2"),
        ("Departement Elektronika", "department3"),
        ("Departement Otomotif", "department4"),
        ("Departement TI", "department5"),
        ("Departement Manajemen", "department6"),
        ("Departement Sipil", "department7")
    ]

    for name, code in building_data:
        execute_db_pg("INSERT INTO buildings (name, code) VALUES (%s, %s);", (name, code))
        # get id
        row = query_db_pg("SELECT id FROM buildings WHERE code = %s LIMIT 1;", (code,), one=True)
        building_id = row['id']
        # insert sensors
        sensor_rows = [(building_id, f"PZEM{i}") for i in range(1, 4)]
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.executemany("INSERT INTO sensors (building_id, name) VALUES (%s, %s);", sensor_rows)
            conn.commit()
            cur.close()
        finally:
            put_conn(conn)

    print("Seeder complete: buildings & sensors created.")

if __name__ == '__main__':
    # init pool + seed
    init_db_pool()
    seed_if_empty()

    mqtt_client = start_mqtt(loop_forever=False)
    # start flush worker (flush every 60 seconds)
    threading.Thread(target=flush_worker, args=(60,), daemon=True).start()

    # Run Flask
    app.run(host="0.0.0.0", port=80, debug=True, use_reloader=False)