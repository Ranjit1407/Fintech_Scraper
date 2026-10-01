import { useEffect, useState } from "react";
import { api, formatTime, isActive } from "../api";
import ComplianceTable from "./ComplianceTable";
import CopyButton from "./CopyButton";
import ItemCard from "./ItemCard";
import KeywordResults from "./KeywordResults";
import PageReport from "./PageReport";
import ProcessingView from "./ProcessingView";
import StatusBadge from "./StatusBadge";

function ItemList({ title, items, empty }) {
  return (
    <section className="panel">
      <h3>
        {title} ({items.length})
      </h3>
      {items.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <div className="items">
          {items.map((item) => (
            <ItemCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </section>
  );
}

export default function JobDetail({ jobId, onChange, onOpen }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  useEffect(() => {
    let stopped = false;
    let stream;
    let timer;

    const load = async () => {
      const data = await api.getJob(jobId);
      if (!stopped) {
        setJob(data);
        setError(null);
      }
      return data;
    };

    // Used when the event stream cannot get through, for instance behind a proxy that buffers it.
    const poll = async () => {
      try {
        const data = await load();
        if (stopped) return;
        if (isActive(data.status)) timer = setTimeout(poll, 2000);
        else onChange?.();
      } catch (err) {
        if (!stopped) setError(err.message);
      }
    };

    const finish = async () => {
      try {
        await load();
        if (!stopped) onChange?.();
      } catch (err) {
        if (!stopped) setError(err.message);
      }
    };

    const listen = (seen) => {
      stream = new EventSource(api.eventsUrl(jobId, seen));
      stream.addEventListener("stage", (message) => {
        const event = JSON.parse(message.data);
        setJob((prev) => (prev ? { ...prev, events: [...prev.events, event] } : prev));
      });
      stream.addEventListener("end", () => {
        stream.close();
        if (!stopped) finish();
      });
      stream.onerror = () => {
        stream.close();
        if (!stopped) poll();
      };
    };

    setJob(null);
    setError(null);
    (async () => {
      try {
        const data = await load();
        if (stopped) return;
        if (isActive(data.status)) listen(data.events.length);
        else onChange?.();
      } catch (err) {
        if (!stopped) setError(err.message);
      }
    })();

    return () => {
      stopped = true;
      clearTimeout(timer);
      stream?.close();
    };
  }, [jobId, onChange]);

  const act = async (kind) => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "cancel") {
        await api.cancelJob(job.id);
        setJob(await api.getJob(job.id));
      } else {
        const next = await api.retryJob(job.id);
        onOpen?.(next.id);
      }
      onChange?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  if (error && !job) return <div className="alert error">{error}</div>;
  if (!job) return <div className="panel muted">Loading…</div>;

  const compliance = job.compliance;
  const understanding = job.understanding;
  const done = job.status === "completed";
  const active = isActive(job.status);
  const keywords = job.request?.keywords || [];
  const findings = job.page_report?.keyword_findings || [];
  const matched = job.items.filter((item) => item.data?.matched_keywords?.length);
  const others = job.items.filter((item) => !item.data?.matched_keywords?.length);

  return (
    <div className="job-detail">
      {active && <ProcessingView job={job} />}

      <section className="panel">
        <div className="job-header">
          <div>
            <h2 className="break" title={decodeURIComponent(job.url)}>
              {job.url}
              <CopyButton value={job.url} label="Copy link" title="Copy the page address" />
            </h2>
            <p className="muted small">
              Job {job.id.slice(0, 8)} · created {formatTime(job.created_at)}
              {job.updated_at && ` · updated ${formatTime(job.updated_at)} from a re-scrape`}
              {job.fetch_method && ` · fetched via ${job.fetch_method}`}
            </p>
            {keywords.length > 0 && (
              <div className="chips">
                <span className="muted small">Keywords:</span>
                {keywords.map((keyword) => (
                  <span key={keyword} className="chip keyword-chip">
                    {keyword}
                  </span>
                ))}
              </div>
            )}
          </div>
          <StatusBadge status={job.status} />
        </div>
        {job.error && (
          <div className={`alert ${job.status === "failed" ? "error" : "warn"}`}>{job.error}</div>
        )}
        {error && <div className="alert error">{error}</div>}
        <div className="job-actions">
          {active ? (
            <button type="button" className="secondary" disabled={busy} onClick={() => act("cancel")}>
              {busy === "cancel" ? "Stopping…" : "Cancel job"}
            </button>
          ) : job.retryable ? (
            <button type="button" className="secondary" disabled={busy} onClick={() => act("retry")}>
              {busy === "retry" ? "Starting…" : done ? "Scrape again" : "Retry"}
            </button>
          ) : (
            <span className="muted small">
              Made by an earlier version of the app, so it cannot be run again.
            </span>
          )}
          {done && (
            <>
              <span className="muted small">Download report:</span>
              {["csv", "pdf"].map((fmt) => (
                <a key={fmt} className="button secondary" href={api.reportUrl(job.id, fmt)}>
                  {fmt.toUpperCase()}
                </a>
              ))}
            </>
          )}
        </div>
      </section>

      {keywords.length > 0 && (done || findings.length > 0) && (
        <KeywordResults findings={findings} keywords={keywords} pending={!done && findings.length === 0} />
      )}
      {keywords.length > 0 && matched.length > 0 && (
        <ItemList title="Items matching your keywords" items={matched} />
      )}

      {job.page_report && (
        <PageReport report={job.page_report} />
      )}

      {(done || others.length > 0) && (
        <ItemList
          title={keywords.length > 0 ? "Other items" : "Items"}
          items={others}
          empty={job.items.length ? "Every item matches one of your keywords." : "No items passed validation."}
        />
      )}
      {job.rejected.length > 0 && (
        <details className="panel details">
          <summary>{job.rejected.length} rejected by validation</summary>
          <ul className="rejected">
            {job.rejected.map((r, i) => (
              <li key={i}>
                <strong>{r.name || "(unnamed)"}</strong> — {r.reason}
                {r.errors?.length > 0 && <span className="muted small"> ({r.errors.join("; ")})</span>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {compliance && (
        <section className="panel">
          <h3>
            Governance checks{" "}
            <span className={compliance.allowed ? "verdict-inline allow" : "verdict-inline block"}>
              {compliance.allowed ? "allowed" : "blocked"}
            </span>
          </h3>
          <ComplianceTable checks={compliance.checks} />
        </section>
      )}

      {understanding && (
        <section className="panel">
          <h3>Page understanding</h3>
          <dl className="kv">
            <dt>Page type</dt>
            <dd>{understanding.page_type.replace("_", " ")}</dd>
            <dt>Publisher</dt>
            <dd>{understanding.publisher || understanding.institution || "—"}</dd>
            <dt>Topic</dt>
            <dd>{understanding.topic || "—"}</dd>
            <dt>Method</dt>
            <dd>{understanding.method}</dd>
          </dl>
        </section>
      )}

      <details className="panel pipeline-log" open={!active && !done}>
        <summary>
          <h3>Pipeline log ({job.events.length} steps)</h3>
        </summary>
        <ol className="timeline">
          {job.events.map((event, i) => (
            <li key={i} className={`event event-${event.level}`}>
              <span className="event-stage">{event.stage}</span>
              <span className="event-message">{event.message}</span>
            </li>
          ))}
          {active && <li className="event event-pending">Working…</li>}
        </ol>
      </details>
    </div>
  );
}
