import { useEffect, useState } from "react";

const STEPS = [
  { label: "Reading the source", stages: ["compliance", "document", "router"] },
  { label: "Cleaning and understanding", stages: ["cleaning", "understanding"] },
  { label: "Indexing the whole document (RAG)", stages: ["rag"] },
  { label: "Extracting products from every part", stages: ["extraction", "classification"] },
  { label: "Checking facts against the source", stages: ["validation", "storage"] },
  { label: "Writing the summary", stages: ["summarization", "report"] },
];

function stepOf(event, understood) {
  // "extraction" is also used for parsing, before the AI stages start
  if (event.stage === "extraction" && !understood) return 0;
  return STEPS.findIndex((step) => step.stages.includes(event.stage));
}

function progress(events) {
  let current = 0;
  let understood = false;
  let usedRag = false;
  for (const event of events) {
    if (event.stage === "understanding") understood = true;
    if (event.stage === "rag") usedRag = true;
    current = Math.max(current, stepOf(event, understood));
  }
  return { current, usedRag };
}

function elapsed(since, now) {
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function parseTime(value) {
  return new Date(value.endsWith("Z") || value.includes("+") ? value : `${value}Z`).getTime();
}

export default function ProcessingView({ job }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const queued = job.status === "queued";
  const { current, usedRag } = progress(job.events);
  const latest = job.events[job.events.length - 1];

  return (
    <section className="panel processing" aria-live="polite" aria-busy="true">
      <div className="spinner" aria-hidden="true" />
      <h3>{queued ? "Waiting to start" : "Processing"}</h3>
      <p className="processing-message">
        {queued ? "Earlier jobs are still running; this one starts when they finish." : latest?.message || "Starting…"}
      </p>
      {!queued && (
        <ol className="steps">
          {STEPS.map((step, i) => {
            const skipped = i === 2 && i < current && !usedRag;
            const state = skipped ? "skipped" : i < current ? "done" : i === current ? "active" : "pending";
            return (
              <li key={step.label} className={`step step-${state}`}>
                <span className="step-mark" aria-hidden="true">
                  {state === "done" ? "✓" : state === "skipped" ? "–" : i + 1}
                </span>
                <span>
                  {step.label}
                  {skipped && <span className="muted small"> · not needed, fits in one pass</span>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
      <p className="muted small">
        Elapsed {elapsed(parseTime(job.started_at || job.created_at), now)}. With the local model, each part of a long
        document takes about a minute.
      </p>
    </section>
  );
}
