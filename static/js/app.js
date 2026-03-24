/* app.js — ESP Line Logger Dashboard */

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  device:          "",
  lineActive:      false,
  lineStartTs:     null,
  lastHeartbeat:   null,
  durationTimer:   null,
  selectedDowntime: null, // {start_ts, stop_ts, reason, comment}
  speedChart:      null,  // Chart.js instance
};

// ── WebSocket ──────────────────────────────────────────────────────────────
let ws = null;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    setDot("connected");
    addLog("WebSocket підключено", "tcp");
  };

  ws.onclose = () => {
    setDot("error");
    addLog("WebSocket відключено — повтор через 3с...", "warn");
    setTimeout(connectWS, 3000);
  };

  ws.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch { }
  };
}

// ── Event handler ──────────────────────────────────────────────────────────
function handleEvent(data) {
  const type = data.event_type;

  if (type === "__log__") {
    addLog(data.msg, data.msg.includes("Помилка") ? "err" : "tcp");
    return;
  }

  if (state.device && data.device_id !== state.device) return;

  addLog(`[${type}] ${data.device_id} ts=${data.ts}`
    + (data.dur  ? ` dur=${data.dur}s`  : "")
    + (data.rssi ? ` rssi=${data.rssi}` : ""), "");

  if (type === "line_start") {
    state.lineActive  = true;
    state.lineStartTs = data.ts || Math.floor(Date.now() / 1000);
    setStatus("ACTIVE", true);
    setText("valCycle", data.cycle ?? "—");
    if (data.speed != null) {
      setText("valSpeed", data.speed);
    } else {
      setText("valSpeed", "—");
    }
    setText("valDuration", "...");
    startDurationTimer();
    prependTableRow(data);
  }

  else if (type === "line_stop") {
    state.lineActive = false;
    stopDurationTimer();
    setStatus("IDLE", false);
    if (data.dur != null) setText("valDuration", formatDuration(data.dur));
    setText("valSpeed", "—");
    prependTableRow(data);
    loadTimeline();
  }

  else if (type === "heartbeat") {
    state.lastHeartbeat = Date.now();
    setText("valHeartbeat", new Date().toLocaleTimeString("uk"));
    if (data.rssi != null) {
      const color = data.rssi > -65 ? "accent-green"
                  : data.rssi > -80 ? "accent-amber" : "accent-red";
      setTextColored("valRssi", `${data.rssi} dBm`, color);
    }
    prependTableRow(data);
    loadStatus();
  }

  else if (type === "boot") {
    addLog(`[BOOT] ${data.device_id} buf=${data.buf_after_reboot} v=${data.version}`, "warn");
    prependTableRow(data);
  }

  else if (type === "speed_update") {
    // Оновлення швидкості під час роботи лінії
    if (state.lineActive && data.speed != null) {
      setText("valSpeed", String(data.speed));
      addLog(`[SPEED] ${data.device_id} speed=${data.speed} cycle=#${data.cycle}`, "tcp");
      // Перезавантажити графік швидкостей щоб показати нову точку
      loadSpeedChart();
    }
  }
}

// ── Status ─────────────────────────────────────────────────────────────────
function setStatus(text, active) {
  const el = document.getElementById("valStatus");
  el.textContent = text;
  el.className   = "card-value " + (active ? "active pulsing" : "idle");
  document.getElementById("cardStatus").style.borderColor =
    active ? "var(--green)" : "var(--border)";
}

function startDurationTimer() {
  stopDurationTimer();
  state.durationTimer = setInterval(() => {
    if (!state.lineActive || !state.lineStartTs) return;
    const elapsed = Math.floor(Date.now() / 1000) - state.lineStartTs;
    setText("valDuration", formatDuration(elapsed));
  }, 1000);
}

function stopDurationTimer() {
  if (state.durationTimer) { clearInterval(state.durationTimer); state.durationTimer = null; }
}

// ── API calls ──────────────────────────────────────────────────────────────
async function loadAll() {
  // Спочатку підтягуємо пристрої (щоб state.device був валідним),
  // а вже потім малюємо інтерактивний таймлайн простоїв.
  await Promise.all([loadStatus(), loadEvents(), loadDevices()]);
  await loadTimeline();
  await loadSpeedChart();
}

async function loadStatus() {
  const r = await fetch("/api/status").then(r => r.json()).catch(() => null);
  if (!r) return;
  setText("dbCount", `БД: ${r.db_count.toLocaleString()} подій`);

  const badge = document.getElementById("tcpBadge");
  if (r.tcp_running) {
    badge.textContent = `TCP ● :${r.tcp_port}`;
    badge.classList.remove("offline");
  } else {
    badge.textContent = "TCP ✕";
    badge.classList.add("offline");
  }

  const statsUrl = "/api/stats" + (state.device ? `?device=${state.device}` : "");
  const stats = await fetch(statsUrl).then(r => r.json()).catch(() => null);
  if (stats) {
    setText("valTotalCycles", stats.total_cycles ?? "—");
    setText("valAvgDur", stats.avg_duration != null ? formatDuration(stats.avg_duration) : "—");
  }
}

async function loadEvents() {
  const url = "/api/events?limit=100" + (state.device ? `&device=${state.device}` : "");
  const rows = await fetch(url).then(r => r.json()).catch(() => []);
  const tbody = document.getElementById("eventsBody");
  tbody.innerHTML = "";
  rows.forEach(r => tbody.appendChild(buildTableRow(r)));
}

async function loadDevices() {
  const devs = await fetch("/api/devices").then(r => r.json()).catch(() => []);
  const sel  = document.getElementById("deviceSelect");
  const cur  = sel.value;
  devs.forEach(d => {
    if (![...sel.options].some(o => o.value === d)) {
      const opt = document.createElement("option");
      opt.value = d; opt.textContent = d;
      sel.appendChild(opt);
    }
  });
  // Якщо пристрій не вибраний — беремо перший доступний.
  if (!cur) {
    if (devs.length) {
      state.device = devs[0];
      sel.value = devs[0];
    } else {
      state.device = "";
      sel.value = "";
    }
  } else {
    state.device = cur;
  }
}

async function loadTimeline() {
  const svg = document.getElementById("timelineSvg");
  if (!svg) return;
  const hint = document.getElementById("timelineHint");

  if (!state.device) {
    svg.innerHTML = "";
    if (hint) hint.textContent = "Оберіть пристрій для таймлайну.";
    return;
  }

  const toTs = Math.floor(Date.now() / 1000);
  const periodSec = getPeriodSeconds();
  const fromTs = toTs - periodSec;

  const url =
    `/api/timeline?from_ts=${fromTs}&to_ts=${toTs}&device=${encodeURIComponent(state.device)}`;
  const res = await fetch(url).then(r => r.json()).catch(() => null);
  if (!res || !Array.isArray(res.intervals)) {
    svg.innerHTML = "";
    if (hint) hint.textContent = "Немає даних для таймлайну.";
    return;
  }

  if (hint) hint.textContent = "Клік по червоному сегменту — причина простою.";
  drawTimeline(res.intervals, fromTs, toTs);
}

async function loadSpeedChart() {
  const canvas = document.getElementById("speedChart");
  if (!canvas) return;

  if (!state.device) {
    // Clear chart if no device selected
    if (state.speedChart) {
      state.speedChart.destroy();
      state.speedChart = null;
    }
    return;
  }

  const toTs = Math.floor(Date.now() / 1000);
  const periodSec = getPeriodSeconds();
  const fromTs = toTs - periodSec;

  const url =
    `/api/speeds?from_ts=${fromTs}&to_ts=${toTs}&device=${encodeURIComponent(state.device)}`;
  const res = await fetch(url).then(r => r.json()).catch(() => null);
  
  if (!res || !Array.isArray(res.speeds) || res.speeds.length === 0) {
    // No data - clear chart
    if (state.speedChart) {
      state.speedChart.destroy();
      state.speedChart = null;
    }
    return;
  }

  // Prepare data for Chart.js
  const cycleAvgData = res.speeds.filter(s => s.source === "cycle_avg");
  const speedUpdates = res.speeds.filter(s => s.source === "speed_update");
  const startData = res.speeds.filter(s => s.source === "start");

  const labels = cycleAvgData.map(d => formatTs(d.ts));
  const cycleAvgSpeeds = cycleAvgData.map(d => d.speed);
  
  // Create gradient for cycle averages
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createLinearGradient(0, 0, 0, 400);
  gradient.addColorStop(0, "rgba(88, 166, 255, 0.9)");
  gradient.addColorStop(1, "rgba(88, 166, 255, 0.1)");

  // Destroy old chart if exists
  if (state.speedChart) {
    state.speedChart.destroy();
  }

  // Create new chart
  state.speedChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        {
          label: "Середня швидкість циклу",
          data: cycleAvgSpeeds,
          borderColor: "#58a6ff",
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBackgroundColor: "#58a6ff",
          pointBorderColor: "#fff",
          pointBorderWidth: 1,
          fill: true,
          tension: 0.3,
          yAxisID: "y"
        },
        {
          label: "Швидкість в реальному часі",
          data: speedUpdates.map(d => ({
            x: cycleAvgData.findIndex(c => Math.abs(c.ts - d.ts) < ((toTs - fromTs) / 20)),
            y: d.speed
          })).filter(p => p.x >= 0),
          type: "scatter",
          borderColor: "#3fb950",
          backgroundColor: "#3fb950",
          pointRadius: 3,
          pointHoverRadius: 5,
          showLine: false,
          yAxisID: "y"
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      interaction: {
        mode: "index",
        intersect: false
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          labels: {
            color: "#e6edf3",
            font: { size: 11 }
          }
        },
        tooltip: {
          backgroundColor: "rgba(0, 0, 0, 0.9)",
          titleColor: "#e6edf3",
          bodyColor: "#8b949e",
          borderColor: "#222",
          borderWidth: 1,
          callbacks: {
            label: function(context) {
              return `${context.dataset.label}: ${context.parsed.y}`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: {
            color: "#8b949e",
            maxTicksLimit: 8,
            maxRotation: 45,
            minRotation: 45
          },
          grid: {
            color: "#222"
          }
        },
        y: {
          beginAtZero: false,
          ticks: {
            color: "#8b949e"
          },
          grid: {
            color: "#222"
          },
          title: {
            display: true,
            text: "Швидкість",
            color: "#8b949e"
          }
        }
      }
    }
  });
}

// ── Timeline (SVG) ───────────────────────────────────────────────────────
function drawTimeline(intervals, fromTs, toTs) {
  const svg = document.getElementById("timelineSvg");
  if (!svg) return;

  const W = 1000;
  const H = 70;
  const total = Math.max(1, toTs - fromTs);
  const rootStyles = getComputedStyle(document.documentElement);
  const green = (rootStyles.getPropertyValue("--green") || "").trim() || "#3fb950";
  const red   = (rootStyles.getPropertyValue("--red")   || "").trim() || "#f85149";

  svg.innerHTML = "";

  intervals.forEach((iv, idx) => {
    const start = iv.start_ts;
    const stop  = iv.stop_ts;
    if (!start || !stop || stop <= start) return;

    const x = ((start - fromTs) / total) * W;
    const w = ((stop - start) / total) * W;
    const isDown = iv.type === "down";

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", String(x));
    rect.setAttribute("y", "0");
    rect.setAttribute("width", String(Math.max(1, w)));
    rect.setAttribute("height", String(H));
    rect.setAttribute("rx", "4");
    rect.setAttribute("ry", "4");
    
    // Color based on downtime reason
    let fillClass = isDown ? "timeline-down" : "timeline-run";
    if (isDown && iv.reason) {
      fillClass = `timeline-down-reason-${iv.reason}`;
    }
    rect.setAttribute("class", `timeline-seg ${fillClass}`);
    
    // Fallback inline fill for older browsers
    const fillColor = isDown 
      ? (iv.reason ? getReasonColor(iv.reason) : red)
      : green;
    rect.setAttribute("fill", fillColor);
    rect.setAttribute("fill-opacity", isDown ? "0.85" : "0.90");

    const titleParts = [
      `${isDown ? "СТОЇТЬ" : "ПРАЦЮЄ"}`,
      `start: ${formatTs(start)}`,
      `stop: ${formatTs(stop)}`
    ];
    if (isDown) {
      const reasonLabels = {
        unknown: "Не визначено",
        planned_maintenance: "Планове ТО",
        lack_material: "Немає матеріалу",
        equipment_failure: "Аварія / несправність",
        operator_stop: "Зупинка оператором",
        logistics: "Логістика / очікування",
        other: "Інше"
      };
      titleParts.push(`reason: ${reasonLabels[iv.reason] || iv.reason || "—"}`);
      if (iv.comment) titleParts.push(`comment: ${iv.comment}`);
    }
    if (!isDown) {
      if (iv.cycle != null) titleParts.push(`cycle: #${iv.cycle}`);
      if (iv.speed != null) titleParts.push(`speed: ${iv.speed}`);
    }
    rect.setAttribute("title", titleParts.join("\\n"));
    rect.dataset.startTs = String(start);
    rect.dataset.stopTs = String(stop);

    rect.style.cursor = isDown ? "pointer" : "default";
    rect.addEventListener("click", () => {
      if (!isDown) return;
      setSelectedDowntime(iv);
      // Локальна підсвітка
      highlightSelectedRect(svg, iv);
    });

    svg.appendChild(rect);
  });

  // Якщо було вибрано сегмент — підсвітити після перерендеру
  if (state.selectedDowntime) highlightSelectedRect(svg, state.selectedDowntime);
}

function getReasonColor(reason) {
  const colors = {
    unknown: "#8b949e",
    planned_maintenance: "#58a6ff",
    lack_material: "#d2a8ff",
    equipment_failure: "#f85149",
    operator_stop: "#e3b341",
    logistics: "#fa9a28",
    other: "#a0a0a0"
  };
  return colors[reason] || "#f85149"; // default red
}

function highlightSelectedRect(svg, iv) {
  if (!svg || !iv) return;
  const rects = svg.querySelectorAll("rect.timeline-seg");
  rects.forEach(r => {
    r.setAttribute("stroke", "none");
    r.setAttribute("stroke-width", "0");

    const rs = Number(r.dataset.startTs);
    const re = Number(r.dataset.stopTs);
    if (Number(iv.start_ts) === rs && Number(iv.stop_ts) === re) {
      r.setAttribute("stroke", "#e3b341");
      r.setAttribute("stroke-width", "2");
    }
  });
}

function setSelectedDowntime(iv) {
  state.selectedDowntime = {
    device_id: iv.device_id,
    start_ts: iv.start_ts,
    stop_ts: iv.stop_ts,
    reason: iv.reason ?? "unknown",
    comment: iv.comment ?? ""
  };

  document.getElementById("selectedDowntimePeriod").textContent =
    `${formatTs(iv.start_ts)} → ${formatTs(iv.stop_ts)}`;

  const sel = document.getElementById("downtimeReason");
  if (sel) sel.value = state.selectedDowntime.reason || "unknown";

  const ta = document.getElementById("downtimeComment");
  if (ta) ta.value = state.selectedDowntime.comment || "";

  const saved = document.getElementById("downtimeSaved");
  if (saved) saved.textContent = "";
}

function clearDowntimeSelection() {
  state.selectedDowntime = null;
  document.getElementById("selectedDowntimePeriod").textContent = "—";
  const sel = document.getElementById("downtimeReason");
  if (sel) sel.value = "unknown";
  const ta = document.getElementById("downtimeComment");
  if (ta) ta.value = "";
  const saved = document.getElementById("downtimeSaved");
  if (saved) saved.textContent = "";
  loadTimeline();
}

async function saveDowntime() {
  const iv = state.selectedDowntime;
  const saved = document.getElementById("downtimeSaved");
  if (saved) saved.textContent = "";

  if (!iv) {
    if (saved) saved.textContent = "Спочатку виберіть червоний сегмент.";
    return;
  }

  const reason = document.getElementById("downtimeReason")?.value || "unknown";
  const comment = document.getElementById("downtimeComment")?.value || "";

  const payload = {
    device_id: iv.device_id || state.device,
    start_ts: iv.start_ts,
    stop_ts: iv.stop_ts,
    reason,
    comment
  };

  const res = await fetch("/api/downtime/set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).catch(() => null);

  if (!res) {
    if (saved) saved.textContent = "Не вдалося зберегти: мережевий/серверний збій.";
    return;
  }

  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail; } catch { }
    if (saved) saved.textContent = `Не вдалося зберегти: ${detail || res.status}`;
    return;
  }

  if (saved) saved.textContent = "Збережено.";
  // Оновимо локальні дані, щоб tooltip/повторний рендер були консистентними.
  state.selectedDowntime.reason = reason;
  state.selectedDowntime.comment = comment;
  await loadTimeline();
}

// ── Table ──────────────────────────────────────────────────────────────────
function buildTableRow(r) {
  const tr  = document.createElement("tr");
  const ts  = r.esp_ts ? formatTs(r.esp_ts) : "—";
  const cls = {
    line_start: "ev-start", line_stop: "ev-stop",
    heartbeat:  "ev-heartbeat", boot: "ev-boot"
  }[r.event_type] || "";

  tr.innerHTML = `
    <td>${ts}</td>
    <td>${r.device_id ?? "—"}</td>
    <td class="${cls}">${r.event_type}</td>
    <td>${r.cycle ?? "—"}</td>
    <td>${r.duration != null ? formatDuration(r.duration) : "—"}</td>
    <td>${r.buffered ? '<span class="badge-buf">buf</span>' : ""}</td>
    <td>${r.rssi != null ? r.rssi + " dBm" : "—"}</td>
  `;
  return tr;
}

function prependTableRow(data) {
  const r = {
    esp_ts:     data.ts,
    device_id:  data.device_id,
    event_type: data.event_type,
    cycle:      data.cycle,
    duration:   data.dur,
    buffered:   data.buffered ? 1 : 0,
    rssi:       data.rssi,
  };
  const tbody = document.getElementById("eventsBody");
  tbody.insertBefore(buildTableRow(r), tbody.firstChild);
  while (tbody.children.length > 100) tbody.removeChild(tbody.lastChild);
}

// ── Log ────────────────────────────────────────────────────────────────────
function addLog(msg, type = "") {
  const box = document.getElementById("logBox");
  const div = document.createElement("div");
  div.className = "log-line";
  const ts  = new Date().toLocaleTimeString("uk");
  const cls = type === "tcp" ? "log-tcp" : type === "warn" ? "log-warn"
            : type === "err" ? "log-err" : "";
  div.innerHTML = `<span class="log-ts">${ts}</span><span class="${cls}">${escHtml(msg)}</span>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  while (box.children.length > 200) box.removeChild(box.firstChild);
}

function clearLog() { document.getElementById("logBox").innerHTML = ""; }

// ── Helpers ────────────────────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setDot(cls) {
  document.getElementById("wsDot").className = `status-dot ${cls}`;
}

function setTextColored(id, val, colorClass) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className   = `card-value ${colorClass}`;
}

function formatTs(unix) {
  return new Date(unix * 1000).toLocaleString("uk", {
    day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

function formatDuration(s) {
  s = Math.round(s);
  if (s < 60)   return `${s} с`;
  if (s < 3600) return `${Math.floor(s/60)} хв ${s % 60} с`;
  return `${Math.floor(s/3600)} г ${Math.floor((s % 3600) / 60)} хв`;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function getPeriodSeconds() {
  const sel = document.getElementById("periodSelect");
  const v = sel?.value || "1h";
  if (v === "15m") return 15 * 60;
  if (v === "1h")  return 60 * 60;
  if (v === "6h")  return 6 * 60 * 60;
  if (v === "24h") return 24 * 60 * 60;
  return 60 * 60; // fallback: 1h
}

function onDeviceChange() {
  state.device = document.getElementById("deviceSelect").value;
  loadAll();
}

// Refresh timeline and speed chart when period changes
const originalLoadTimeline = loadTimeline;
loadTimeline = async function() {
  await originalLoadTimeline();
  await loadSpeedChart();
};

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  connectWS();
  loadAll();
  setInterval(loadStatus,  15000);
  setInterval(loadDevices, 30000);
});
