/*
 * mint_flipbook public viewer (task #94304, Phase 3 / Phase 2).
 * Renders the merged flipbook PDF into a StPageFlip page-flip viewer using
 * self-hosted PDF.js. No external/CDN requests: pdf.js, its worker, and
 * StPageFlip are all served from /mint_flipbook/static/ (AC05).
 *
 * Globals (vanilla, no bundler): `pdfjsLib` (pdf.min.js) and `St` (page-flip
 * browser build). Loaded by <script> tags in flipbook_viewer_templates.xml
 * before this file.
 */
(function () {
    "use strict";

    var mount = document.getElementById("flipbook");
    if (!mount) {
        return;
    }

    function fail(msg) {
        mount.innerHTML = '<div class="fb-msg">' + msg + "</div>";
    }

    if (typeof window.pdfjsLib === "undefined" || typeof window.St === "undefined") {
        fail("Viewer failed to load.");
        return;
    }

    var pdfUrl = mount.getAttribute("data-pdf-url");
    var workerSrc = mount.getAttribute("data-worker-src");
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;

    var RENDER_SCALE = 1.5;

    window.pdfjsLib
        .getDocument(pdfUrl)
        .promise.then(function (pdf) {
            var pageEls = [];
            var chain = Promise.resolve();

            for (var i = 1; i <= pdf.numPages; i++) {
                (function (pageNum) {
                    chain = chain.then(function () {
                        return pdf.getPage(pageNum).then(function (page) {
                            var viewport = page.getViewport({ scale: RENDER_SCALE });
                            var canvas = document.createElement("canvas");
                            canvas.width = Math.floor(viewport.width);
                            canvas.height = Math.floor(viewport.height);

                            var pageDiv = document.createElement("div");
                            pageDiv.className = "fb-page";
                            pageDiv.appendChild(canvas);
                            mount.appendChild(pageDiv);
                            pageEls.push(pageDiv);

                            return page.render({
                                canvasContext: canvas.getContext("2d"),
                                viewport: viewport,
                            }).promise;
                        });
                    });
                })(i);
            }

            return chain.then(function () {
                if (!pageEls.length) {
                    fail("This flipbook has no pages.");
                    return;
                }
                var firstCanvas = pageEls[0].querySelector("canvas");
                var w = firstCanvas ? firstCanvas.width : 600;
                var h = firstCanvas ? firstCanvas.height : 800;

                var flip = new window.St.PageFlip(mount, {
                    width: w,
                    height: h,
                    size: "stretch",
                    minWidth: 300,
                    maxWidth: 2400,
                    minHeight: 400,
                    maxHeight: 3200,
                    showCover: false,
                    maxShadowOpacity: 0.5,
                    mobileScrollSupport: true,
                });
                flip.loadFromHTML(pageEls);
            });
        })
        .catch(function (err) {
            fail("Unable to load this flipbook.");
            if (window.console) {
                window.console.error("mint_flipbook viewer:", err);
            }
        });
})();
