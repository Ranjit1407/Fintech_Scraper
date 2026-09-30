import { isDocument, percent, sourceLabel } from "../api";

export default function ItemCard({ item }) {
  const data = item.data || {};
  const attributes = Object.entries(data.attributes || {});
  const matched = data.matched_keywords || [];
  const references = item.references || [];
  const reference = references[references.length - 1]; // latest upload or scrape
  return (
    <article className={matched.length ? "item-card matched" : "item-card"}>
      <header className="item-head">
        <div>
          <h4>{item.name}</h4>
          {matched.length > 0 && (
            <div className="chips">
              {matched.map((keyword) => (
                <span key={keyword} className="chip keyword-chip">
                  {keyword}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="item-tags">
          <span className="chip">{item.type}</span>
          <span className="muted small" title={`Extracted via ${item.extraction_method}`}>
            {percent(item.confidence)} confidence
          </span>
        </div>
      </header>

      {item.description && <p>{item.description}</p>}

      {attributes.length > 0 && (
        <dl className="attributes">
          {attributes.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {data.unverified?.length > 0 && (
        <p className="unverified small">
          <strong>Removed as not supported by the source:</strong> {data.unverified.join("; ")}
        </p>
      )}

      {reference && (
        <footer className="source">
          <span className="muted small">Source: </span>
          {isDocument(reference.url) ? (
            <span title={decodeURIComponent(reference.url)}>{sourceLabel(reference.url)}</span>
          ) : (
            <a href={reference.url} target="_blank" rel="noreferrer">
              {reference.url}
            </a>
          )}
          {references.length > 1 && (
            <span className="muted small"> · updated from {references.length} uploads</span>
          )}
          {reference.snippet && <blockquote>“{reference.snippet}”</blockquote>}
        </footer>
      )}
    </article>
  );
}
