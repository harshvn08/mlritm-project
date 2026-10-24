import React, { useMemo, useState } from 'react';
import { API, Case, OfficerSession, addOfficerUploadedCaseId } from '../services/api';
import { FileStore } from '../services/fileStore';

export interface DataIngestionModuleProps {
  officerSession: OfficerSession | null;
  fetchCases: () => Promise<Case[]>;
  changeView: (view: string) => void;
  addToast: (msg: string, type?: string) => void;
}

export function DataIngestionModule({ officerSession, fetchCases, changeView, addToast }: DataIngestionModuleProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [ingesting, setIngesting] = useState<boolean>(false);
  const [preview, setPreview] = useState<string[][]>([]);
  const [previewName, setPreviewName] = useState('');
  const supportedExtensions = ['.pdf', '.txt', '.csv', '.json', '.xlsx', '.docx'];
  const totalSize = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);

  const updateFiles = async (selected: File[]) => {
    setFiles(selected);
    setPreview([]);
    setPreviewName('');
    const previewFile = selected.find(file => /\.(csv|txt|json)$/i.test(file.name));
    if (!previewFile) return;
    try {
      const rows = (await previewFile.text()).split(/\r?\n/).filter(Boolean).slice(0, 6)
        .map(row => row.split(',').map(value => value.trim()).slice(0, 8));
      setPreview(rows);
      setPreviewName(previewFile.name);
    } catch {
      // The server remains the source of truth for parsing binary or malformed files.
    }
  };

  const handleFileUpload = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!files || files.length === 0) {
      addToast('Please select a CSV, JSON, TXT, PDF, XLSX, or DOCX file.', 'info');
      return;
    }

    const invalidFiles = files.filter(f => {
      const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
      return !supportedExtensions.includes(ext) || f.size === 0 || f.size > 20 * 1024 * 1024;
    });

    if (invalidFiles.length > 0) {
      addToast(`Unsupported, empty, or over-20 MB files: ${invalidFiles.map(f => f.name).join(', ')}`, 'err');
      return;
    }

    setIngesting(true);
    try {
      // Read file contents before sending so they can be archived into FileStore
      const filePayloads: Array<{ name: string; type: 'pdf' | 'csv' | 'txt' | 'document'; size: string; content: string }> = [];
      for (const f of files) {
        const extension = f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
        const type = extension === '.pdf' ? 'pdf' : extension === '.csv' ? 'csv' : extension === '.txt' ? 'txt' : 'document';
        let content = '';
        try {
          content = await f.text();
        } catch {
          content = `[Binary Document ${f.name} - Extracted by Ingestion Engine]`;
        }
        filePayloads.push({
          name: f.name,
          type,
          size: FileStore.formatBytes(f.size),
          content: content || `[Document content for ${f.name}]`
        });
      }

      const requestedCaseId = `CASE_${Date.now()}`;
      let targetCaseId = requestedCaseId;

      const formData = new FormData();
      files.forEach(file => formData.append('files', file));
      if (officerSession) {
        formData.append('uploaded_by', officerSession.badgeNumber);
        formData.append('officer_name', officerSession.officerName);
      }
      const res = await API.send<{ case_id: string; created: { nodes: number; relationships: number; source_records: number }; warnings?: string[] }>(
        `/api/v1/ingest?case_id=${encodeURIComponent(requestedCaseId)}`,
        { method: 'POST', body: formData, form: true }
      );
      targetCaseId = res.case_id;

      // Associate case with logged in officer badge
      if (officerSession) {
        addOfficerUploadedCaseId(officerSession.badgeNumber, targetCaseId);
      }

      // Persist to FileStore linked to the ingested case
      filePayloads.forEach(fp => {
        FileStore.saveFile({
          case_id: targetCaseId,
          file_name: fp.name,
          file_type: fp.type,
          file_size: fp.size,
          uploaded_at: new Date().toISOString().replace('T', ' ').substring(0, 19) + ' UTC',
          content: fp.content
        });
      });

      addToast(`Ingestion complete: ${res.created.nodes} nodes and ${res.created.relationships} relationships saved to ${targetCaseId}.`, 'ok');
      await fetchCases();
      changeView('cases');

    } catch (e) {
      addToast(`Upload notice: ${(e as Error).message}`, 'err');
    } finally {

      setIngesting(false);
    }
  };

  const removeFile = (idx: number) => {
    setFiles(prev => prev.filter((_, i) => i !== idx));
  };

  return (
    <div className="fade-in">
      <div className="card">
        <div className="card-header border-b pb-3 mb-4">
          <div>
            <div className="card-title text-base">
              <span>📄</span> Multi-Source Case Evidence Ingestion Engine
            </div>
            <div className="card-desc">
              Extract source-supported entities and evidence links locally. Offline rule-based extraction needs no API key; inferred associations remain candidates for analyst verification.
            </div>
          </div>
          <span className="tag open font-semibold">CSV · JSON · TXT · PDF · XLSX · DOCX</span>
        </div>

        <form onSubmit={handleFileUpload} className="flex flex-col gap-4">
          <div
            style={{
              padding: '36px 20px',
              border: '2px dashed var(--border-strong)',
              borderRadius: 'var(--radius-sm)',
              textAlign: 'center',
              background: 'var(--bg1)'
            }}
          >
            <div className="flex flex-col items-center gap-3">
              <div style={{ width: '48px', height: '48px', borderRadius: '12px', background: 'var(--surface)', border: '1px solid var(--border)', display: 'grid', placeItems: 'center', fontSize: '24px' }}>
                📁
              </div>
              <div>
                <div className="font-bold text-sm" style={{ color: 'var(--text)' }}>
                  Upload Case Documents & Evidence Files
                </div>
                <div className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>
                  Up to 20 MB per file. CSV headers for CDR or transaction records are detected automatically; unsupported or empty files are rejected.
                </div>
              </div>

              <label className="neu-btn primary cursor-pointer mt-2">
                <span>📂</span> Select Files to Upload
                <input
                  type="file"
                  multiple
                  accept=".pdf,.txt,.csv,.json,.xlsx,.docx"
                  onChange={e => {
                    if (e.target.files) {
                      void updateFiles(Array.from(e.target.files));
                    }
                  }}
                  style={{ display: 'none' }}
                />
              </label>
            </div>
          </div>

          {files.length > 0 && (
            <div>
              <div className="text-xs font-bold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                Selected Files Ready for Ingestion ({files.length}):
              </div>
              <div className="flex flex-col gap-2">
                {files.map((f, idx) => (
                  <div key={idx} className="flex items-center justify-between p-3 rounded-lg border" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
                    <div className="flex items-center gap-3">
                      <span style={{ fontSize: '18px' }}>
                        {f.name.endsWith('.pdf') ? '📜' : f.name.endsWith('.csv') || f.name.endsWith('.xlsx') ? '📊' : '📝'}
                      </span>
                      <div>
                        <div className="font-semibold text-sm" style={{ color: 'var(--text)' }}>{f.name}</div>
                        <div className="mono text-xs" style={{ color: 'var(--text-faint)' }}>{FileStore.formatBytes(f.size)}</div>
                      </div>
                    </div>
                    <button
                      type="button"
                      className="neu-btn ghost danger text-xs"
                      style={{ padding: '4px 8px' }}
                      onClick={() => removeFile(idx)}
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.length > 0 && (
            <div className="tbl-wrap">
              <div className="px-3 py-2 text-xs font-semibold" style={{ color: 'var(--text-dim)', borderBottom: '1px solid var(--border)' }}>
                Source preview: {previewName} (first {preview.length} rows)
              </div>
              <table>
                <tbody>{preview.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody>
              </table>
            </div>
          )}

          <div className="flex justify-end gap-3 mt-4 border-t pt-4" style={{ borderColor: 'var(--border)' }}>
            <button
              type="button"
              className="neu-btn ghost"
              onClick={() => {
                setFiles([]); setPreview([]);
                changeView('cases');
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="neu-btn primary font-semibold"
              disabled={ingesting || files.length === 0}
              style={{ minWidth: '170px' }}
            >
              {ingesting ? 'Processing Ingestion…' : 'Start Evidence Ingestion →'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
