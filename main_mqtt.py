import threading
import time
from flask import Flask, jsonify, request, render_template
import sqlite3
from datetime import datetime, timedelta
import json
import paho.mqtt.client as mqtt

# ----------------------- CONFIG -----------------------
app = Flask(__name__)

BROKER = "10.1.1.55"
PORT = 1883
TOPIC_PATTERN = "sensor/#"  # semua sensor
TOPIC_PREDICT = "predict/pub"
TOPIC_PREDICT_RESULT = "predict/result"

DB_NAME = "pzem.db"

# Menyimpan data terakhir dari setiap topic
latest_data = {}

# -------------------- THREAD SAFETY --------------------
db_write_lock = threading.Lock()
MODEL = None
MODEL_LOCK = threading.Lock()

# --------------------- DATABASE ------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def query_db(query, args=(), one=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, args)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows

def get_sensor_id_from_topic(topic: str):
    """Parse topic sensor/<building_code>/<sensor_name> -> sensor_id"""
    try:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "sensor":
            return None
        building_code, sensor_name = parts[1], parts[2]

        row = query_db("""
            SELECT s.id
            FROM sensors s
            JOIN buildings b ON s.building_id = b.id
            WHERE b.code = ? AND s.name = ?
        """, (building_code, sensor_name), one=True)

        return row["id"] if row else None
    except Exception as e:
        print("Error get_sensor_id_from_topic:", e)
        return None

def save_sensor_data(sensor_id: int, data: dict):
    """Simpan data sensor ke tabel sensor_readings"""
    required = ('tegangan', 'arus', 'daya', 'energi', 'frekuensi', 'biaya', 'tanggal', 'pf')
    if not all(k in data for k in required):
        raise ValueError("Data sensor tidak lengkap saat save_sensor_data")

    with db_write_lock:
        conn = get_db_connection()
        try:
            conn.execute("""
                INSERT INTO sensor_readings 
                (sensor_id, timestamp, voltage, current, power, energy, frequency, cost, power_factor) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sensor_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                round(data['tegangan'], 3),
                round(data['arus'], 3),
                round(data['daya'], 3),
                round(data['energi'], 7),
                round(data['frekuensi'], 3),
                data['biaya'],
                round(data['pf'], 3)
            ))
            conn.commit()
            print(f"Data disimpan untuk sensor_id {sensor_id}: {data}")
        finally:
            conn.close()

# ------------------- AGGREGATION BUFFER -------------------
agg_buffer = {}
agg_lock = threading.Lock()

INTERVAL_SEC = 3
TARIF_PER_KWH = 1500
PPJ = 0.10  # 10%

def accumulate_sensor_data(sensor_id: int, data: dict):
    """Akumulasi data sensor sampai di-flush worker"""
    with agg_lock:
        if sensor_id not in agg_buffer:
            agg_buffer[sensor_id] = {
                "sums": {k: 0.0 for k in ['tegangan', 'arus', 'daya', 'energi', 'frekuensi', 'biaya', 'pf']},
                "count": 0
            }

        buf = agg_buffer[sensor_id]

        try:
            daya = float(data.get('daya', 0.0))
        except Exception:
            daya = 0.0

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
            "daya": sums['daya'],
            "energi": sums['energi'],
            "frekuensi": sums['frekuensi'] / count,
            "biaya": sums['biaya'],
            "tanggal": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pf": sums['pf'] / count
        }
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
            flush_buffer(sensor_id)

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
    rows = query_db("""
        SELECT b.id as building_id, b.name as building_name, b.code as building_code,
               s.id as sensor_id, s.name as sensor_name
        FROM buildings b
        LEFT JOIN sensors s ON b.id = s.building_id
        ORDER BY b.id
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

    now = datetime.now()
    # Awal bulan (contoh: 2025-10-01 00:00:00)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Akhir bulan (tanggal terakhir bulan ini)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
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

            # Tentukan fase
            phase_key = sensor['sensor_name'][-1].lower() if sensor['sensor_name'][-1].lower() in ['r', 's', 't'] else sensor['sensor_name']

            # Default nilai jika belum ada data MQTT
            if not sensor_data:
                sensor_data = {"tegangan": 0, "arus": 0, "daya": 0, "energi": 0}

            # Data realtime
            phases[phase_key] = {
                "voltage": float(sensor_data.get("tegangan", 0.0)),
                "current": float(sensor_data.get("arus", 0.0)),
                "power": float(sensor_data.get("daya", 0.0)),
                "energy": float(sensor_data.get("energi", 0.0))
            }

            # Ambil data bulanan dari DB
            row = query_db("""
                SELECT 
                    SUM(energy) as total_energy,
                    SUM(cost) as total_cost
                FROM sensor_readings
                WHERE sensor_id = ?
                AND timestamp BETWEEN ? AND ?
            """, (
                sensor['sensor_id'],
                month_start.strftime("%Y-%m-%d %H:%M:%S"),
                month_end.strftime("%Y-%m-%d %H:%M:%S")
            ), one=True)

            if row:
                total_energy += row["total_energy"] or 0
                total_cost += row["total_cost"] or 0

        # Total per departemen/gedung
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

    # ringkasan bulan berjalan
    summary = {
        "overall_energy": round(total_energy_all, 1),
        "overall_cost": round(total_cost_all, 0),
        "month": now.strftime("%B"),   # nama bulan (Oktober)
        "year": now.year
    }

    return jsonify({
        "success": True,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "departments": departments,
        "summary": summary
    })

# ------------------------ DASHBOARD ADMIN API ------------------------
@app.route("/admin")
def admin_page():
    """Render halaman dashboard"""
    return render_template("realtime_fetch.html")

@app.route("/dashboard-admin", methods=["GET"])
def get_dashboard():
    """
    Realtime data rata-rata sensor per gedung.
    Ambil dari latest_data yang diupdate MQTT, kelompokkan berdasarkan building_code.
    """
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
                    "sums": {k: 0.0 for k in sensor_data.keys()},
                    "count": 0
                }

            stats = building_stats[building_name]
            stats["count"] += 1

            if field:
                if field.lower() in ["energi", "energy"]:
                    daya_val = float(sensor_data.get("daya", 0) or 0)
                    energi_val = (daya_val * 3) / 3600 / 1000

                    # Simpan daya juga supaya tidak hilang
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
                    averaged[k] = round(daya_total, 3)  # total daya
                elif key in ["energi", "energy"]:
                    energi_val = (daya_total * 3) / 3600 / 1000
                    if energi_val > 0 and energi_val < 0.001:
                        averaged[k] = float(f"{energi_val:.7e}")  # tampilkan scientific notation
                    else:
                        averaged[k] = round(energi_val, 6)        # tampilkan normal
                else:
                    averaged[k] = round(v / count, 3)  # rata-rata

            results[building_name] = averaged
        else:
            results[building_name] = None

    return jsonify(results)

# ------------------------ QUERY HELPER ------------------------
def query_db(query, args=(), one=False):
    """Helper function untuk query database"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, args)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows

# ------------------------ ENERGY USAGE API ------------------------
@app.route("/index/energy-usage")
def energy_usage():
    """Get energy usage untuk line chart (per gedung, jumlah dari semua sensor)"""
    buildings_data = get_buildings_with_sensors()
    datasets = []
    labels = []

    for building_name, info in buildings_data.items():
        # Dictionary untuk menjumlahkan energi berdasarkan jam
        energy_per_time = {}

        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']

            rows = query_db("""
                SELECT strftime('%H', timestamp) as jam, energy
                FROM sensor_readings 
                WHERE sensor_id = ?
                ORDER BY timestamp ASC
                LIMIT 60
            """, (sensor_id,))

            for row in rows:
                jam = row["jam"]
                energi = row["energy"] or 0

                if jam not in energy_per_time:
                    energy_per_time[jam] = 0
                energy_per_time[jam] += energi

        # urutkan berdasarkan jam biar rapi
        sorted_times = sorted(energy_per_time.keys())
        usage = [energy_per_time[j] for j in sorted_times]

        datasets.append({
            "building": building_name,
            "usage": usage
        })

        # gunakan label dari gedung pertama sebagai acuan
        if not labels and sorted_times:
            labels = sorted_times

    return jsonify({
        "labels": labels,
        "datasets": datasets
    })


# ======================== PIE CHART ========================
@app.route("/index/energy-pie")
def energy_pie():
    """Get energy distribution untuk pie chart"""
    period = request.args.get('period', 'minggu')
    
    end_date = datetime.now()
    if period == 'minggu':
        start_date = end_date - timedelta(days=7)
        period_label = "Minggu Ini"
    else:  # bulan
        start_date = end_date - timedelta(days=30)
        period_label = "Bulan Ini"
    
    start_date_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
    end_date_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
    
    labels = []
    values = []
    total_energy = 0

    buildings_data = get_buildings_with_sensors()
    
    for building_name, info in buildings_data.items():
        building_total = 0
        
        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']
            
            try:
                row = query_db("""
                    SELECT SUM(energy) as total
                    FROM sensor_readings
                    WHERE sensor_id = ? AND timestamp >= ? AND timestamp <= ?
                """, (sensor_id, start_date_str, end_date_str), one=True)

                if row and row["total"] is not None:
                    building_total += row["total"]
                    
            except Exception as e:
                print(f"Error querying sensor {sensor_id}: {e}")
        
        labels.append(building_name)
        values.append(building_total)
        total_energy += building_total

    return jsonify({
        "labels": labels,
        "values": values,
        "period": period,
        "period_label": period_label,
        "total_energy": total_energy,
        "start_date": start_date_str,
        "end_date": end_date_str
    })

# ======================== STATS ========================
@app.route("/index/stats")
def get_stats():
    """Get statistics untuk dashboard"""
    period = request.args.get('period', 'minggu')
    
    end_date = datetime.now()
    if period == 'minggu':
        start_date = end_date - timedelta(days=7)
    else:  # bulan
        start_date = end_date - timedelta(days=30)
    
    start_date_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
    end_date_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
    
    total_energy = 0
    total_cost = 0
    
    # Calculate total across all sensors
    buildings_data = get_buildings_with_sensors()
    
    for building_name, info in buildings_data.items():
        for sensor_info in info['sensors']:
            sensor_id = sensor_info['sensor_id']
            
            try:
                row = query_db("""
                    SELECT SUM(energy) as energy, SUM(cost) as cost
                    FROM sensor_readings
                    WHERE sensor_id = ? AND timestamp >= ? AND timestamp <= ?
                """, (sensor_id, start_date_str, end_date_str), one=True)
                
                if row:
                    if row["energy"]:
                        total_energy += row["energy"]
                    if row["cost"]:
                        total_cost += row["cost"]
                    
            except Exception as e:
                print(f"Error querying stats from sensor {sensor_id}: {e}")
    
    return jsonify({
        "period": period,
        "total_energy": total_energy,
        "total_cost": total_cost,
        "energy_formatted": f"{total_energy:,.4f} kWh",
        "cost_formatted": f"IDR {total_cost:,.0f}",
        "start_date": start_date_str,
        "end_date": end_date_str
    })
    
    
# ------------------------ MAIN STARTUP ------------------------
if __name__ == '__main__':
    mqtt_client = start_mqtt(loop_forever=False)
    threading.Thread(target=flush_worker, args=(60,), daemon=True).start()
    app.run(host="0.0.0.0", port=80, debug=True, use_reloader=False)
