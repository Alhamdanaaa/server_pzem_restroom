// Struktur dataset kosong (akan diisi dari backend)
const chartData = {
    kWh: { labels: [], data: [[], [], [], []] }, 
    V: { labels: [], data: [[], [], [], []] },
    A: { labels: [], data: [[], [], [], []] },
    Hz: { labels: [], data: [[], [], [], []] }
};

// Warna per ruang
const colors = ['#ef4444', '#f59e0b', '#10b981', '#0ea5e9'];

// Inisialisasi grafik kosong
const hourlyCtx = document.getElementById('hourlyChart').getContext('2d');
let hourlyChart = new Chart(hourlyCtx, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { 
                display: true, 
                position: 'bottom', 
                labels: { 
                    color: '#ffffffff',
                    usePointStyle: true,
                    pointStyle: 'circle',
                    padding: 20,  
                    font: {size: 14, weight: 'bold' } 
                } 
            }
        },
        scales: {
            x: { ticks: { color: '#94a3b8' }, grid: { color: '#94a3b8'} },
            y: { ticks: { color: '#94a3b8' }, grid: { color: '#94a3b8', drawBorder: false } }
        }
    }
});

// === Load Hourly Chart dari Flask ===
// === Load Hourly Chart dari Flask ===
function loadHourlyChart() {
    fetch('/get_hourly')
        .then(res => res.json())
        .then(data => {
            const maxPoints = 30; // 30 titik terakhir (anggap 1 titik = 1 menit)

            // Simpan semua unit ke chartData, tapi langsung slice
            ["kWh", "V", "A", "Hz"].forEach(unit => {
                const labels = data.labels.slice(-maxPoints);
                const datasets = [
                    (data[unit]["Ruang 1"] || []).slice(-maxPoints),
                    (data[unit]["Ruang 2"] || []).slice(-maxPoints),
                    (data[unit]["Ruang 3"] || []).slice(-maxPoints),
                    (data[unit]["Ruang 4"] || []).slice(-maxPoints)
                ];

                chartData[unit].labels = labels;
                chartData[unit].data = datasets;
            });

            // Default tampilkan kWh
            hourlyChart.data.labels = chartData.kWh.labels;
            hourlyChart.data.datasets = chartData.kWh.data.map((dataset, i) => ({
                label: `Ruang ${i + 1}`,
                data: dataset,
                borderColor: colors[i],
                backgroundColor: colors[i],
                fill: false,
                tension: 0.4,
                borderWidth: 2
            }));
            hourlyChart.update();

            // Update widget (pakai kWh default)
            chartData.kWh.data.forEach((dataset, i) => {
                let latestValue = dataset.length ? dataset[dataset.length - 1] : "--";
                document.getElementById(`r${i + 1}`).textContent = latestValue;
            });
        })
        .catch(err => console.error("Error hourly:", err));
}

// --- HARIAN ---
function updateAverageProgressHari(totalHarian) {
    const avg = totalHarian / 4;
    const maxValue = 300;
    let percent = (avg / maxValue) * 100;
    if (percent > 100) percent = 100;

    document.getElementById("avgProgress").style.width = percent + "%";
    document.getElementById("avgValue").textContent = avg.toFixed(2) + " kWh / 300.00 kWh";
}

function updateFromElementHari() {
    let textValue = document.getElementById("totaldayaharian").innerText; 
    let numberValue = parseFloat(textValue);
    updateAverageProgressHari(numberValue);
}

// --- BULANAN ---
function updateAverageProgressBulan(totalPemakaian1Bulan) {
    const avg = totalPemakaian1Bulan;
    const maxValue = 7500;
    let percent = (avg / maxValue) * 100;
    if (percent > 100) percent = 100;

    document.getElementById("avgProgressbulan").style.width = percent + "%";
    document.getElementById("avgValuebulan").textContent = avg.toFixed(2) + " kWh / 7500.00 kWh";
}

function updateFromElementBulan() {
    let textValue = document.getElementById("totaldayabulanan").innerText; 
    let numberValue = parseFloat(textValue);
    updateAverageProgressBulan(numberValue);
}

// --- Trigger awal ---
updateFromElementHari();
updateFromElementBulan();


// === Load Pie Chart ===
function loadPieChart() {
    fetch("/get_pie")
        .then(res => res.json())
        .then(data => {
            const ctx = document.getElementById("pieRoomChart").getContext("2d");
            if (window.pieRoomChartInstance) {
                window.pieRoomChartInstance.destroy();
            }
            window.pieRoomChartInstance = new Chart(ctx, {
                type: "doughnut",
                data: {
                    labels: data.labels,
                    datasets: [{
                        data: data.values,
                        backgroundColor: ["#ef4444","#f59e0b","#10b981","#0ea5e9"]
                    }]
                },
                options: { 
                    responsive: true,
                    plugins: {
                        legend: {
                            display: false   // <-- matikan legend bawaan
                        }
                    }
                 }
            });
        });
}

// === Update Totals Bulanan ===
function updateTotals() {
    fetch('/get_totals')
        .then(res => res.json())
        .then(data => {
            console.log("DEBUG /get_totals:", data); // cek kenapa nilai besar
            document.getElementById("totaldayabulanan").innerText =
                Number(data.total_energi).toFixed(2) + " kWh";
            document.getElementById("tagihanbulan").innerText =
                "Rp " + Number(data.total_tagihan).toLocaleString("id-ID");
        })
        .catch(err => console.error("Error totals:", err));
}


// === Dropdown Unit ===
const unitSelect = document.getElementById("unitSelect");
const unitElements = document.querySelectorAll(".device-unit");

unitSelect.addEventListener("change", function () {
    let selectedUnit = unitSelect.value;
    let unitData = chartData[selectedUnit];

    // Update chart pakai data dari chartData
    hourlyChart.data.labels = unitData.labels;
    hourlyChart.data.datasets = unitData.data.map((dataset, i) => ({
        label: `Ruang ${i + 1}`,
        data: dataset,
        borderColor: colors[i],
        backgroundColor: colors[i],
        fill: false,
        tension: 0.4,
        borderWidth: 2
    }));
    hourlyChart.update();

    // Update widget ruang
    unitData.data.forEach((dataset, i) => {
        let latestValue = dataset.length ? dataset[dataset.length - 1] : "--";
        document.getElementById(`r${i + 1}`).textContent = latestValue;
        unitElements[i].textContent = selectedUnit;
    });
});

function updateDaily() {
    fetch('/get_daily')
        .then(res => res.json())
        .then(data => {
            document.getElementById("totaldayaharian").innerText =
                Number(data.total_energi || 0).toFixed(2) + " kWh";
            document.getElementById("biayaharian").innerText =
                "Rp " + Number(data.total_tagihan || 0).toLocaleString("id-ID");

            console.log("DEBUG /get_daily:", data);
        })
        .catch(err => console.error("Error daily:", err));
}

// === Jalankan saat halaman load ===
document.addEventListener("DOMContentLoaded", () => {
    updateTotals();
    updateDaily();   // <=== tambahin ini
    loadHourlyChart();
    loadPieChart();

    setInterval(updateTotals, 6000);
    setInterval(updateDaily, 6000);  // <=== refresh otomatis
    setInterval(loadHourlyChart, 60000);
    setInterval(loadPieChart, 60000);
});

