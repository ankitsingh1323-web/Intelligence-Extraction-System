import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { deleteJob, listJobs } from "../api/client";
import type { Job } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";

const ACTIVE_STATUSES = new Set(["queued", "parsing", "extracting", "graph_build", "synthesizing"]);

export default function HistoryPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    listJobs()
      .then((j) => { setJobs(j); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load job history."))
      .finally(() => setLoading(false));
  }, []);

  async function handleDelete(e: React.MouseEvent, job: Job) {
    e.preventDefault();
    e.stopPropagation();
    if (ACTIVE_STATUSES.has(job.status)) {
      alert(`"${job.name || job.job_id}" is still ${job.status} — wait for it to finish before deleting it.`);
      return;
    }
    if (!confirm(`Delete "${job.name || job.job_id}"? This removes its uploaded files and all generated outputs. This can't be undone.`)) {
      return;
    }
    setDeletingId(job.job_id);
    try {
      await deleteJob(job.job_id);
      setJobs((prev) => prev.filter((j) => j.job_id !== job.job_id));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to delete job.");
    } finally {
      setDeletingId(null);
    }
  }

  if (loading) return <div className="page-narrow">Loading…</div>;

  return (
    <div className="page-narrow">
      <h1 className="page-title">Job History</h1>
      {error && <div className="error-banner">Couldn't load job history: {error}</div>}
      {!error && jobs.length === 0 && <p className="page-sub">No analyses run yet.</p>}
      <div className="job-list">
        {jobs.map((job) => (
          <Link key={job.job_id} to={`/jobs/${job.job_id}`} className="job-row">
            <span className="job-id">{job.name || job.job_id}</span>
            <span className="job-files">{job.files.length} file(s)</span>
            <StatusBadge status={job.status} />
            <span className="job-date">{new Date(job.created_at).toLocaleString()}</span>
            <button
              className="job-delete-btn"
              title="Delete job"
              disabled={deletingId === job.job_id}
              onClick={(e) => handleDelete(e, job)}
            >
              {deletingId === job.job_id ? "…" : "🗑"}
            </button>
          </Link>
        ))}
      </div>
    </div>
  );
}
