/**
 * Daisy+ Chat Embed Widget
 * Standalone chat widget for embedding on external sites.
 * Loaded by /daisy/chat/widget.js bootstrap script.
 *
 * Usage: window.DaisyChat.init({ serverUrl, channelUuid })
 */
(function () {
    "use strict";

    var CONTAINER_ID = "daisy-chat-container";

    function DaisyChat() {
        this.config = {};
        this.channelConfig = null;
        this.conversationHistory = [];
        this.isOpen = false;
        this.container = null;
    }

    DaisyChat.prototype.init = function (config) {
        this.config = config || {};
        this.config.serverUrl = (this.config.serverUrl || "").replace(/\/$/, "");
        this._buildWidget();
        this._fetchConfig();
    };

    DaisyChat.prototype._apiCall = function (endpoint, data) {
        return fetch(this.config.serverUrl + endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params: data || {},
                id: Date.now(),
            }),
        })
            .then(function (r) { return r.json(); })
            .then(function (r) { return r.result || r; });
    };

    DaisyChat.prototype._fetchConfig = function () {
        var self = this;
        if (!this.config.channelUuid) return;

        this._apiCall("/daisy/chat/init", {
            channel_uuid: this.config.channelUuid,
        }).then(function (res) {
            if (res && res.success) {
                self.channelConfig = res.config;
                self._applyConfig();
                self._setupAutoGreet();
            }
        }).catch(function (err) {
            console.error("Daisy Chat: init failed", err);
        });
    };

    DaisyChat.prototype._applyConfig = function () {
        var cfg = this.channelConfig;
        if (!cfg) return;
        var root = this.container;
        if (cfg.primary_color) {
            root.style.setProperty("--daisy-embed-primary", cfg.primary_color);
        }
        var header = root.querySelector(".daisy-embed-header-text");
        if (header && cfg.header_text) {
            header.textContent = cfg.header_text;
        }
        var input = root.querySelector(".daisy-embed-input");
        if (input && cfg.placeholder) {
            input.placeholder = cfg.placeholder;
        }
        if (cfg.widget_position === "bottom_left") {
            root.style.right = "auto";
            root.style.left = "20px";
            var btn = document.getElementById("daisy-chat-toggle");
            if (btn) {
                btn.style.right = "auto";
                btn.style.left = "20px";
            }
        }
    };

    DaisyChat.prototype._setupAutoGreet = function () {
        var self = this;
        var cfg = this.channelConfig;
        if (!cfg || !cfg.auto_greet || !cfg.auto_greet.enabled) return;

        var delay = (cfg.auto_greet.delay || 3) * 1000;
        setTimeout(function () {
            if (!self.isOpen && cfg.auto_greet.message) {
                self._showAutoGreet(cfg.auto_greet.message);
            }
        }, delay);
    };

    DaisyChat.prototype._showAutoGreet = function (message) {
        var bubble = document.createElement("div");
        bubble.className = "daisy-embed-greet";
        bubble.innerHTML =
            '<button class="daisy-embed-greet-close">&times;</button>' +
            '<div class="daisy-embed-greet-text">' + this._escapeHtml(message) + "</div>";

        var self = this;
        bubble.querySelector(".daisy-embed-greet-close").addEventListener("click", function () {
            bubble.remove();
        });
        bubble.querySelector(".daisy-embed-greet-text").addEventListener("click", function () {
            bubble.remove();
            self._toggle();
        });

        document.body.appendChild(bubble);
    };

    DaisyChat.prototype._buildWidget = function () {
        // Floating toggle button
        var toggle = document.createElement("button");
        toggle.id = "daisy-chat-toggle";
        toggle.className = "daisy-embed-toggle";
        toggle.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
        toggle.addEventListener("click", this._toggle.bind(this));
        document.body.appendChild(toggle);

        // Chat container
        var container = document.createElement("div");
        container.id = CONTAINER_ID;
        container.className = "daisy-embed-container daisy-embed-hidden";
        container.innerHTML =
            '<div class="daisy-embed-header">' +
            '  <span class="daisy-embed-header-text">Chat</span>' +
            '  <button class="daisy-embed-close">&times;</button>' +
            "</div>" +
            '<div class="daisy-embed-messages"></div>' +
            '<div class="daisy-embed-composer">' +
            '  <input class="daisy-embed-input" type="text" placeholder="Type your message..." />' +
            '  <button class="daisy-embed-send">&#9654;</button>' +
            "</div>";

        container.querySelector(".daisy-embed-close").addEventListener("click", this._toggle.bind(this));

        var self = this;
        var input = container.querySelector(".daisy-embed-input");
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter" && input.value.trim()) {
                self._sendMessage(input.value.trim());
                input.value = "";
            }
        });
        container.querySelector(".daisy-embed-send").addEventListener("click", function () {
            if (input.value.trim()) {
                self._sendMessage(input.value.trim());
                input.value = "";
            }
        });

        document.body.appendChild(container);
        this.container = container;
    };

    DaisyChat.prototype._toggle = function () {
        this.isOpen = !this.isOpen;
        this.container.classList.toggle("daisy-embed-hidden", !this.isOpen);
        if (this.isOpen) {
            this.container.querySelector(".daisy-embed-input").focus();
        }
    };

    DaisyChat.prototype._appendMessage = function (text, role) {
        var messages = this.container.querySelector(".daisy-embed-messages");
        var msg = document.createElement("div");
        msg.className = "daisy-embed-msg daisy-embed-msg-" + role;
        msg.textContent = text;
        messages.appendChild(msg);
        messages.scrollTop = messages.scrollHeight;
    };

    DaisyChat.prototype._showTyping = function () {
        var messages = this.container.querySelector(".daisy-embed-messages");
        var indicator = document.createElement("div");
        indicator.className = "daisy-embed-typing";
        indicator.id = "daisy-typing";
        indicator.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
        messages.appendChild(indicator);
        messages.scrollTop = messages.scrollHeight;
    };

    DaisyChat.prototype._hideTyping = function () {
        var el = document.getElementById("daisy-typing");
        if (el) el.remove();
    };

    DaisyChat.prototype._sendMessage = function (text) {
        var self = this;
        this._appendMessage(text, "user");
        this.conversationHistory.push({ role: "user", content: text });

        this._showTyping();

        this._apiCall("/daisy/chat/message", {
            channel_id: this.channelConfig ? this.channelConfig.channel_id : null,
            message: text,
            history: this.conversationHistory,
        })
            .then(function (res) {
                self._hideTyping();
                if (res && res.success && res.response) {
                    self._appendMessage(res.response, "assistant");
                    self.conversationHistory.push({ role: "assistant", content: res.response });

                    if (res.suggested_actions && res.suggested_actions.length) {
                        self._showActions(res.suggested_actions);
                    }
                    if (res.should_handoff) {
                        self._appendMessage("Connecting you with a team member...", "system");
                    }
                } else if (res && !res.success) {
                    self._appendMessage("Sorry, something went wrong. Please try again.", "system");
                }
            })
            .catch(function () {
                self._hideTyping();
                self._appendMessage("Unable to connect. Please try again later.", "system");
            });
    };

    DaisyChat.prototype._showActions = function (actions) {
        var self = this;
        var messages = this.container.querySelector(".daisy-embed-messages");
        var wrapper = document.createElement("div");
        wrapper.className = "daisy-embed-actions";
        actions.forEach(function (action) {
            var label = typeof action === "string" ? action : action.label || action;
            var btn = document.createElement("button");
            btn.className = "daisy-embed-action-btn";
            btn.textContent = label;
            btn.addEventListener("click", function () {
                wrapper.remove();
                self._sendMessage(label);
            });
            wrapper.appendChild(btn);
        });
        messages.appendChild(wrapper);
        messages.scrollTop = messages.scrollHeight;
    };

    DaisyChat.prototype._escapeHtml = function (str) {
        var div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    };

    // Expose globally (the bootstrap script calls window.DaisyChat.init())
    window.DaisyChat = new DaisyChat();
})();
