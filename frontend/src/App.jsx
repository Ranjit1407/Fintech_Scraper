import { useCallback, useEffect, useRef, useState } from "react";
import { api, isActive } from "./api";
import JobDetail from "./components/JobDetail";
import JobList, { statusesFor } from "./components/JobList";
import NewJobForm from "./components/NewJobForm";
import StatsBar from "./components/StatsBar";

const TABS = [
  ["new", "New job"],
  ["jobs", "Jobs"],
];

export default function App() {
  const [tab, setTab] = useState("new");
  const [jobs, setJobs] = useState([]);
  const [stats, setStats] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [jobList, statData] = await Promise.all([api.listJobs(statusesFor(filter)), api.stats()]);
      setJobs(jobList);
      setStats(statData);
      setError(null);
    } catch (err) {
      setError(`Cannot reach the API (is the backend running on port 8000?): ${err.message}`);
    }
  }, [filter]);

  // Children get an identity that never changes, so changing the filter does not restart their polling.
  const latest = useRef(refresh);
  latest.current = refresh;
  const onChange = useCallback(() => latest.current(), []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Counted from stats as well as the list, so a filtered list still refreshes while a job runs.
  const byStatus = stats?.jobs_by_status || {};
  const anyActive =
    jobs.some((job) => isActive(job.status)) || (byStatus.queued ?? 0) + (byStatus.running ?? 0) > 0;
  useEffect(() => {
    if (!anyActive) return undefined;
    const timer = setInterval(() => latest.current(), 3000);
    return () => clearInterval(timer);
  }, [anyActive]);

  const openJob = (id) => {
    setSelectedId(id);
    setTab("jobs");
  };

  const handleCreated = (job) => {
    openJob(job.id);
    refresh();
  };

  return (
    <div className="app">
      <header className="topbar">
        <button className="brand" onClick={() => setTab("new")}>
          <span className="brand-mark" aria-hidden="true">◆</span>
          <span>
            <span className="brand-name">Web Scraper</span>
            <span className="brand-sub muted small">Governance-first scraping of any website, with keyword search</span>
          </span>
        </button>
        <nav className="tabs" aria-label="Views">
          {TABS.map(([key, label]) => (
            <button key={key} className={tab === key ? "tab active" : "tab"} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </nav>
      </header>

      {error && <div className="alert error">{error}</div>}

      {tab === "new" && (
        <>
          <NewJobForm onCreated={handleCreated} />
          {jobs.length > 0 && (
            <div className="recent">
              <JobList title="Recent jobs" jobs={jobs.slice(0, 5)} selectedId={null} onSelect={openJob} />
            </div>
          )}
        </>
      )}

      {tab === "jobs" && (
        <>
          <StatsBar stats={stats} />
          <div className="layout">
            <aside className="sidebar">
              <JobList
                jobs={jobs}
                selectedId={selectedId}
                onSelect={setSelectedId}
                filter={filter}
                onFilterChange={setFilter}
                statusCounts={byStatus}
              />
            </aside>
            <main className="content">
              {selectedId ? (
                <JobDetail jobId={selectedId} onChange={onChange} onOpen={openJob} />
              ) : (
                <div className="empty panel">
                  <h2>No job selected</h2>
                  <p className="muted">Pick a job from the list, or start a new one.</p>
                  <button onClick={() => setTab("new")}>New job</button>
                </div>
              )}
            </main>
          </div>
        </>
      )}
    </div>
  );
}
