const SECTIONS = [
  ["key_points", "Key points"],
  ["products_overview", "Products"],
  ["rates_and_fees", "Rates & fees"],
  ["eligibility_and_conditions", "Eligibility & conditions"],
  ["things_to_watch", "Things to watch"],
  ["data_gaps", "Not stated in the source"],
];

function ReportDetails({ report }) {
  const paragraphs = (report.detailed_summary || "").split(/\n\s*\n/).filter(Boolean);
  const sections = report.sections || [];
  const figures = report.key_figures || [];
  const notes = (report.part_notes || []).filter((part) => part.length > 0);
  if (!paragraphs.length && !sections.length && !figures.length && !notes.length) return null;

  return (
    <div className="report-details">
      {paragraphs.length > 0 && (
        <div className="detail-block">
          <h4>Detailed summary</h4>
          {paragraphs.map((text, i) => (
            <p key={i}>{text}</p>
          ))}
        </div>
      )}

      {figures.length > 0 && (
        <div className="detail-block">
          <h4>Key figures ({figures.length})</h4>
          <div className="table-wrap">
            <table className="figures">
              <thead>
                <tr>
                  <th>What it measures</th>
                  <th>Value</th>
                  <th>Context</th>
                </tr>
              </thead>
              <tbody>
                {figures.map((figure, i) => (
                  <tr key={i}>
                    <td>{figure.label}</td>
                    <td className="figure-value">{figure.value}</td>
                    <td className="muted">{figure.context || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {sections.length > 0 && (
        <div className="detail-block">
          <h4>Section by section ({sections.length})</h4>
          <dl className="section-list">
            {sections.map((section, i) => (
              <div key={i}>
                <dt>{section.title}</dt>
                <dd>{section.summary}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {notes.length > 0 && (
        <details className="detail-block">
          <summary>
            <h4>Notes from every part ({notes.reduce((n, part) => n + part.length, 0)})</h4>
          </summary>
          {notes.map((facts, i) => (
            <div key={i} className="part-notes">
              <h5>Part {i + 1}</h5>
              <ul>
                {facts.map((fact, j) => (
                  <li key={j}>{fact}</li>
                ))}
              </ul>
            </div>
          ))}
        </details>
      )}
      <p className="muted small">Every figure above was checked against the source text; unsupported ones were removed.</p>
    </div>
  );
}

export default function PageReport({ report, rag, isDocument }) {
  const isLlm = report.method === "llm";
  const sections = SECTIONS.filter(([key]) => report[key]?.length > 0);

  return (
    <section className="panel page-report">
      <div className="report-head">
        <h3>About this {isDocument ? "document" : "page"}</h3>
        <span className={isLlm ? "report-source ai" : "report-source basic"}>
          {isLlm ? `AI report · ${report.model}` : "Basic summary · no LLM"}
        </span>
      </div>

      {report.headline && <p className="report-headline">{report.headline}</p>}
      <p className="report-overview">{report.overview}</p>
      {report.audience && (
        <p className="small">
          <span className="muted">Audience: </span>
          {report.audience}
        </p>
      )}

      {sections.length > 0 && (
        <div className="facts-grid report-grid">
          {sections.map(([key, title]) => (
            <div key={key} className={key === "things_to_watch" ? "facts watch" : "facts"}>
              <h5>{title}</h5>
              <ul>
                {report[key].map((item, i) => (
                  <li key={i}>{item}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      <ReportDetails report={report} />

      {rag && (
        <p className="report-note small">
          Whole {isDocument ? "document" : "page"} read: {rag.characters.toLocaleString()} characters in {rag.parts}{" "}
          parts, {rag.chunks} chunks indexed with {rag.embedding}, and {rag.retrieved} passages retrieved for this
          summary.
        </p>
      )}

      {!isLlm && (
        <p className="report-note small">
          This summary was pieced together from text in the source, not written by an LLM. To get an AI report, keep
          Ollama running (default <code>ollama-local</code> profile) or set <code>OPENROUTER_API_KEY</code> in{" "}
          <code>.env</code>, then process it again.
        </p>
      )}
    </section>
  );
}
