# Vendored third-party libraries (mint_flipbook public viewer)

Self-hosted so the public viewer makes zero external/CDN requests (task #94304 AC05).

| Library | Version | License | Upstream |
|---|---|---|---|
| StPageFlip (`stpageflip/page-flip.browser.js`) | 2.0.7 | MIT | https://github.com/Nodlik/StPageFlip |
| PDF.js (`pdfjs/pdf.min.js`, `pdfjs/pdf.worker.min.js`) | 3.11.174 | Apache-2.0 | https://github.com/mozilla/pdf.js |

Both licenses are compatible with this module's LGPL-3. PDF.js carries its Apache-2.0
notice inline at the top of `pdf.min.js`.
