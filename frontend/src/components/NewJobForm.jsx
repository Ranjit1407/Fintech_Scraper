import { useRef, useState } from "react";
import { api, formatTime } from "../api";
import ComplianceTable from "./ComplianceTable";

const SOURCES = [
  ["web", "Website"],
  ["docs", "Document folder"],
];

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

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const MAX_LISTED = 8;

function previousLabel(previous) {
  const when = formatTime(previous.processed_at);
  const what = previous.match === "identical" ? "Uploaded before" : "Newer version of a document uploaded";
  return `${what} · ${when} · ${previous.item_count} item${previous.item_count === 1 ? "" : "s"}`;
}

function uploadQuestion(fresh, again) {
  const docs = (n) => `${n} document${n === 1 ? "" : "s"}`;
  if (again === 0) return `Process ${fresh === 1 ? "this document" : `these ${fresh} documents`} now?`;
  const update = `${again === 1 ? "This document was" : `${again} of these documents were`} processed before. Re-process ${
    again === 1 ? "it" : "them"
  } and add the new data to the existing results?`;
  return fresh === 0 ? update : `${update} The other ${docs(fresh)} will be processed as new.`;
}

function UploadReview({ upload, busy, onProcess, onCancel }) {
  const { folderName, batch } = upload;
  const count = batch.files.length;
  const again = batch.files.filter((f) => f.previous).length;
  const busyElsewhere = batch.files.some((f) => f.previous && ["queued", "running"].includes(f.previous.status));
  const action = again === 0 ? "Process documents" : again === count ? "Update existing results" : "Process and update";
  return (
    <div className="dry-run panel upload-review">
      <p className="verdict allow">
        Uploaded {count} document{count === 1 ? "" : "s"} from <strong>{folderName}</strong>
      </p>
      <ul className="upload-files">
        {batch.files.slice(0, MAX_LISTED).map((file) => (
          <li key={file.name}>
            <span className="upload-name">
              <span className="break">{file.name}</span>
              {file.previous && <span className="reupload-note small">{previousLabel(file.previous)}</span>}
            </span>
            <span className="muted small">{formatBytes(file.bytes)}</span>
          </li>
        ))}
        {count > MAX_LISTED && <li className="muted small">and {count - MAX_LISTED} more</li>}
      </ul>
      {batch.skipped.length > 0 && (
        <p className="muted small">
          Skipped {batch.skipped.length}: {batch.skipped.map((s) => `${s.name} (${s.reason})`).join(", ")}
        </p>
      )}
      {again > 0 && (
        <div className="alert warn">
          Re-uploaded documents are not added as new entries. Their existing results are updated: new items and
          facts are added, changed values are replaced, and nothing found earlier is removed.
          {busyElsewhere && " One of them is still being processed from an earlier upload; wait for it to finish."}
        </div>
      )}
      <p className="upload-question">{uploadQuestion(count - again, again)}</p>
      <div className="actions">
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
          {busy === "cancel" ? "Cancelling…" : "Cancel upload"}
        </button>
        <button type="button" onClick={onProcess} disabled={busy || busyElsewhere}>
          {busy === "process" ? "Starting…" : action}
        </button>
      </div>
    </div>
  );
}

export default function NewJobForm({ onCreated }) {
  const [source, setSource] = useState("web");
  const [upload, setUpload] = useState(null);
  const [recursive, setRecursive] = useState(false);
  const folderInput = useRef(null);
  const filesInput = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [url, setUrl] = useState("");
  const [selected, setSelected] = useState(() => new Set(DATA_TYPES.map(([key]) => key)));
  const [renderMode, setRenderMode] = useState("auto");
  const [apiEndpoint, setApiEndpoint] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [wantKeywords, setWantKeywords] = useState(false);
  const [keywordText, setKeywordText] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [report, setReport] = useState(null);

  const toggle = (key) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const dataTypes = () => DATA_TYPES.map(([key]) => key).filter((key) => selected.has(key));

  const keywords = () => {
    if (!wantKeywords) return [];
    const list = parseKeywords(keywordText);
    if (list.length === 0) throw new Error("Enter at least one keyword, or answer No to the keyword question.");
    if (list.length > MAX_KEYWORDS) throw new Error(`Enter at most ${MAX_KEYWORDS} keywords.`);
    return list;
  };

  const uploadFolder = (fileList) => {
    const all = Array.from(fileList);
    if (all.length === 0) {
      setError("That folder is empty.");
      return;
    }
    const folderName = all[0].webkitRelativePath.split("/")[0];
    // webkitRelativePath is "<folder>/<file>" for top-level files, deeper for sub-folders
    const files = recursive ? all : all.filter((f) => f.webkitRelativePath.split("/").length <= 2);
    if (files.length === 0) {
      setError(`${folderName} has no files at its top level. Tick "Include sub-folders" to upload its sub-folders.`);
      return;
    }
    return uploadFiles(files, folderName);
  };

  const uploadSelectedFiles = (fileList) => {
    const files = Array.from(fileList);
    if (files.length === 0) return;
    return uploadFiles(files, files.length === 1 ? files[0].name : `${files.length} selected files`);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const entries = Array.from(e.dataTransfer.items || []).map((item) => item.webkitGetAsEntry?.());
    if (entries.some((entry) => entry?.isDirectory)) {
      setError("Folders can't be dropped here. Drop the documents themselves, or use Choose folder.");
      return;
    }
    uploadSelectedFiles(e.dataTransfer.files);
  };

  const uploadFiles = async (files, folderName) => {
    setBusy("upload");
    setError(null);
    try {
      if (upload) await api.cancelUpload(upload.batch.batch_id).catch(() => {});
      setUpload({ folderName, batch: await api.uploadDocuments(files) });
    } catch (err) {
      setUpload(null);
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const processUpload = async () => {
    if (selected.size === 0) {
      setError("Select at least one type of data to collect");
      setShowOptions(true);
      return;
    }
    setBusy("process");
    setError(null);
    try {
      const jobs = await api.processUpload(upload.batch.batch_id, { keywords: keywords(), data_types: dataTypes() });
      setUpload(null);
      onCreated(jobs[0]);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const cancelUpload = async () => {
    setBusy("cancel");
    try {
      await api.cancelUpload(upload.batch.batch_id);
    } catch {
      /* already gone */
    } finally {
      setUpload(null);
      setBusy(null);
    }
  };

  const run = async (kind) => {
    if (source === "docs") {
      filesInput.current?.click();
      return;
    }
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
        {source === "web"
          ? "Paste the address of any web page. Governance checks run first: if the site doesn't permit scraping, nothing is collected."
          : "Upload PDF, Word, text, Markdown or HTML documents, one by one or a whole folder. You review what was uploaded before anything is processed, and each document becomes its own job."}
      </p>

      <div className="source-switch" role="radiogroup" aria-label="Source">
        {SOURCES.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={source === key}
            className={source === key ? "active" : ""}
            onClick={() => {
              setSource(key);
              setError(null);
              setReport(null);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <form
        className={dragging ? "url-form dragging" : "url-form"}
        onSubmit={(e) => {
          e.preventDefault();
          run("scrape");
        }}
        onDragOver={(e) => {
          if (source !== "docs") return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => (source === "docs" ? onDrop(e) : undefined)}
      >
        {source === "web" ? (
          <>
            <label htmlFor="url-input" className="sr-only">
              Website URL
            </label>
            <input
              id="url-input"
              key="url"
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
          </>
        ) : (
          <>
            <input
              ref={filesInput}
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md,.markdown,.html,.htm"
              hidden
              onChange={(e) => {
                uploadSelectedFiles(e.target.files);
                e.target.value = ""; // allow choosing the same files again
              }}
            />
            <input
              ref={folderInput}
              type="file"
              webkitdirectory=""
              multiple
              hidden
              onChange={(e) => {
                uploadFolder(e.target.files);
                e.target.value = "";
              }}
            />
            <span className="url-input folder-status">
              {busy === "upload"
                ? "Uploading…"
                : dragging
                  ? "Drop to upload"
                  : upload
                    ? `${upload.folderName}: ${upload.batch.files.length} document(s) uploaded`
                    : "Drop documents here, or choose files or a folder"}
            </span>
            <button
              type="button"
              className="url-submit secondary"
              disabled={busy}
              onClick={() => folderInput.current?.click()}
            >
              Choose folder
            </button>
          </>
        )}
        <button
          type="submit"
          className="url-submit"
          disabled={source === "web" ? !url.trim() || busy : busy}
        >
          {source === "web"
            ? busy === "scrape"
              ? "Starting…"
              : "Start scrape"
            : busy === "upload"
              ? "Uploading…"
              : "Choose files"}
        </button>
      </form>

      {source === "docs" && !upload && (
        <p className="muted small upload-hint">
          The folder picker lists folders only. Select the folder and click <strong>Upload</strong>; its documents
          are listed here before anything is processed.
        </p>
      )}

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

      {source === "docs" && upload && (
        <UploadReview upload={upload} busy={busy} onProcess={processUpload} onCancel={cancelUpload} />
      )}

      <div className="hero-links">
        {source === "web" ? (
          <button type="button" className="link" disabled={!url.trim() || busy} onClick={() => run("check")}>
            {busy === "check" ? "Running governance checks…" : "Run governance checks only"}
          </button>
        ) : (
          <label className="check">
            <input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} />
            Include sub-folders
          </label>
        )}
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
          {source === "web" && (
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
          )}
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
