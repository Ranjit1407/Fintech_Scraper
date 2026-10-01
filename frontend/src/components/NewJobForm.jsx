import { useEffect, useState } from "react";
import { api } from "../api";
import ComplianceTable from "./ComplianceTable";

const DATA_TYPES = [
  ["items", "Items (things the source lists)"],
  ["tables", "Raw tables"],
  ["text", "Page text"],
  ["metadata", "Metadata"],
];
const MAX_KEYWORDS = 10;

function parseKeywords(text) {
  const seen = new Set();
  return text
    .split(/[,;\n]/)
    .map((k) => k.trim())
    .filter((k) => k && !seen.has(k.toLowerCase()) && seen.add(k.toLowerCase()));
}

/** Accepts "example.com/cards" as well as full URLs; returns null when it is not a usable web address. */
export function normalizeUrl(value) {
  let text = value.trim();
  if (!text) return null;
  if (!/^https?:\/\//i.test(text)) text = `https://${text}`;
  try {
    const url = new URL(text);
    return url.hostname.includes(".") ? url.toString() : null;
  } catch {
    return null;
  }
}

export default function NewJobForm({ onCreated }) {
  const [url, setUrl] = useState("");
  const [selected, setSelected] = useState(() => new Set(DATA_TYPES.map(([key]) => key)));
  const [renderMode, setRenderMode] = useState("auto");
  const [apiEndpoint, setApiEndpoint] = useState("");
  const [profiles, setProfiles] = useState({ default: null, profiles: [] });
  const [profile, setProfile] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [wantKeywords, setWantKeywords] = useState(false);
  const [keywordText, setKeywordText] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [report, setReport] = useState(null);

  useEffect(() => {
    api.llmProfiles().then(setProfiles, () => undefined); // the picker is hidden if the list cannot be read
  }, []);

  const toggle = (key) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const dataTypes = () => DATA_TYPES.map(([key]) => key).filter((key) => selected.has(key));

  const selectedProfile = profiles.profiles.find((item) => item.name === (profile || profiles.default));

  const keywords = () => {
    if (!wantKeywords) return [];
    const list = parseKeywords(keywordText);
    if (list.length === 0) throw new Error("Enter at least one keyword, or answer No to the keyword question.");
    if (list.length > MAX_KEYWORDS) throw new Error(`Enter at most ${MAX_KEYWORDS} keywords.`);
    return list;
  };

  const run = async (kind) => {
    const target = normalizeUrl(url);
    if (!target) {
      setError("Enter a website address, for example www.example.com/pricing");
      return;
    }
    if (selected.size === 0) {
      setError("Select at least one type of data to collect");
      setShowOptions(true);
      return;
    }
    let keywordList;
    try {
      keywordList = keywords();
    } catch (err) {
      setError(err.message);
      return;
    }
    const body = {
      url: target,
      keywords: keywordList,
      data_types: dataTypes(),
      render_mode: renderMode,
      api_endpoint: apiEndpoint.trim() ? normalizeUrl(apiEndpoint) : null,
      llm_profile: profile || null,
    };
    setBusy(kind);
    setError(null);
    try {
      if (kind === "check") {
        setReport(await api.checkCompliance(body));
      } else {
        const job = await api.createJob(body);
        setReport(null);
        setUrl("");
        onCreated(job);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="hero">
      <h2 className="hero-title">What should we analyse?</h2>
      <p className="hero-lead muted">
        Paste the address of any web page. Governance checks run first: if the site doesn't permit scraping,
        nothing is collected.
      </p>

      <form
        className="url-form"
        onSubmit={(e) => {
          e.preventDefault();
          run("scrape");
        }}
      >
        <label htmlFor="url-input" className="sr-only">
          Website URL
        </label>
        <input
          id="url-input"
          className="url-input"
          type="text"
          inputMode="url"
          autoComplete="url"
          spellCheck="false"
          autoFocus
          placeholder="www.example.com/pricing"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setError(null);
          }}
        />
        <button type="submit" className="url-submit" disabled={!url.trim() || busy}>
          {busy === "scrape" ? "Starting…" : "Start scrape"}
        </button>
      </form>

      <div className="keyword-ask">
        <span className="keyword-question">Do you have keywords to look for?</span>
        <div className="source-switch compact" role="radiogroup" aria-label="Keywords">
          {[
            [false, "No"],
            [true, "Yes"],
          ].map(([value, label]) => (
            <button
              key={label}
              type="button"
              role="radio"
              aria-checked={wantKeywords === value}
              className={wantKeywords === value ? "active" : ""}
              onClick={() => {
                setWantKeywords(value);
                setError(null);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {wantKeywords && (
        <div className="keyword-entry">
          <label htmlFor="keyword-input" className="sr-only">
            Keywords
          </label>
          <input
            id="keyword-input"
            className="keyword-input"
            type="text"
            autoFocus
            placeholder="e.g. pricing, refund policy, opening hours"
            value={keywordText}
            onChange={(e) => {
              setKeywordText(e.target.value);
              setError(null);
            }}
          />
          <p className="muted small">
            Separate keywords with commas (up to {MAX_KEYWORDS}). Results for your keywords are shown first, then
            everything else. Personal identifiers such as email addresses or phone numbers are not accepted.
          </p>
        </div>
      )}

      {error && <div className="alert error">{error}</div>}

      <div className="hero-links">
        <button type="button" className="link" disabled={!url.trim() || busy} onClick={() => run("check")}>
          {busy === "check" ? "Running governance checks…" : "Run governance checks only"}
        </button>
        <span className="muted" aria-hidden="true">·</span>
        <button
          type="button"
          className="link"
          aria-expanded={showOptions}
          onClick={() => setShowOptions((v) => !v)}
        >
          {showOptions ? "Hide options" : `Options (${selected.size}/${DATA_TYPES.length} data types)`}
        </button>
      </div>

      {showOptions && (
        <div className="options panel">
          <fieldset className="field">
            <legend>Data to collect</legend>
            <div className="checks">
              {DATA_TYPES.map(([key, label]) => (
                <label key={key} className="check">
                  <input type="checkbox" checked={selected.has(key)} onChange={() => toggle(key)} />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>
          {profiles.profiles.length > 0 && (
            <label className="field">
              <span>Model</span>
              <select value={profile} onChange={(e) => setProfile(e.target.value)}>
                <option value="">Default{profiles.default ? ` (${profiles.default})` : ""}</option>
                {profiles.profiles.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                    {item.enabled ? "" : " · not usable yet"}
                  </option>
                ))}
              </select>
              <span className="muted small">{selectedProfile?.description || "Set in config/llm.toml"}</span>
              {selectedProfile && !selectedProfile.enabled && (
                <span className="muted small">{selectedProfile.reason} — the report falls back to a basic summary.</span>
              )}
            </label>
          )}
          <div className="options-row">
            <label className="field">
              <span>Rendering</span>
              <select value={renderMode} onChange={(e) => setRenderMode(e.target.value)}>
                <option value="auto">Auto (browser only for JavaScript pages)</option>
                <option value="http">Static HTML only</option>
                <option value="browser">Always render in a browser</option>
              </select>
            </label>
            <label className="field">
              <span>Official API endpoint (optional)</span>
              <input
                type="text"
                inputMode="url"
                placeholder="api.example.com/v1/items"
                value={apiEndpoint}
                onChange={(e) => setApiEndpoint(e.target.value)}
              />
            </label>
          </div>
        </div>
      )}

      {report && (
        <div className="dry-run panel">
          <p className={report.allowed ? "verdict allow" : "verdict block"}>
            {report.allowed ? "Allowed: scraping can go ahead" : "Blocked: scraping would stop at this step"}
          </p>
          <ComplianceTable checks={report.checks} compact />
          {report.allowed && (
            <div className="actions">
              <button type="button" onClick={() => run("scrape")} disabled={busy}>
                Start scrape
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
