/**
 * MintDeals Embed Widget
 * ======================
 * Drop a single <script> tag on any WordPress (or other) page to embed
 * banners, blog feeds, contact forms, newsletter signups, or info pages
 * powered by the Odoo backend at letsgomint.us.
 *
 * Usage:
 *   <script src="https://letsgomint.us/embed/mint-widget.js"
 *           data-type="banners"
 *           data-slot="hero"
 *           data-base="https://letsgomint.us"></script>
 */
(function () {
  'use strict';

  // ── Find our own script tag ──────────────────────────────────
  var scripts = document.querySelectorAll('script[src*="mint-widget"]');
  var me = scripts[scripts.length - 1];
  if (!me) return;

  var TYPE = me.getAttribute('data-type') || 'banners';
  var BASE = (me.getAttribute('data-base') || 'https://letsgomint.us').replace(/\/$/, '');
  var THEME = me.getAttribute('data-theme') || 'light';
  var ACCENT = me.getAttribute('data-accent') || '#00954c';
  var MAX_WIDTH = me.getAttribute('data-max-width') || '100%';
  var EMBED_KEY = me.getAttribute('data-key') || '';

  // ── Create container with Shadow DOM ─────────────────────────
  var host = document.createElement('div');
  host.className = 'mint-embed-host';
  me.parentNode.insertBefore(host, me);

  var shadow = host.attachShadow({ mode: 'open' });

  // ── Shared styles ────────────────────────────────────────────
  var baseCSS = '\n'
    + ':host { display: block; font-family: "Montserrat", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }\n'
    + '.mint-root { max-width: ' + MAX_WIDTH + '; margin: 0 auto; }\n'
    + '.mint-root * { box-sizing: border-box; }\n'
    + '.mint-loading { text-align: center; padding: 2rem; color: #888; }\n'
    + '.mint-error { text-align: center; padding: 1rem; color: #dc2626; font-size: 0.875rem; }\n'
    + (THEME === 'dark'
      ? '.mint-root { color: #e5e7eb; } .mint-root a { color: ' + ACCENT + '; }\n'
      : '.mint-root { color: #1f2937; } .mint-root a { color: ' + ACCENT + '; }\n')
    + '.mint-btn { display: inline-block; padding: 0.625rem 1.5rem; border: none; border-radius: 0.5rem;'
    + ' background: ' + ACCENT + '; color: #fff; font-size: 0.9375rem; font-weight: 600;'
    + ' cursor: pointer; text-decoration: none; transition: opacity .2s; }\n'
    + '.mint-btn:hover { opacity: 0.85; }\n'
    + '.mint-btn:disabled { opacity: 0.5; cursor: not-allowed; }\n'
    + '.mint-input { display: block; width: 100%; padding: 0.625rem 0.75rem; border: 1px solid #d1d5db;'
    + ' border-radius: 0.375rem; font-size: 0.9375rem; font-family: inherit; margin-bottom: 0.75rem; }\n'
    + '.mint-input:focus { outline: none; border-color: ' + ACCENT + '; box-shadow: 0 0 0 2px ' + ACCENT + '33; }\n'
    + '.mint-textarea { resize: vertical; min-height: 5rem; }\n'
    + '.mint-success { padding: 1rem; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 0.5rem;'
    + ' color: #065f46; text-align: center; font-weight: 500; }\n';

  // ── Helpers ──────────────────────────────────────────────────
  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json(); });
  }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'className') e.className = attrs[k];
      else if (k === 'innerHTML') e.innerHTML = attrs[k];
      else if (k.indexOf('on') === 0) e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    });
    if (children) children.forEach(function (c) {
      if (typeof c === 'string') e.appendChild(document.createTextNode(c));
      else if (c) e.appendChild(c);
    });
    return e;
  }

  // ── BANNERS ──────────────────────────────────────────────────
  function renderBanners() {
    var slot = me.getAttribute('data-slot') || '';
    var store = me.getAttribute('data-store') || '';
    var params = [];
    if (slot) params.push('slot=' + encodeURIComponent(slot));
    if (store) params.push('company_id=' + encodeURIComponent(store));
    var url = BASE + '/api/v1/banners' + (params.length ? '?' + params.join('&') : '');

    var css = baseCSS
      + '.mint-banners { display: flex; flex-wrap: wrap; gap: 1rem; }\n'
      + '.mint-banner { flex: 1 1 100%; border-radius: 0.75rem; overflow: hidden;'
      + ' position: relative; text-decoration: none; display: block; }\n'
      + '.mint-banner img { width: 100%; height: auto; display: block; }\n'
      + '.mint-banner-overlay { position: absolute; bottom: 0; left: 0; right: 0;'
      + ' padding: 1.25rem; background: linear-gradient(transparent, rgba(0,0,0,.7)); color: #fff; }\n'
      + '.mint-banner-title { font-size: 1.25rem; font-weight: 700; margin: 0 0 0.25rem; }\n'
      + '.mint-banner-sub { font-size: 0.875rem; margin: 0; opacity: 0.9; }\n'
      + '@media(min-width:640px){ .mint-banners { gap: 1.25rem; }'
      + ' .mint-banner.mint-size-small { flex: 0 0 calc(50% - 0.75rem); } }\n';

    var style = el('style', { innerHTML: css });
    var root = el('div', { className: 'mint-root' });
    var loading = el('div', { className: 'mint-loading' }, ['Loading banners...']);
    root.appendChild(loading);
    shadow.appendChild(style);
    shadow.appendChild(root);

    fetchJSON(url).then(function (banners) {
      root.removeChild(loading);
      if (!banners || !banners.length) {
        root.appendChild(el('div', { className: 'mint-error' }, ['No banners available.']));
        return;
      }
      var wrap = el('div', { className: 'mint-banners' });
      banners.forEach(function (b) {
        var sizeClass = ' mint-size-' + (b.size || 'medium');
        var banner = el(b.link_url ? 'a' : 'div', {
          className: 'mint-banner' + sizeClass,
          href: b.link_url || undefined,
          target: b.link_url ? '_blank' : undefined,
          rel: b.link_url ? 'noopener' : undefined,
        });
        if (b.background_color) banner.style.backgroundColor = b.background_color;
        if (b.image) banner.appendChild(el('img', { src: b.image, alt: b.title || b.name, loading: 'lazy' }));
        if (b.title || b.subtitle) {
          var overlay = el('div', { className: 'mint-banner-overlay' });
          if (b.title) overlay.appendChild(el('p', { className: 'mint-banner-title' }, [b.title]));
          if (b.subtitle) overlay.appendChild(el('p', { className: 'mint-banner-sub' }, [b.subtitle]));
          banner.appendChild(overlay);
        }
        wrap.appendChild(banner);
      });
      root.appendChild(wrap);
    }).catch(function (err) {
      root.innerHTML = '';
      root.appendChild(el('div', { className: 'mint-error' }, ['Failed to load banners.']));
    });
  }

  // ── BLOG ─────────────────────────────────────────────────────
  function renderBlog() {
    var limit = me.getAttribute('data-limit') || '3';
    var featured = me.getAttribute('data-featured') || '';
    var params = ['limit=' + limit];
    if (featured === 'true') params.push('featured=true');
    var url = BASE + '/api/v1/blog?' + params.join('&');

    var css = baseCSS
      + '.mint-blog-grid { display: grid; grid-template-columns: 1fr; gap: 1.25rem; }\n'
      + '@media(min-width:640px){ .mint-blog-grid { grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); } }\n'
      + '.mint-blog-card { border-radius: 0.75rem; overflow: hidden; border: 1px solid #e5e7eb;'
      + ' transition: box-shadow .2s; }\n'
      + '.mint-blog-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,.1); }\n'
      + '.mint-blog-card img { width: 100%; height: 180px; object-fit: cover; display: block; }\n'
      + '.mint-blog-body { padding: 1rem; }\n'
      + '.mint-blog-title { font-size: 1.0625rem; font-weight: 700; margin: 0 0 0.375rem;'
      + ' color: inherit; text-decoration: none; }\n'
      + '.mint-blog-excerpt { font-size: 0.875rem; color: #6b7280; margin: 0; line-height: 1.5; }\n'
      + '.mint-blog-date { font-size: 0.75rem; color: #9ca3af; margin-top: 0.5rem; }\n';

    var style = el('style', { innerHTML: css });
    var root = el('div', { className: 'mint-root' });
    var loading = el('div', { className: 'mint-loading' }, ['Loading articles...']);
    root.appendChild(loading);
    shadow.appendChild(style);
    shadow.appendChild(root);

    fetchJSON(url).then(function (data) {
      root.removeChild(loading);
      var posts = data.items || data || [];
      if (!posts.length) {
        root.appendChild(el('div', { className: 'mint-error' }, ['No articles available.']));
        return;
      }
      var grid = el('div', { className: 'mint-blog-grid' });
      posts.forEach(function (p) {
        var card = el('div', { className: 'mint-blog-card' });
        var imgUrl = p.image_url ? (p.image_url.startsWith('http') ? p.image_url : BASE + p.image_url) : null;
        if (imgUrl) card.appendChild(el('img', { src: imgUrl, alt: p.title || '', loading: 'lazy' }));
        var body = el('div', { className: 'mint-blog-body' });
        body.appendChild(el('div', { className: 'mint-blog-title' }, [p.title || '']));
        if (p.excerpt) body.appendChild(el('p', { className: 'mint-blog-excerpt' }, [p.excerpt]));
        if (p.published_at) {
          var d = new Date(p.published_at);
          body.appendChild(el('div', { className: 'mint-blog-date' }, [d.toLocaleDateString()]));
        }
        card.appendChild(body);
        grid.appendChild(card);
      });
      root.appendChild(grid);
    }).catch(function () {
      root.innerHTML = '';
      root.appendChild(el('div', { className: 'mint-error' }, ['Failed to load articles.']));
    });
  }

  // ── CONTACT FORM ─────────────────────────────────────────────
  function renderContactForm() {
    var css = baseCSS
      + '.mint-form { max-width: 480px; margin: 0 auto; }\n'
      + '.mint-form-title { font-size: 1.25rem; font-weight: 700; margin: 0 0 0.25rem; }\n'
      + '.mint-form-desc { font-size: 0.875rem; color: #6b7280; margin: 0 0 1.25rem; }\n'
      + '.mint-form-row { display: flex; gap: 0.75rem; }\n'
      + '.mint-form-row .mint-input { flex: 1; }\n'
      + '@media(max-width:480px){ .mint-form-row { flex-direction: column; gap: 0; } }\n'
      + '.mint-form-error { color: #dc2626; font-size: 0.8125rem; margin-bottom: 0.5rem; }\n';

    var style = el('style', { innerHTML: css });
    var root = el('div', { className: 'mint-root' });
    var form = el('div', { className: 'mint-form' });
    form.appendChild(el('h3', { className: 'mint-form-title' }, ['Get in Touch']));
    form.appendChild(el('p', { className: 'mint-form-desc' }, ['We\'d love to hear from you.']));

    var errorEl = el('div', { className: 'mint-form-error' });
    errorEl.style.display = 'none';
    form.appendChild(errorEl);

    var nameInput = el('input', { className: 'mint-input', type: 'text', placeholder: 'Your name', required: '' });
    var emailInput = el('input', { className: 'mint-input', type: 'email', placeholder: 'Email address', required: '' });
    var phoneInput = el('input', { className: 'mint-input', type: 'tel', placeholder: 'Phone (optional)' });
    var messageInput = el('textarea', { className: 'mint-input mint-textarea', placeholder: 'Your message', required: '' });
    var submitBtn = el('button', { className: 'mint-btn', type: 'button' }, ['Send Message']);

    var row1 = el('div', { className: 'mint-form-row' }, [nameInput, emailInput]);
    form.appendChild(row1);
    form.appendChild(phoneInput);
    form.appendChild(messageInput);
    form.appendChild(submitBtn);

    submitBtn.addEventListener('click', function () {
      errorEl.style.display = 'none';
      var name = nameInput.value.trim();
      var email = emailInput.value.trim();
      var message = messageInput.value.trim();
      if (!name || !email || !message) {
        errorEl.textContent = 'Please fill in name, email, and message.';
        errorEl.style.display = 'block';
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Sending...';
      postJSON(BASE + '/api/v1/embed/contact', {
        name: name,
        email: email,
        phone: phoneInput.value.trim(),
        message: message,
        source_url: window.location.href,
        embed_key: EMBED_KEY,
      }).then(function (res) {
        if (res.error) {
          errorEl.textContent = res.error;
          errorEl.style.display = 'block';
          submitBtn.disabled = false;
          submitBtn.textContent = 'Send Message';
        } else {
          form.innerHTML = '';
          form.appendChild(el('div', { className: 'mint-success' }, [res.message || 'Thank you!']));
        }
      }).catch(function () {
        errorEl.textContent = 'Something went wrong. Please try again.';
        errorEl.style.display = 'block';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send Message';
      });
    });

    root.appendChild(form);
    shadow.appendChild(style);
    shadow.appendChild(root);
  }

  // ── NEWSLETTER FORM ──────────────────────────────────────────
  function renderNewsletterForm() {
    var css = baseCSS
      + '.mint-newsletter { max-width: 480px; margin: 0 auto; text-align: center; }\n'
      + '.mint-newsletter-title { font-size: 1.125rem; font-weight: 700; margin: 0 0 0.25rem; }\n'
      + '.mint-newsletter-desc { font-size: 0.875rem; color: #6b7280; margin: 0 0 1rem; }\n'
      + '.mint-newsletter-row { display: flex; gap: 0.5rem; }\n'
      + '.mint-newsletter-row .mint-input { flex: 1; margin-bottom: 0; }\n'
      + '@media(max-width:480px){ .mint-newsletter-row { flex-direction: column; }'
      + ' .mint-newsletter-row .mint-btn { width: 100%; } }\n'
      + '.mint-form-error { color: #dc2626; font-size: 0.8125rem; margin-bottom: 0.5rem; }\n';

    var style = el('style', { innerHTML: css });
    var root = el('div', { className: 'mint-root' });
    var wrap = el('div', { className: 'mint-newsletter' });
    wrap.appendChild(el('h3', { className: 'mint-newsletter-title' }, ['Stay in the Loop']));
    wrap.appendChild(el('p', { className: 'mint-newsletter-desc' }, ['Get deals, news, and updates from Mint.']));

    var errorEl = el('div', { className: 'mint-form-error' });
    errorEl.style.display = 'none';
    wrap.appendChild(errorEl);

    var emailInput = el('input', { className: 'mint-input', type: 'email', placeholder: 'Enter your email' });
    var submitBtn = el('button', { className: 'mint-btn', type: 'button' }, ['Subscribe']);

    var row = el('div', { className: 'mint-newsletter-row' }, [emailInput, submitBtn]);
    wrap.appendChild(row);

    submitBtn.addEventListener('click', function () {
      errorEl.style.display = 'none';
      var email = emailInput.value.trim();
      if (!email) {
        errorEl.textContent = 'Please enter your email address.';
        errorEl.style.display = 'block';
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Subscribing...';
      postJSON(BASE + '/api/v1/embed/newsletter', {
        email: email,
        source_url: window.location.href,
        embed_key: EMBED_KEY,
      }).then(function (res) {
        if (res.error) {
          errorEl.textContent = res.error;
          errorEl.style.display = 'block';
          submitBtn.disabled = false;
          submitBtn.textContent = 'Subscribe';
        } else {
          wrap.innerHTML = '';
          wrap.appendChild(el('div', { className: 'mint-success' }, [res.message || 'Subscribed!']));
        }
      }).catch(function () {
        errorEl.textContent = 'Something went wrong. Please try again.';
        errorEl.style.display = 'block';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Subscribe';
      });
    });

    root.appendChild(wrap);
    shadow.appendChild(style);
    shadow.appendChild(root);
  }

  // ── INFO PAGE ────────────────────────────────────────────────
  function renderPage() {
    var slug = me.getAttribute('data-page') || '';
    if (!slug) return;

    var css = baseCSS
      + '.mint-page { max-width: 720px; margin: 0 auto; }\n'
      + '.mint-page-hero { width: 100%; border-radius: 0.75rem; margin-bottom: 1.5rem; }\n'
      + '.mint-page-title { font-size: 1.75rem; font-weight: 800; margin: 0 0 0.5rem; }\n'
      + '.mint-page-summary { font-size: 1.0625rem; color: #6b7280; margin: 0 0 1.5rem; line-height: 1.6; }\n'
      + '.mint-page-content { line-height: 1.7; font-size: 1rem; }\n'
      + '.mint-page-content img { max-width: 100%; height: auto; border-radius: 0.5rem; }\n'
      + '.mint-page-content h2 { font-size: 1.375rem; font-weight: 700; margin: 1.5rem 0 0.75rem; }\n'
      + '.mint-page-content h3 { font-size: 1.125rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }\n'
      + '.mint-page-content p { margin: 0 0 1rem; }\n'
      + '.mint-page-content ul, .mint-page-content ol { padding-left: 1.5rem; margin: 0 0 1rem; }\n'
      + '.mint-page-cta { text-align: center; margin-top: 2rem; }\n';

    var style = el('style', { innerHTML: css });
    var root = el('div', { className: 'mint-root' });
    var loading = el('div', { className: 'mint-loading' }, ['Loading...']);
    root.appendChild(loading);
    shadow.appendChild(style);
    shadow.appendChild(root);

    fetchJSON(BASE + '/api/v1/embed/pages/' + encodeURIComponent(slug)).then(function (page) {
      root.removeChild(loading);
      var wrap = el('div', { className: 'mint-page' });
      if (page.hero_image) {
        var imgSrc = page.hero_image.startsWith('http') ? page.hero_image : BASE + page.hero_image;
        wrap.appendChild(el('img', { className: 'mint-page-hero', src: imgSrc, alt: page.title || '' }));
      }
      if (page.title) wrap.appendChild(el('h1', { className: 'mint-page-title' }, [page.title]));
      if (page.summary) wrap.appendChild(el('p', { className: 'mint-page-summary' }, [page.summary]));
      if (page.content) wrap.appendChild(el('div', { className: 'mint-page-content', innerHTML: page.content }));
      if (page.cta_label && page.cta_url) {
        var ctaWrap = el('div', { className: 'mint-page-cta' });
        ctaWrap.appendChild(el('a', { className: 'mint-btn', href: page.cta_url, target: '_blank', rel: 'noopener' }, [page.cta_label]));
        wrap.appendChild(ctaWrap);
      }
      root.appendChild(wrap);
    }).catch(function () {
      root.innerHTML = '';
      root.appendChild(el('div', { className: 'mint-error' }, ['Failed to load page.']));
    });
  }

  // ── Router ───────────────────────────────────────────────────
  switch (TYPE) {
    case 'banners': renderBanners(); break;
    case 'blog': renderBlog(); break;
    case 'form_contact': renderContactForm(); break;
    case 'form_newsletter': renderNewsletterForm(); break;
    case 'page': renderPage(); break;
    default:
      shadow.appendChild(el('div', { className: 'mint-error' }, ['Unknown widget type: ' + TYPE]));
  }
})();
