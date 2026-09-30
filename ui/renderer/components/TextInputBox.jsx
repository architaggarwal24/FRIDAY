/**
 * F.R.I.D.A.Y. — components/TextInputBox.jsx
 * Lets you type a query instead of speaking it. Goes through the exact
 * same pipeline as voice input on the backend (see start.py
 * _handle_user_text) — intent classification, LLM response, and FRIDAY
 * speaks the answer same as she would for a spoken query.
 *
 * File attachment: pick via the paperclip, drag-and-drop anywhere onto
 * the bar, or paste an image straight from the clipboard. One file at a
 * time — the backend saves it and hands the LLM the path (see
 * ws_server.py's "upload_file" handler), so whatever gets attached is
 * available to file_processor/file_controller same as any other path.
 */

import React, { useState, useCallback, useRef } from "react";

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024; // keep in sync with ws_server.py's _MAX_UPLOAD_BYTES

function humanSize(n) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

function fileGlyph(file) {
  const type = file.type || "";
  if (type.startsWith("image/")) return "🖼";
  if (type.startsWith("audio/")) return "♪";
  if (type.startsWith("video/")) return "▶";
  if (type === "application/pdf") return "▤";
  if (/zip|rar|7z|tar|gzip/.test(type)) return "🗀";
  return "📄";
}

export default function TextInputBox({ onSend, onSendFile, connected, disabled }) {
  const [value, setValue] = useState("");
  const [file, setFile] = useState(null);       // { file, previewUrl? }
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  const dragDepth = useRef(0);

  const attachFile = useCallback((f) => {
    if (!f) return;
    setUploadError("");
    if (f.size > MAX_UPLOAD_BYTES) {
      setUploadError(`That's over the ${humanSize(MAX_UPLOAD_BYTES)} limit.`);
      return;
    }
    const previewUrl = f.type.startsWith("image/") ? URL.createObjectURL(f) : null;
    setFile({ file: f, previewUrl });
  }, []);

  const clearFile = useCallback(() => {
    setFile((prev) => {
      if (prev?.previewUrl) URL.revokeObjectURL(prev.previewUrl);
      return null;
    });
    setUploadError("");
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const submit = useCallback(async () => {
    const text = value.trim();
    if (!connected || uploading) return;
    if (!text && !file) return;

    if (file) {
      setUploading(true);
      setUploadError("");
      try {
        await onSendFile(file.file, text);
        setValue("");
        clearFile();
      } catch (e) {
        setUploadError(e?.message || "Upload failed — try again?");
      } finally {
        setUploading(false);
      }
      return;
    }

    const ok = onSend(text);
    if (ok !== false) setValue("");
  }, [value, file, connected, uploading, onSend, onSendFile, clearFile]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape" && file) {
      clearFile();
    }
  }, [submit, file, clearFile]);

  const handlePaste = useCallback((e) => {
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) { e.preventDefault(); attachFile(f); }
        break;
      }
    }
  }, [attachFile]);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    const f = e.dataTransfer?.files?.[0];
    if (f) attachFile(f);
  }, [attachFile]);

  const handleDragEnter = useCallback((e) => {
    e.preventDefault();
    dragDepth.current += 1;
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    dragDepth.current -= 1;
    if (dragDepth.current <= 0) { dragDepth.current = 0; setDragOver(false); }
  }, []);

  const disabledAll = !connected || disabled;
  const canSubmit = !disabledAll && !uploading && (value.trim() || file);

  return (
    <div
      style={{ ...styles.outer, ...(dragOver ? styles.outerDragOver : null) }}
      onDragEnter={handleDragEnter}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragOver && (
        <div style={styles.dropOverlay}>
          <span style={styles.dropText}>DROP TO ATTACH</span>
        </div>
      )}

      {file && (
        <div style={styles.chipRow}>
          <div style={styles.chip}>
            {file.previewUrl
              ? <img src={file.previewUrl} alt="" style={styles.chipThumb} />
              : <span style={styles.chipGlyph}>{fileGlyph(file.file)}</span>}
            <span style={styles.chipName} title={file.file.name}>{file.file.name}</span>
            <span style={styles.chipSize}>{humanSize(file.file.size)}</span>
            {!uploading && (
              <button onClick={clearFile} style={styles.chipRemove} title="Remove">✕</button>
            )}
            {uploading && <span style={styles.chipUploading}>UPLOADING…</span>}
          </div>
        </div>
      )}

      {uploadError && <div style={styles.errorRow}>{uploadError}</div>}

      <div style={styles.wrap}>
        <span style={styles.caret}>{">"}</span>

        <input
          type="file"
          ref={fileInputRef}
          style={{ display: "none" }}
          onChange={(e) => { attachFile(e.target.files?.[0]); e.target.value = ""; }}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={disabledAll || uploading}
          style={{
            ...styles.attachBtn,
            opacity: (disabledAll || uploading) ? 0.3 : 1,
            cursor: (disabledAll || uploading) ? "default" : "pointer",
          }}
          title="Attach a file"
        >
          📎
        </button>

        <input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            !connected ? "Not connected…"
            : file ? "Add a note about this file (optional)…"
            : "Type a command or question… (or drop a file)"
          }
          disabled={disabledAll}
          style={styles.input}
          spellCheck={false}
          autoComplete="off"
        />
        <button
          onClick={submit}
          disabled={!canSubmit}
          style={{
            ...styles.sendBtn,
            opacity: canSubmit ? 1 : 0.3,
            cursor: canSubmit ? "pointer" : "default",
          }}
        >
          {uploading ? "…" : "SEND"}
        </button>
      </div>
    </div>
  );
}

const styles = {
  outer: {
    position: "relative", flexShrink: 0,
    borderTop: "1px solid rgba(var(--accent-rgb), 0.1)",
    borderBottomLeftRadius: 12, borderBottomRightRadius: 12,
    background: "var(--input-bg)",
    backdropFilter: "blur(var(--panel-blur))", WebkitBackdropFilter: "blur(var(--panel-blur))",
    transition: "background 0.15s ease, box-shadow 0.15s ease",
  },
  outerDragOver: {
    background: "rgba(var(--accent-rgb), 0.06)",
    boxShadow: "inset 0 0 0 1.5px rgba(var(--accent-rgb), 0.45)",
  },
  dropOverlay: {
    position: "absolute", inset: 0, zIndex: 2,
    display: "flex", alignItems: "center", justifyContent: "center",
    pointerEvents: "none",
  },
  dropText: {
    fontSize: 10, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 3, color: "var(--accent)", fontWeight: 700,
  },
  chipRow: { padding: "8px 14px 0 14px" },
  chip: {
    display: "inline-flex", alignItems: "center", gap: 8,
    maxWidth: "100%", padding: "5px 8px 5px 6px",
    background: "rgba(var(--accent-rgb), 0.07)",
    border: "1px solid rgba(var(--accent-rgb), 0.25)",
    borderRadius: 8,
  },
  chipThumb: {
    width: 22, height: 22, borderRadius: 4, objectFit: "cover", flexShrink: 0,
  },
  chipGlyph: { fontSize: 13, flexShrink: 0 },
  chipName: {
    fontSize: 11, color: "#e5e5e5", fontFamily: "'JetBrains Mono', monospace",
    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 220,
  },
  chipSize: {
    fontSize: 9, color: "#666", fontFamily: "'JetBrains Mono', monospace", flexShrink: 0,
  },
  chipRemove: {
    background: "transparent", border: "none", color: "#888", cursor: "pointer",
    fontSize: 11, padding: "0 2px", lineHeight: 1, flexShrink: 0,
  },
  chipUploading: {
    fontSize: 9, color: "var(--accent)", fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 1.5, flexShrink: 0,
  },
  errorRow: {
    padding: "6px 14px 0 14px", fontSize: 10, color: "#ef4444",
    fontFamily: "'JetBrains Mono', monospace",
  },
  wrap: {
    display: "flex", alignItems: "center", gap: 8,
    height: 38, padding: "0 14px",
  },
  caret: {
    color: "var(--accent)", fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13, fontWeight: 700, flexShrink: 0,
  },
  attachBtn: {
    flexShrink: 0, background: "transparent", border: "none",
    fontSize: 14, padding: "2px 2px", lineHeight: 1,
    transition: "opacity 0.15s ease",
  },
  input: {
    flex: 1, background: "transparent", border: "none", outline: "none",
    color: "#e5e5e5", fontFamily: "'JetBrains Mono', monospace", fontSize: 12,
    letterSpacing: 0.3, minWidth: 0,
  },
  sendBtn: {
    flexShrink: 0, padding: "4px 12px", borderRadius: 14,
    border: "1px solid rgba(var(--accent-rgb), 0.3)", background: "rgba(var(--accent-rgb), 0.08)",
    color: "var(--accent)", fontFamily: "'JetBrains Mono', monospace", fontSize: 9,
    letterSpacing: 2, fontWeight: 700, transition: "opacity 0.15s ease",
  },
};
