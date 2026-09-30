import { useEffect, useState } from "react";

const STEPS = [
  { label: "Governance checks and reading the source", stages: ["compliance", "document", "router"] },
  { label: "Cleaning and understanding", stages: ["cleaning", "understanding"] },
  { label: "Indexing the whole document (RAG)", stages: ["rag"] },
  { label: "Extracting items from every part", stages: ["extraction"] },
  { label: "Checking facts against the source", stages: ["validation", "storage"] },
  { label: "Searching your keywords", stages: ["keywords"] },
  { label: "Writing the summary", stages: ["report"] },
];
const RAG_STEP = 2;
const KEYWORD_STEP = 5;

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
    // the RAG summary step logs "rag" again while writing the report
    if (!(event.stage === "rag" && current > RAG_STEP)) current = Math.max(current, stepOf(event, understood));
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
  const hasKeywords = (job.request?.keywords || []).length > 0;
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
            const skipped = (i === RAG_STEP && i < current && !usedRag) || (i === KEYWORD_STEP && !hasKeywords);
            const state = skipped ? "skipped" : i < current ? "done" : i === current ? "active" : "pending";
            return (
              <li key={step.label} className={`step step-${state}`}>
                <span className="step-mark" aria-hidden="true">
                  {state === "done" ? "✓" : state === "skipped" ? "–" : i + 1}
                </span>
                <span>
                  {step.label}
                  {skipped && (
                    <span className="muted small">
                      {i === RAG_STEP ? " · not needed, fits in one pass" : " · no keywords given"}
                    </span>
                  )}
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
