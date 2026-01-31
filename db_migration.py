import sqlite3

DB_PATH = "pzem.db"

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1. Tabel gedung (dengan code unik untuk MQTT topic)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS buildings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,   -- misalnya 'gedung1'
            name TEXT NOT NULL
        )
    """)

    # 2. Tabel sensor
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            building_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY (building_id) REFERENCES buildings(id) ON DELETE CASCADE
        )
    """)

    # 3. Tabel pembacaan sensor
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            voltage REAL,
            current REAL,
            power REAL,
            energy REAL,
            frequency REAL,
            power_factor REAL,
            cost REAL,
            FOREIGN KEY (sensor_id) REFERENCES sensors(id) ON DELETE CASCADE
        )
    """)

    # 4. Tabel daily_energy (agregasi harian)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_energy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            total_energy_kWh REAL,
            avg_power REAL,
            peak_power REAL,
            FOREIGN KEY (sensor_id) REFERENCES sensors(id) ON DELETE CASCADE,
            UNIQUE(sensor_id, date)
        )
    """)

    # Index untuk mempercepat query per sensor & waktu
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sensor_time
        ON sensor_readings (sensor_id, timestamp)
    """)

    conn.commit()
    conn.close()
    print("Migration selesai: tabel siap digunakan.")


def seed():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Cek apakah sudah ada data agar tidak double insert
    cur.execute("SELECT COUNT(*) FROM buildings")
    if cur.fetchone()[0] > 0:
        print("Seeder dilewati: data sudah ada.")
        conn.close()
        return

    # Daftar nama gedung (edit sesuai kebutuhanmu)
    building_names = [
        "Departement Pusat",
        "Departement Mesin",
        "Departement Elektronika",
        "Departement Otomotif",
        "Departement TI",
        "Departement Manajemen",
        "Departement Sipil"
    ]
    building_codes = [
        "department1",
        "department2",
        "department3",
        "department4",
        "department5",
        "department6",
        "department7"
    ]

    # Insert gedung + sensor
    for name in building_names:
        i = building_names.index(name)
        cur.execute("INSERT INTO buildings (name, code) VALUES (?, ?)", (name, building_codes[i]))
        building_id = cur.lastrowid

        # Tambah 3 sensor per gedung
        for j in range(1, 4):
            cur.execute(
                "INSERT INTO sensors (building_id, name) VALUES (?, ?)",
                (building_id, f"PZEM{j}")
            )

    conn.commit()
    conn.close()
    print("Seeder selesai: gedung & sensor berhasil dibuat.")


if __name__ == "__main__":
    migrate()
    seed()
