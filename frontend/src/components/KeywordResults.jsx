function Passages({ passages }) {
  if (!passages.length) return null;
  const exact = passages.filter((p) => p.match === "exact").length;
  const related = passages.length - exact;
  const label = [exact && `${exact} exact`, related && `${related} related`].filter(Boolean).join(", ");
  return (
    <details className="passages">
      <summary className="small">Passages from the source ({label})</summary>
      {passages.map((passage, i) => (
        <blockquote key={i} className={`passage passage-${passage.match}`}>
          <span className="muted small">
            {passage.match === "exact" ? "Mentions it" : "Related in meaning"}
            {passage.heading && ` · ${passage.heading}`}
          </span>
          <span className="passage-text">{passage.text}</span>
        </blockquote>
      ))}
    </details>
  );
}

function Finding({ finding }) {
  const related = finding.passages.some((p) => p.match === "related");
  return (
    <div className={finding.found ? "finding" : "finding not-found"}>
      <div className="finding-head">
        <h4>{finding.keyword}</h4>
        <span className={finding.found ? "verdict-inline allow" : "verdict-inline block"}>
          {finding.found ? "found" : related ? "only related passages" : "not found"}
        </span>
      </div>
      {finding.summary && <p>{finding.summary}</p>}
      {finding.points.length > 0 && (
        <ul>
          {finding.points.map((point, i) => (
            <li key={i}>{point}</li>
          ))}
        </ul>
      )}
      {finding.items.length > 0 && (
        <p className="small">
          <span className="muted">Items: </span>
          {finding.items.join(", ")}
        </p>
      )}
      {!finding.found && !related && <p className="muted small">The source doesn't mention this keyword.</p>}
      <Passages passages={finding.passages} />
    </div>
  );
}

export default function KeywordResults({ findings, keywords, pending }) {
  return (
    <section className="panel keyword-results">
      <div className="report-head">
        <h3>Your keywords</h3>
        <span className="muted small">shown first · everything else follows below</span>
      </div>
      {pending ? (
        <p className="muted">Searching for {keywords.join(", ")}…</p>
      ) : (
        <div className="findings">
          {findings.map((finding) => (
            <Finding key={finding.keyword} finding={finding} />
          ))}
        </div>
      )}
      <p className="muted small">
        Summaries and points were checked against the source; figures it doesn't contain were removed.
      </p>
    </section>
  );
}
