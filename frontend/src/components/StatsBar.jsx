export default function StatsBar({ stats }) {
  if (!stats) return null;
  const byType = Object.entries(stats.items_by_type || {});
  const blocked = stats.jobs_by_status?.blocked || 0;
  return (
    <section className="stats" aria-label="Totals">
      <div className="stat">
        <span className="stat-value">{stats.items}</span>
        <span className="stat-label">Items</span>
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
        <span className="stat-label">Blocked by governance</span>
      </div>
      <div className="stat stat-wide">
        <span className="stat-label">Most common item types</span>
        <div className="chips">
          {byType.map(([type, count]) => (
            <span key={type} className="chip">
              {type} <strong>{count}</strong>
            </span>
          ))}
          {!stats.items && <span className="muted small">No items yet</span>}
        </div>
      </div>
    </section>
  );
}
