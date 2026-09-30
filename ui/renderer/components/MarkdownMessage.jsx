/**
 * F.R.I.D.A.Y. — components/MarkdownMessage.jsx
 * Renders a chat message's text as markdown (bold, numbered/bulleted
 * pointers, inline code, links) styled to match the existing amber/cyan
 * HUD theme, instead of showing raw '**...**' / '- ' syntax as plain text.
 */

import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const ACCENT = { user: "#06b6d4", friday: "var(--accent)" };

export default function MarkdownMessage({ text, role }) {
  const accent = ACCENT[role] || ACCENT.friday;

  const components = {
    p: ({ node, ...props }) => <p style={styles.p} {...props} />,
    strong: ({ node, ...props }) => <strong style={{ ...styles.strong, color: accent }} {...props} />,
    em: ({ node, ...props }) => <em style={styles.em} {...props} />,
    ul: ({ node, ...props }) => <ul style={styles.list} {...props} />,
    ol: ({ node, ...props }) => <ol style={styles.list} {...props} />,
    li: ({ node, children, ...props }) => (
      <li style={styles.listItem} {...props}>
        <span style={{ ...styles.marker, color: accent }}>▸</span>
        <span style={styles.listItemText}>{children}</span>
      </li>
    ),
    a: ({ node, ...props }) => (
      <a style={{ ...styles.link, color: ACCENT.user }} target="_blank" rel="noreferrer" {...props} />
    ),
    code: ({ node, className, children, ...props }) => {
      const isBlock = /language-/.test(className || "") || String(children).includes("\n");
      return isBlock
        ? <code style={styles.codeBlockInner} className={className} {...props}>{children}</code>
        : <code style={{ ...styles.codeInline, borderColor: `color-mix(in srgb, ${accent} 20%, transparent)` }} {...props}>{children}</code>;
    },
    pre: ({ node, ...props }) => (
      <pre style={{ ...styles.pre, borderColor: `color-mix(in srgb, ${accent} 15%, transparent)` }} {...props} />
    ),
    blockquote: ({ node, ...props }) => (
      <blockquote style={{ ...styles.blockquote, borderLeftColor: accent }} {...props} />
    ),
  };

  return (
    <div style={styles.wrap}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

const styles = {
  wrap: {
    fontSize: 12,
    color: "#d4d4d4",
    lineHeight: 1.5,
    fontFamily: "Segoe UI, system-ui, sans-serif",
  },
  p: {
    margin: "0 0 6px 0",
  },
  strong: {
    fontWeight: 700,
  },
  em: {
    fontStyle: "italic",
    color: "#e5e5e5",
  },
  list: {
    listStyle: "none",
    margin: "0 0 6px 0",
    padding: 0,
    display: "flex",
    flexDirection: "column",
    gap: 3,
  },
  listItem: {
    display: "flex",
    alignItems: "flex-start",
    gap: 6,
  },
  marker: {
    flexShrink: 0,
    fontSize: 10,
    lineHeight: "18px",
  },
  listItemText: {
    flex: 1,
  },
  link: {
    textDecoration: "underline",
    textUnderlineOffset: 2,
  },
  codeInline: {
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 11,
    background: "rgba(255,255,255,0.06)",
    border: "1px solid",
    borderRadius: 4,
    padding: "1px 5px",
  },
  pre: {
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 11,
    background: "rgba(0,0,0,0.35)",
    border: "1px solid",
    borderRadius: 6,
    padding: "8px 10px",
    overflowX: "auto",
    margin: "4px 0 8px 0",
  },
  codeBlockInner: {
    fontFamily: "'JetBrains Mono', monospace",
    background: "none",
    padding: 0,
  },
  blockquote: {
    borderLeft: "2px solid",
    margin: "4px 0 8px 0",
    padding: "2px 0 2px 10px",
    color: "#a3a3a3",
  },
};
