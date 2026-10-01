const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request(path, options = {}) {
  // FormData bodies must let the browser set the multipart boundary itself
  const headers = typeof options.body === "string" ? { "Content-Type": "application/json" } : {};
  const res = await fetch(BASE + path, { headers, ...options });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d) => d.msg).join("; ");
    } catch {
      /* keep the status text */
    }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  createJob: (body) => request("/api/jobs", { method: "POST", body: JSON.stringify(body) }),
  checkCompliance: (body) => request("/api/compliance/check", { method: "POST", body: JSON.stringify(body) }),
  listJobs: (statuses = []) => {
    const params = new URLSearchParams({ limit: "100" });
    statuses.forEach((status) => params.append("status", status));
    return request(`/api/jobs?${params}`);
  },
  getJob: (id) => request(`/api/jobs/${id}`),
  cancelJob: (id) => request(`/api/jobs/${id}`, { method: "DELETE" }),
  retryJob: (id) => request(`/api/jobs/${id}/retry`, { method: "POST" }),
  llmProfiles: () => request("/api/llm/profiles"),
  stats: () => request("/api/stats"),
  health: () => request("/api/health"),
  reportUrl: (id, format) => `${BASE}/api/jobs/${id}/report/${format}`,
  /** Live pipeline steps; `after` is how many the caller already has, so nothing arrives twice. */
  eventsUrl: (id, after = 0) => `${BASE}/api/jobs/${id}/events?after=${after}`,
};

export const isActive = (status) => status === "queued" || status === "running";

export function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") || value.includes("+") ? value : `${value}Z`);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const percent = (value) => `${Math.round((value ?? 0) * 100)}%`;

/** "example.com/pricing" for a web page. */
export const sourceLabel = (url) => url.replace(/^https?:\/\//, "");
