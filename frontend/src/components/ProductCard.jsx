import { isDocument, percent, sourceLabel } from "../api";

function FactList({ title, items }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="facts">
      <h5>{title}</h5>
      <ul>
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export default function ProductCard({ product }) {
  const summary = product.summary || {};
  const references = product.references || [];
  const reference = references[references.length - 1]; // latest upload or scrape
  return (
    <article className="product">
      <header className="product-head">
        <div>
          <h4>{product.name}</h4>
          <p className="muted small">{product.provider || "Unknown provider"}</p>
        </div>
        <div className="product-tags">
          <span className="chip">{product.category}</span>
          <span className="muted small" title={`Extracted via ${product.extraction_method}`}>
            {percent(product.confidence)} confidence
          </span>
        </div>
      </header>

      {summary.summary && <p>{summary.summary}</p>}

      <div className="facts-grid">
        <FactList title="Fees & rates" items={summary.fees_and_rates} />
        <FactList title="Key features" items={summary.key_features} />
        <FactList title="Eligibility" items={summary.eligibility} />
        <FactList title="Important conditions" items={summary.important_conditions} />
      </div>

      {product.data?.unverified?.length > 0 && (
        <p className="unverified small">
          <strong>Removed as not supported by the source:</strong> {product.data.unverified.join("; ")}
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
