import { useEffect, useState } from "react";
import {
  activateObsCredential, createObsCredential, deleteObsCredential, getObsSettings,
  setObsEnabled, testObsCredential, updateObsCredential,
} from "../api/client";
import type { OBSCredential, OBSCredentialInput, OBSSettings } from "../api/types";

const EMPTY_FORM: OBSCredentialInput = {
  name: "", endpoint: "", region: "", bucket: "", path_prefix: "", access_key: "", secret_key: "",
};

function timeAgo(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function ProfileCard({
  cred, testing, onActivate, onEdit, onDelete, onTest,
}: {
  cred: OBSCredential;
  testing: boolean;
  onActivate: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onTest: () => void;
}) {
  let status: React.ReactNode;
  if (testing) {
    status = <span className="profile-status is-untested"><span className="dot" /> Testing…</span>;
  } else if (cred.verified_ok === true) {
    status = (
      <span className="profile-status" title={cred.verified_detail ?? undefined}>
        <span className="dot" /> Verified{cred.verified_at ? ` ${timeAgo(cred.verified_at)}` : ""}
      </span>
    );
  } else if (cred.verified_ok === false) {
    status = (
      <span className="profile-status is-bad" title={cred.verified_detail ?? undefined}>
        <span className="dot" /> Unreachable
      </span>
    );
  } else {
    status = <span className="profile-status is-untested"><span className="dot" /> Not tested yet</span>;
  }

  return (
    <div className={`profile-card${cred.is_active ? " is-active" : ""}`}>
      <button
        className="profile-radio-btn"
        onClick={onActivate}
        disabled={cred.is_active}
        title={cred.is_active ? "This is the active credential" : "Make this the active credential"}
        type="button"
      >
        {cred.is_active && <span className="profile-radio-dot" />}
      </button>
      <div className="profile-body">
        <div className="profile-name-row">
          <span className="profile-name">{cred.name}</span>
          {cred.is_active && <span className="active-chip">Active</span>}
        </div>
        <div className="profile-meta" title={`${cred.endpoint} · bucket: ${cred.bucket}`}>
          {cred.endpoint} · bucket: {cred.bucket} · AK {cred.access_key.slice(0, 4)}•••• · SK {cred.secret_key_hint}
        </div>
      </div>
      {status}
      <div className="profile-actions">
        <button className="icon-btn" onClick={onTest} disabled={testing} type="button">⇄ Test</button>
        <button className="icon-btn" onClick={onEdit} title="Edit" type="button">✎</button>
        <button className="icon-btn danger" onClick={onDelete} title="Remove" type="button">✕</button>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<OBSSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<OBSCredentialInput>(EMPTY_FORM);
  const [secretVisible, setSecretVisible] = useState(false);

  function refresh() {
    return getObsSettings().then(setSettings);
  }

  useEffect(() => {
    refresh()
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load settings."))
      .finally(() => setLoading(false));
  }, []);

  async function toggleEnabled() {
    if (!settings) return;
    setBusy(true);
    setError(null);
    try {
      setSettings(await setObsEnabled(!settings.enabled));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update setting.");
    } finally {
      setBusy(false);
    }
  }

  function openCreateForm() {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setSecretVisible(false);
    setError(null);
    setShowForm(true);
  }

  function openEditForm(cred: OBSCredential) {
    setForm({
      name: cred.name, endpoint: cred.endpoint, region: cred.region ?? "", bucket: cred.bucket,
      path_prefix: cred.path_prefix, access_key: cred.access_key, secret_key: "",
    });
    setEditingId(cred.id);
    setSecretVisible(false);
    setError(null);
    setShowForm(true);
  }

  function closeForm() {
    setShowForm(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
  }

  async function saveForm() {
    setBusy(true);
    setError(null);
    try {
      const payload: OBSCredentialInput = {
        name: form.name.trim(),
        endpoint: form.endpoint.trim(),
        region: form.region?.trim() || undefined,
        bucket: form.bucket.trim(),
        path_prefix: form.path_prefix?.trim() || "",
        access_key: form.access_key.trim(),
        secret_key: form.secret_key,
      };
      if (editingId) {
        // A blank secret field means "leave the saved one alone" -- never
        // send an empty string, which would overwrite it with nothing.
        const updatePayload: Partial<OBSCredentialInput> = { ...payload };
        if (!updatePayload.secret_key) delete updatePayload.secret_key;
        await updateObsCredential(editingId, updatePayload);
      } else {
        await createObsCredential(payload);
      }
      await refresh();
      closeForm();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save credential.");
    } finally {
      setBusy(false);
    }
  }

  async function handleActivate(id: string) {
    setBusy(true);
    setError(null);
    try {
      setSettings(await activateObsCredential(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to activate credential.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(cred: OBSCredential) {
    if (!confirm(`Remove credential "${cred.name}"? This can't be undone.`)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteObsCredential(cred.id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove credential.");
    } finally {
      setBusy(false);
    }
  }

  async function handleTest(id: string) {
    setTestingId(id);
    setError(null);
    try {
      await testObsCredential(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Connection test failed to run.");
    } finally {
      setTestingId(null);
    }
  }

  if (loading) return <div className="page-wide">Loading…</div>;
  if (!settings) {
    return (
      <div className="page-wide">
        <div className="obs-inline-error">Could not load settings{error ? `: ${error}` : "."}</div>
      </div>
    );
  }

  const formValid = !!(
    form.name.trim() && form.endpoint.trim() && form.bucket.trim() && form.access_key.trim() &&
    (editingId || form.secret_key.trim())
  );

  return (
    <div className="page-wide">
      <div className="page-eyebrow">Deployment Configuration</div>
      <h1 className="page-title">Settings</h1>
      <p className="page-sub">Storage and access credentials for this deployment.</p>

      <div className="settings-tabs">
        <div className="settings-tab is-active">Storage</div>
      </div>

      <section className="report-card">
        <div className="settings-card-head">
          <div>
            <h3>Object Storage (OBS) <span className="badge-optional">Optional</span></h3>
            <p>Push a copy of every uploaded source file to an S3-compatible object storage bucket as it's ingested.</p>
          </div>
          <div className="toggle-col">
            <button
              className={`toggle-switch${settings.enabled ? " is-on" : ""}`}
              onClick={toggleEnabled}
              disabled={busy}
              role="switch"
              aria-checked={settings.enabled}
              aria-label="Enable OBS uploads"
              type="button"
            >
              <span className="toggle-switch-knob" />
            </button>
            <span className="toggle-state-label">
              {settings.enabled ? <><strong>On</strong> for this deployment</> : "Off — air-gapped default"}
            </span>
          </div>
        </div>

        <div className="air-gap-note">
          <span className="glyph">◈</span>
          <div>
            <strong>This deployment still runs fully air-gapped without it.</strong> Extraction, chat, and the
            knowledge graph never depend on OBS being configured — turning it on only adds an outbound copy step
            for uploads, for deployments that do have reachable network access to their own bucket.
          </div>
        </div>

        {error && <div className="obs-inline-error">{error}</div>}

        <div className="settings-sub-head">AK / SK Credentials</div>

        {settings.credentials.length === 0 && !showForm && (
          <p className="muted small" style={{ marginBottom: 14 }}>No credentials saved yet.</p>
        )}

        {settings.credentials.length > 0 && (
          <div className="profile-list">
            {settings.credentials.map((cred) => (
              <ProfileCard
                key={cred.id}
                cred={cred}
                testing={testingId === cred.id}
                onActivate={() => handleActivate(cred.id)}
                onEdit={() => openEditForm(cred)}
                onDelete={() => handleDelete(cred)}
                onTest={() => handleTest(cred.id)}
              />
            ))}
          </div>
        )}

        {!showForm && (
          <button className="add-credential-btn" onClick={openCreateForm} type="button">
            <span className="plus">+</span> Add {settings.credentials.length > 0 ? "another " : ""}AK / SK credential
          </button>
        )}

        {showForm && (
          <div className="obs-form-panel">
            <p className="obs-form-title">{editingId ? "Edit credential" : "New credential"}</p>
            <div className="obs-form-grid">
              <div className="obs-field span-2">
                <label htmlFor="f-name">Profile name</label>
                <input
                  id="f-name" type="text" placeholder="e.g. Primary — Production"
                  value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-endpoint">Endpoint URL</label>
                <input
                  id="f-endpoint" type="text" placeholder="obs.region.example-cloud.com"
                  value={form.endpoint} onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-region">Region <span className="obs-field-hint">— optional</span></label>
                <input
                  id="f-region" type="text" placeholder="eu-west-1"
                  value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-bucket">Bucket name</label>
                <input
                  id="f-bucket" type="text" placeholder="my-uploads-bucket"
                  value={form.bucket} onChange={(e) => setForm({ ...form, bucket: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-prefix">Path prefix <span className="obs-field-hint">— optional</span></label>
                <input
                  id="f-prefix" type="text" placeholder="dataloom/uploads/"
                  value={form.path_prefix} onChange={(e) => setForm({ ...form, path_prefix: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-ak">Access Key (AK)</label>
                <input
                  id="f-ak" type="text" placeholder="AKIA..."
                  value={form.access_key} onChange={(e) => setForm({ ...form, access_key: e.target.value })}
                />
              </div>
              <div className="obs-field">
                <label htmlFor="f-sk">
                  Secret Key (SK)
                  {editingId && <span className="obs-field-hint"> — leave blank to keep current</span>}
                </label>
                <div className="secret-input-wrap">
                  <input
                    id="f-sk" type={secretVisible ? "text" : "password"}
                    placeholder={editingId ? "•••• (unchanged)" : "Secret access key"}
                    value={form.secret_key} onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
                  />
                  <button type="button" className="reveal-btn" onClick={() => setSecretVisible((v) => !v)}>
                    {secretVisible ? "Hide" : "Show"}
                  </button>
                </div>
              </div>
            </div>

            <div className="field-security-note">
              <span>🔒</span>
              <div>
                The secret key is encrypted at rest and never sent back to this page after saving — only the
                access key and a masked hint are shown once a credential is stored.
              </div>
            </div>

            <div className="form-actions">
              <button className="btn-primary" disabled={!formValid || busy} onClick={saveForm} type="button">
                {editingId ? "Save changes" : "Save credential"}
              </button>
              <button className="btn-secondary" onClick={closeForm} disabled={busy} type="button">Cancel</button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
