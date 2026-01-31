// Konfigurasi
const CONFIG = {
  API_URL: "/realtime",
  REFRESH_INTERVAL: 3000,
  CURRENCY: "IDR",
};

// Mapping PZEM ke fase (R, S, T)
const PZEM_TO_PHASE = {
  PZEM1: "r",
  PZEM2: "s",
  PZEM3: "t",
};

// Format angka dengan pemisah ribuan
function formatNumber(num) {
  return new Intl.NumberFormat("id-ID", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(num);
}

// Format currency
function formatCurrency(amount) {
  return new Intl.NumberFormat("id-ID", {
    style: "currency",
    currency: CONFIG.CURRENCY,
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(amount);
}

// Format nilai untuk display
function formatValue(value, metric) {
  switch (metric) {
    case "voltage":
      return `${formatNumber(value)} V`;
    case "current":
      return `${value.toFixed(2)} A`;
    case "power":
      return `${formatNumber(value)} W`;
    case "energy":
      return `${value.toFixed(4)} kWh`;
    default:
      return formatNumber(value);
  }
}

// Convert month
function formatMonth(month) {
  if (month === "January") {
    return "Januari";
  } else if (month === "February") {
    return "Februari";
  } else if (month === "March") {
    return "Maret";
  } else if (month === "April") {
    return "April";
  } else if (month === "May") {
    return "Mei";
  } else if (month === "June") {
    return "Juni";
  } else if (month === "July") {
    return "Juli";
  } else if (month === "August") {
    return "Agustus";
  } else if (month === "September") {
    return "September";
  } else if (month === "October") {
    return "Oktober";
  } else if (month === "November") {
    return "November";
  } else if (month === "December") {
    return "Desember";
  }
}

// Update single cell
function updateCell(deptId, phase, metric, value) {
  const cell = document.querySelector(
    `.bar[data-dept="${deptId}"][data-phase="${phase}"][data-metric="${metric}"]`
  );

  if (cell) {
    cell.textContent = formatValue(value, metric);
  }
}

// Update footer (total energy & cost) untuk setiap departemen
function updateDepartmentFooter(deptId, totalEnergy, totalCost) {
  // Cari footer berdasarkan data-dept
  const footerItems = document.querySelectorAll(
    `.footer-icon[data-dept="${deptId}"]`
  );

  footerItems.forEach((icon) => {
    const metric = icon.getAttribute("data-metric");
    const valueSpan = icon.parentElement.querySelector("span:last-child");

    if (valueSpan) {
      if (metric === "total_energy") {
        valueSpan.textContent = `${totalEnergy.toFixed(3)} kWh`;
      } else if (metric === "total_cost") {
        valueSpan.textContent = formatCurrency(totalCost);
      }
    }
  });
}

// Update summary card (total bulan)
function updateSummary(summary) {
  // Hitung total dari semua departemen
  let totalEnergy = summary.overall_energy || 0;
  let totalCost = summary.overall_cost || 0;
  console.log(totalEnergy, totalCost);
  let month = summary.month || "";
  let year = summary.year || "";

  const summaryValues = document.querySelectorAll(
    ".summary-card .summary-value"
  );
  const summaryMonth = document.querySelector(".summary-header .summary-month");
  const summaryYear = document.querySelector(".summary-header .summary-year");

  if (summaryValues.length >= 2) {
    summaryValues[0].textContent = `${formatNumber(totalEnergy)} kWh`;
    summaryValues[1].textContent = formatCurrency(totalCost);
  }
  summaryMonth.textContent = formatMonth(month);
  summaryYear.textContent = year;
}

// Update semua data dari response JSON
function updateAllData(data) {
  if (!data.departments || !Array.isArray(data.departments)) {
    console.error("Invalid data format:", data);
    return;
  }

  const { departments } = data;
  console.log("departments:", departments);

  // Update setiap departemen
  departments.forEach((dept) => {
    const deptId = dept.id;
    const phases = dept.phases;

    if (!phases) return;

    // Mapping PZEM1, PZEM2, PZEM3 ke fase r, s, t
    Object.keys(PZEM_TO_PHASE).forEach((pzemKey) => {
      const phase = PZEM_TO_PHASE[pzemKey];
      const pzemData = phases[pzemKey];

      if (pzemData) {
        // Update voltage, current, power, energy
        // console.log(`Updating ${deptId} ${phase} data:`, pzemData);
        updateCell(deptId, phase, "voltage", pzemData.voltage || 0);
        updateCell(deptId, phase, "current", pzemData.current || 0);
        updateCell(deptId, phase, "power", pzemData.power || 0);
        updateCell(deptId, phase, "energy", pzemData.energy || 0);
      }
    });

    // Update footer departemen
    if (dept.total) {
      updateDepartmentFooter(
        deptId,
        dept.total.total_energy || 0,
        dept.total.total_cost || 0
      );
    }
  });

  // Update summary
  updateSummary(data.summary);

  // Update timestamp terakhir
  updateLastUpdate();
}

// Update timestamp terakhir update
function updateLastUpdate() {
  const lastUpdateEl = document.getElementById("last-update");
  if (lastUpdateEl) {
    const date = new Date();
    lastUpdateEl.textContent = `Terakhir update: ${date.toLocaleString(
      "id-ID"
    )}`;
  }
}

// Fetch data dari API
async function fetchData() {
  try {
    const response = await fetch(CONFIG.API_URL);

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    // console.log('Data received:', data);
    updateAllData(data);

    // Hapus indicator loading jika ada
    hideLoading();
  } catch (error) {
    console.error("Error fetching data:", error);
    showError("Gagal mengambil data. Akan mencoba lagi...");
  }
}
// Fetch summary data dari API
async function fetchSummaryData() {
  try {
    const response = await fetch("/index/prediksi");

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    // console.log('Data received summary:', data);
    document.getElementById("predict-value").textContent = `Rp ${formatNumber(data.cost_pred)}`;
     
  } catch (error) {
    console.error("Error fetching data:", error);
    showError("Gagal mengambil data summary. Akan mencoba lagi...");
  }
}

// Show loading indicator
function showLoading() {
  const container = document.querySelector(".container");
  if (!container.querySelector(".loading-indicator")) {
    const loading = document.createElement("div");
    loading.className = "loading-indicator";
    loading.style.cssText = `
      position: fixed;
      top: 10px;
      right: 10px;
      background: #4299e1;
      color: white;
      padding: 10px 20px;
      border-radius: 5px;
      font-size: 12px;
      z-index: 1000;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    `;
    loading.innerHTML =
      '<i class="fa-solid fa-spinner fa-spin"></i> Memuat data...';
    container.appendChild(loading);
  }
}

// Hide loading indicator
function hideLoading() {
  const loading = document.querySelector(".loading-indicator");
  if (loading) {
    loading.remove();
  }
}

// Show error message
function showError(message) {
  const container = document.querySelector(".container");
  let errorEl = container.querySelector(".error-indicator");

  if (!errorEl) {
    errorEl = document.createElement("div");
    errorEl.className = "error-indicator";
    errorEl.style.cssText = `
      position: fixed;
      top: 10px;
      right: 10px;
      background: #f56565;
      color: white;
      padding: 10px 20px;
      border-radius: 5px;
      font-size: 12px;
      z-index: 1000;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    `;
    container.appendChild(errorEl);
  }

  errorEl.innerHTML = `<i class="fa-solid fa-exclamation-triangle"></i> ${message}`;

  // Auto hide after 5 seconds
  setTimeout(() => {
    errorEl.remove();
  }, 5000);
}

// Show success indicator
function showSuccess() {
  const container = document.querySelector(".container");
  let successEl = container.querySelector(".success-indicator");

  if (!successEl) {
    successEl = document.createElement("div");
    successEl.className = "success-indicator";
    successEl.style.cssText = `
      position: fixed;
      top: 10px;
      right: 10px;
      background: #48bb78;
      color: white;
      padding: 10px 20px;
      border-radius: 5px;
      font-size: 12px;
      z-index: 1000;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    `;
    container.appendChild(successEl);
  }

  successEl.innerHTML =
    '<i class="fa-solid fa-check-circle"></i> Data berhasil diperbarui';

  // Auto hide after 2 seconds
  setTimeout(() => {
    successEl.remove();
  }, 2000);
}

// Start auto-refresh
function startAutoRefresh() {
  // Fetch pertama kali
  fetchData();
  fetchSummaryData();

  // Setup interval untuk refresh otomatis
  const intervalId = setInterval(fetchData, CONFIG.REFRESH_INTERVAL);

  // console.log(
  //   `Auto-refresh started (every ${CONFIG.REFRESH_INTERVAL / 1000} seconds)`
  // );

  // Return interval ID untuk bisa di-stop jika diperlukan
  return intervalId;
}

// Stop auto-refresh (untuk testing atau debugging)
let autoRefreshInterval = null;

function stopAutoRefresh() {
  if (autoRefreshInterval) {
    clearInterval(autoRefreshInterval);
    console.log("Auto-refresh stopped");
  }
}

// Initialize ketika DOM ready
document.addEventListener("DOMContentLoaded", function () {
  // console.log("Electricity Monitoring System initialized");
  // console.log("Configuration:", CONFIG);

  // Start fetching data
  showLoading();
  autoRefreshInterval = startAutoRefresh();

  // Optional: Manual refresh button
  const refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      showLoading();
      fetchData();
    });
  }

  // Keyboard shortcut: Press R to refresh
  document.addEventListener("keydown", (e) => {
    if (e.key === "r" || e.key === "R") {
      if (!e.ctrlKey && !e.metaKey) {
        // Hindari conflict dengan Ctrl+R
        showLoading();
        fetchData();
      }
    }
  });
});

// Handle visibility change (pause saat tab tidak aktif)
document.addEventListener("visibilitychange", function () {
  if (document.hidden) {
    console.log("Tab hidden - pausing refresh");
    stopAutoRefresh();
  } else {
    console.log("Tab visible - resuming refresh");
    autoRefreshInterval = startAutoRefresh();
  }
});

// Export functions untuk testing atau penggunaan manual
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    fetchData,
    fetchSummaryData,
    updateAllData,
    formatCurrency,
    formatNumber,
    startAutoRefresh,
    stopAutoRefresh,
  };
}
