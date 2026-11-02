"use strict";

const elements = {
  connection: document.querySelector("#connection-state"),
  overall: document.querySelector("#overall-status"),
  healthy: document.querySelector("#healthy-count"),
  down: document.querySelector("#down-count"),
  total: document.querySelector("#total-count"),
  lastChecked: document.querySelector("#last-checked"),
  uptime: document.querySelector("#uptime"),
  serviceList: document.querySelector("#service-list"),
  emptyState: document.querySelector("#empty-state"),
  error: document.querySelector("#error-message"),
};

function formatTime(value) {
  if (!value) return "Not checked yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days) return `${days}d ${hours % 24}h`;
  if (hours) return `${hours}h ${minutes % 60}m`;
  if (minutes) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function createServiceCard(service) {
  const card = document.createElement("article");
  card.className = "service-card";

  const heading = document.createElement("div");
  heading.className = "service-heading";
  const identity = document.createElement("div");
  const name = document.createElement("h3");
  name.className = "service-name";
  name.textContent = service.name;
  const url = document.createElement("p");
  url.className = "service-url";
  url.textContent = service.url;
  identity.append(name, url);

  const badge = document.createElement("span");
  badge.className = `status-badge status-${service.status}`;
  badge.textContent = {
    healthy: "Healthy",
    down: "Down",
    unknown: "Checking",
  }[service.status] || "Unknown";
  heading.append(identity, badge);

  const meta = document.createElement("div");
  meta.className = "service-meta";
  const checked = document.createElement("span");
  checked.textContent = `Last checked: ${formatTime(service.checked_at)}`;
  meta.append(checked);

  if (service.response_time_ms !== null) {
    const response = document.createElement("span");
    response.textContent = `Response: ${service.response_time_ms} ms`;
    meta.append(response);
  }
  if (service.status_code !== null) {
    const code = document.createElement("span");
    code.textContent = `HTTP ${service.status_code}`;
    meta.append(code);
  }
  if (service.error) {
    const error = document.createElement("span");
    error.className = "service-error";
    error.textContent = service.error;
    meta.append(error);
  }

  card.append(heading, meta);
  return card;
}

function renderStatus(data) {
  elements.connection.textContent = "Live";
  elements.overall.textContent = {
    healthy: "All systems healthy",
    degraded: "Degraded",
    down: "All systems down",
    unknown: "No checks yet",
  }[data.status] || "Unknown";
  elements.overall.className = `summary-value status-${data.status}`;
  elements.healthy.textContent = data.summary.healthy;
  elements.down.textContent = data.summary.down;
  elements.total.textContent = data.summary.total;
  elements.lastChecked.textContent = data.last_checked_at
    ? `Last health check: ${formatTime(data.last_checked_at)}`
    : "Waiting for the first health check…";
  elements.uptime.textContent = `Monitor uptime ${formatDuration(data.uptime_seconds)}`;
  elements.serviceList.replaceChildren(...data.services.map(createServiceCard));
  elements.emptyState.hidden = data.services.length > 0;
  elements.error.hidden = true;
}

async function updateDashboard() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`Monitor returned HTTP ${response.status}`);
    const data = await response.json();
    renderStatus(data);
  } catch (error) {
    elements.connection.textContent = "Disconnected";
    elements.error.textContent = `Could not load monitor status: ${error.message}`;
    elements.error.hidden = false;
  }
}

updateDashboard();
window.setInterval(updateDashboard, 2000);
