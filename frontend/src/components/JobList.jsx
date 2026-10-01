import { formatTime, sourceLabel } from "../api";
import StatusBadge from "./StatusBadge";

/** Sidebar filters. Each maps to the statuses sent to GET /api/jobs; "all" sends none. */
export const JOB_FILTERS = [
  ["all", "All", []],
  ["active", "Active", ["queued", "running"]],
  ["completed", "Completed", ["completed"]],
  ["failed", "Failed", ["failed"]],
  ["blocked", "Blocked", ["blocked"]],
  ["cancelled", "Cancelled", ["cancelled"]],
];

export function statusesFor(key) {
  return JOB_FILTERS.find(([name]) => name === key)?.[2] ?? [];
}

function countFor(statuses, counts) {
  const byStatus = counts || {};
  const keys = statuses.length ? statuses : Object.keys(byStatus);
  return keys.reduce((total, status) => total + (byStatus[status] || 0), 0);
}

function Filters({ filter, onFilterChange, counts }) {
  return (
    <div className="job-filters" role="group" aria-label="Filter jobs by status">
      {JOB_FILTERS.map(([key, label, statuses]) => {
        const count = countFor(statuses, counts);
        if (count === 0 && key !== "all" && key !== filter) return null;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={filter === key}
            className={filter === key ? "job-filter active" : "job-filter"}
            onClick={() => onFilterChange(key)}
          >
            {label} <span className="job-filter-count">{count}</span>
          </button>
        );
      })}
    </div>
  );
}

export default function JobList({
  jobs,
  selectedId,
  onSelect,
  title = "Jobs",
  filter,
  onFilterChange,
  statusCounts,
}) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {onFilterChange && <Filters filter={filter} onFilterChange={onFilterChange} counts={statusCounts} />}
      {jobs.length === 0 ? (
        <p className="muted small">{filter && filter !== "all" ? "No jobs with this status." : "No jobs yet."}</p>
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
                    {job.item_count} item{job.item_count === 1 ? "" : "s"} ·{" "}
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
