import { CATEGORIES } from "../api";

export default function StatsBar({ stats }) {
  if (!stats) return null;
  const byCategory = stats.products_by_category || {};
  const blocked = stats.jobs_by_status?.blocked || 0;
  return (
    <section className="stats" aria-label="Totals">
      <div className="stat">
        <span className="stat-value">{stats.products}</span>
        <span className="stat-label">Products</span>
      </div>
      <div className="stat">
        <span className="stat-value">{stats.sources}</span>
        <span className="stat-label">Sources</span>
      </div>
      <div className="stat">
        <span className="stat-value">{stats.jobs}</span>
        <span className="stat-label">Jobs</span>
      </div>
      <div className="stat">
        <span className="stat-value">{blocked}</span>
        <span className="stat-label">Blocked by compliance</span>
      </div>
      <div className="stat stat-wide">
        <span className="stat-label">By category</span>
        <div className="chips">
          {CATEGORIES.filter((c) => byCategory[c]).map((c) => (
            <span key={c} className="chip">
              {c} <strong>{byCategory[c]}</strong>
            </span>
          ))}
          {!stats.products && <span className="muted small">No products yet</span>}
        </div>
      </div>
    </section>
  );
}
