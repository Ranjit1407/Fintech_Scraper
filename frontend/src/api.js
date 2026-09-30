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
  uploadDocuments: (files) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.webkitRelativePath || file.name);
    return request("/api/uploads", { method: "POST", body: form });
  },
  processUpload: (batchId, body) =>
    request(`/api/uploads/${batchId}/process`, { method: "POST", body: JSON.stringify(body) }),
  cancelUpload: (batchId) => request(`/api/uploads/${batchId}`, { method: "DELETE" }),
  checkCompliance: (body) => request("/api/compliance/check", { method: "POST", body: JSON.stringify(body) }),
  listJobs: () => request("/api/jobs?limit=100"),
  getJob: (id) => request(`/api/jobs/${id}`),
  stats: () => request("/api/stats"),
  health: () => request("/api/health"),
  reportUrl: (id, format) => `${BASE}/api/jobs/${id}/report/${format}`,
};

export const isActive = (status) => status === "queued" || status === "running";

export function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") || value.includes("+") ? value : `${value}Z`);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const percent = (value) => `${Math.round((value ?? 0) * 100)}%`;

export const isDocument = (url) => url.startsWith("file:");

/** "example.com/pricing" for web pages, "Document · guide.pdf" for local files. */
export function sourceLabel(url) {
  if (!isDocument(url)) return url.replace(/^https?:\/\//, "");
  return `Document · ${decodeURIComponent(url.split("/").pop())}`;
}
