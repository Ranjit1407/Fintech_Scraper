import { useCallback, useEffect, useState } from "react";
import { api, isActive } from "./api";
import JobDetail from "./components/JobDetail";
import JobList from "./components/JobList";
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
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [jobList, statData] = await Promise.all([api.listJobs(), api.stats()]);
      setJobs(jobList);
      setStats(statData);
      setError(null);
    } catch (err) {
      setError(`Cannot reach the API (is the backend running on port 8000?): ${err.message}`);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const anyActive = jobs.some((job) => isActive(job.status));
  useEffect(() => {
    if (!anyActive) return undefined;
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [anyActive, refresh]);

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
            <span className="brand-name">Fintech Scraper</span>
            <span className="brand-sub muted small">Compliance-first financial product extraction</span>
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
              <JobList jobs={jobs} selectedId={selectedId} onSelect={setSelectedId} />
            </aside>
            <main className="content">
              {selectedId ? (
                <JobDetail jobId={selectedId} onChange={refresh} />
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
