# Browser Libraries

These pinned, locally served browser distributions require no npm installation or CDN access at runtime.

| Library | Version | Source | License File |
| --- | --- | --- | --- |
| Marked | 17.0.5 | Existing bundled runtime package, markedjs/marked | MARKED-LICENSE.md |
| DOMPurify | 3.4.16 | cure53/DOMPurify release distribution | DOMPURIFY-LICENSE |
| Highlight.js | 11.12.0 | highlightjs/cdn-release browser distribution | HIGHLIGHT-LICENSE |
| Lucide | 1.8.0 | Existing bundled runtime package, lucide-icons/lucide | LUCIDE-LICENSE |

Untrusted Markdown is sanitized before insertion. Remote images are not loaded; only authenticated conversation artifacts are rendered. Highlighting runs after sanitization against text content.
