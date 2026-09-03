import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { browseObsCredential, getObsSettings, ingestFromObs, uploadFiles } from "../api/client";
import type { OBSObjectSummary, OBSSettings } from "../api/types";
import { RobotIcon } from "../components/RobotIcon";

const SUPPORTED = [
  ["PDF", "📕"], ["Images", "🖼️"], ["CSV / TSV", "📊"], ["JSON / JSONL", "{ }"],
  ["Excel", "📗"], ["Office", "📄"], ["Database", "🗄️"], ["Code & Logs", "🧾"],
  ["Archives", "🗜️"], ["Web / XML", "🌐"],
];

const FLEET = ["Orchestrator", "Extraction Agents", "Synthesiser", "Validator"];

type SourceMode = "upload" | "obs";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function UploadPage() {
  const [mode, setMode] = useState<SourceMode>("upload");
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  // Object storage source -- the "From Object Storage" tab is always
  // visible (see obsAvailable below) so people discover the feature exists
  // even on a deployment that hasn't configured it yet; the tab's own
  // content then prompts to finish setup in Settings instead of hiding.
  const [obsSettings, setObsSettings] = useState<OBSSettings | null>(null);
  const [obsCredentialId, setObsCredentialId] = useState("");
  const [obsPrefix, setObsPrefix] = useState("");
  const [obsBrowsing, setObsBrowsing] = useState(false);
  const [obsBrowseError, setObsBrowseError] = useState<string | null>(null);
  const [obsObjects, setObsObjects] = useState<OBSObjectSummary[]>([]);
  const [obsTruncated, setObsTruncated] = useState(false);
  const [obsSelected, setObsSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    getObsSettings()
      .then((s) => {
        setObsSettings(s);
        const active = s.credentials.find((c) => c.is_active) ?? s.credentials[0];
        if (active) setObsCredentialId(active.id);
      })
      .catch(() => {
        // OBS is an optional feature -- if this deployment's backend is
        // older or the call fails for any reason, obsSettings just stays
        // null and the tab's content falls back to the "set it up in
        // Settings" prompt, same as a freshly-disabled deployment. The
        // default upload flow below doesn't depend on this succeeding.
      });
  }, []);

  const obsAvailable = !!(obsSettings?.enabled && obsSettings.credentials.length > 0);

  const addFiles = useCallback((incoming: FileList | null) => {
    if (!incoming) return;
    setFiles((prev) => [...prev, ...Array.from(incoming)]);
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      addFiles(e.dataTransfer.files);
    },
    [addFiles]
  );

  const removeFile = (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx));

  const submit = async () => {
    if (files.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const job = await uploadFiles(files);
      navigate(`/jobs/${job.job_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
      setSubmitting(false);
    }
  };

  async function browseObs() {
    if (!obsCredentialId) return;
    setObsBrowsing(true);
    setObsBrowseError(null);
    try {
      const result = await browseObsCredential(obsCredentialId, obsPrefix);
      setObsObjects(result.objects);
      setObsTruncated(result.truncated);
      setObsSelected(new Set());
    } catch (e) {
      setObsBrowseError(e instanceof Error ? e.message : "Failed to browse object storage.");
      setObsObjects([]);
    } finally {
      setObsBrowsing(false);
    }
  }

  function toggleObsKey(key: string) {
    setObsSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  function selectAllObs() {
    setObsSelected(new Set(obsObjects.map((o) => o.key)));
  }

  const submitObs = async () => {
    if (obsSelected.size === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const job = await ingestFromObs(obsCredentialId, Array.from(obsSelected));
      navigate(`/jobs/${job.job_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start analysis from object storage.");
      setSubmitting(false);
    }
  };

  return (
    <div className="landing">
      <div className="bot-hero-wrap">
        <RobotIcon className="bot-hero" visorClassName="visor" />
      </div>

      <h1 className="landing-title">New Analysis</h1>
      <p className="landing-sub">
        Drop in contracts, invoices, spreadsheets, scans — anything. Data Loom routes
        each file to the right extraction agent, runs everything on your on-prem Qwen and Kimi2
        models, and hands back a briefing you can question directly.
      </p>

      <div className="source-mode-tabs">
        <button
          className={`source-mode-tab${mode === "upload" ? " is-active" : ""}`}
          onClick={() => setMode("upload")}
          type="button"
        >
          Upload Files
        </button>
        <button
          className={`source-mode-tab${mode === "obs" ? " is-active" : ""}`}
          onClick={() => setMode("obs")}
          type="button"
        >
          From Object Storage
        </button>
      </div>

      {mode === "upload" && (
        <>
          <div
            className={`dropzone ${dragging ? "is-drag" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => document.getElementById("file-input")?.click()}
          >
            <input id="file-input" type="file" multiple hidden onChange={(e) => addFiles(e.target.files)} />
            <div className="dropzone-ico">⇪</div>
            <div className="dropzone-title">Drag &amp; drop files here, or click to browse</div>
            <div className="dropzone-sub">No size limit set locally · nothing leaves this network</div>
          </div>

          <div className="filetype-row">
            {SUPPORTED.map(([name, icon]) => (
              <span key={name} className="filetype-chip">{icon} {name}</span>
            ))}
          </div>

          {files.length > 0 && (
            <div className="file-list scroll-list">
              {files.map((f, i) => (
                <div key={`${f.name}-${i}`} className="file-row">
                  <span className="file-name">{f.name}</span>
                  <span className="file-size">{(f.size / 1024).toFixed(1)} KB</span>
                  <button className="file-remove" onClick={() => removeFile(i)}>✕</button>
                </div>
              ))}
            </div>
          )}

          {error && <div className="error-banner">{error}</div>}

          <button className="landing-cta" disabled={files.length === 0 || submitting} onClick={submit}>
            {submitting ? "Starting analysis…" : `Analyze ${files.length || ""} file${files.length === 1 ? "" : "s"} →`}
          </button>
        </>
      )}

      {mode === "obs" && !obsAvailable && (
        <div className="obs-source-panel obs-source-empty">
          <p className="muted">Object Storage isn't set up on this deployment yet.</p>
          <p className="muted small">
            Enable it and add a bucket credential in Settings to start an analysis directly from a
            bucket, without uploading files by hand.
          </p>
          <Link to="/settings" className="btn-secondary">Open Settings →</Link>
        </div>
      )}

      {mode === "obs" && obsAvailable && (
        <div className="obs-source-panel">
          <div className="obs-source-row">
            <div className="obs-field">
              <label htmlFor="obs-cred">Credential</label>
              <select id="obs-cred" value={obsCredentialId} onChange={(e) => setObsCredentialId(e.target.value)}>
                {obsSettings?.credentials.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}{c.is_active ? " (active)" : ""}</option>
                ))}
              </select>
            </div>
            <div className="obs-field" style={{ flex: 1 }}>
              <label htmlFor="obs-prefix">Path / prefix</label>
              <input
                id="obs-prefix" type="text" placeholder="e.g. incoming/2026-q1/"
                value={obsPrefix} onChange={(e) => setObsPrefix(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") browseObs(); }}
              />
            </div>
            <button className="btn-secondary obs-browse-btn" onClick={browseObs} disabled={obsBrowsing} type="button">
              {obsBrowsing ? "Browsing…" : "Browse"}
            </button>
          </div>

          {obsBrowseError && <div className="error-banner">{obsBrowseError}</div>}

          {obsObjects.length > 0 && (
            <>
              <div className="obs-object-list-head">
                <span className="muted small">{obsObjects.length} object{obsObjects.length === 1 ? "" : "s"} found{obsTruncated ? " (more than shown — narrow the prefix)" : ""}</span>
                <button className="btn-secondary" onClick={selectAllObs} type="button">Select all</button>
              </div>
              <div className="obs-object-list scroll-list">
                {obsObjects.map((o) => (
                  <label key={o.key} className="obs-object-row">
                    <input type="checkbox" checked={obsSelected.has(o.key)} onChange={() => toggleObsKey(o.key)} />
                    <span className="file-name mono small">{o.key}</span>
                    <span className="file-size">{formatSize(o.size)}</span>
                  </label>
                ))}
              </div>
            </>
          )}

          {!obsBrowsing && !obsBrowseError && obsObjects.length === 0 && (
            <p className="muted small">Browse a bucket path above to select files.</p>
          )}

          {error && <div className="error-banner">{error}</div>}

          <button className="landing-cta" disabled={obsSelected.size === 0 || submitting} onClick={submitObs}>
            {submitting
              ? "Starting analysis…"
              : `Analyze ${obsSelected.size || ""} file${obsSelected.size === 1 ? "" : "s"} from Object Storage →`}
          </button>
        </div>
      )}

      <div className="section-caption">Agents standing by</div>
      <div className="agent-fleet">
        {FLEET.map((label) => (
          <div key={label} className="agent-card">
            <div className="agent-bot-wrap">
              <RobotIcon className="agent-bot" visorClassName="visor" />
            </div>
            <div className="agent-status-row-fleet"><span className="agent-status-dot-fleet" />Ready</div>
            <div className="agent-label">{label}</div>
          </div>
        ))}
      </div>

      <div className="landing-meta-row">
        <span>Qwen · translation</span>
        <span>Kimi2 · extraction, synthesis &amp; chat</span>
        <span>Neo4j · knowledge graph</span>
      </div>
    </div>
  );
}
