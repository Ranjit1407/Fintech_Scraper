import { formatTime, sourceLabel } from "../api";
import StatusBadge from "./StatusBadge";

export default function JobList({ jobs, selectedId, onSelect, title = "Jobs" }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {jobs.length === 0 ? (
        <p className="muted small">No jobs yet.</p>
      ) : (
        <ul className="job-list">
          {jobs.map((job) => (
            <li key={job.id}>
              <button
                className={job.id === selectedId ? "job-item selected" : "job-item"}
                onClick={() => onSelect(job.id)}
              >
                <span className="job-url" title={job.url}>
                  {sourceLabel(job.url)}
                </span>
                <span className="job-meta">
                  <StatusBadge status={job.status} />
                  <span className="muted small">
                    {job.product_count} product{job.product_count === 1 ? "" : "s"} ·{" "}
                    {job.updated_at ? `updated ${formatTime(job.updated_at)}` : formatTime(job.created_at)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
