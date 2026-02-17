/** @odoo-module **/

/**
 * Daisy+ Chat Widget
 * Extends Odoo's livechat with AI-powered responses and custom styling
 */

import { patch } from "@web/core/utils/patch";
import { Component, useState, onMounted } from "@odoo/owl";

// Widget customization applied on load
document.addEventListener("DOMContentLoaded", function() {
    // Add Daisy branding class to chat elements
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            mutation.addedNodes.forEach(function(node) {
                if (node.classList && node.classList.contains("o_livechat_button")) {
                    node.classList.add("daisy-chat-button");
                }
                if (node.classList && node.classList.contains("o_thread_window")) {
                    node.classList.add("daisy-chat-window");
                }
            });
        });
    });

    observer.observe(document.body, { childList: true, subtree: true });
});

/**
 * Daisy Chat Manager - handles AI responses and widget behavior
 */
export class DaisyChatManager {
    constructor(config = {}) {
        this.config = {
            serverUrl: config.serverUrl || window.location.origin,
            channelUuid: config.channelUuid || null,
            autoGreet: config.autoGreet !== false,
            autoGreetDelay: config.autoGreetDelay || 3000,
            ...config
        };
        this.conversationHistory = [];
        this.isInitialized = false;
    }

    async init() {
        if (this.isInitialized) return;

        try {
            const response = await this.apiCall("/daisy/chat/init", {
                channel_uuid: this.config.channelUuid
            });

            if (response.success) {
                this.channelConfig = response.config;
                this.applyWidgetConfig();
                this.setupAutoGreet();
                this.isInitialized = true;
            }
        } catch (error) {
            console.error("Daisy Chat init failed:", error);
        }
    }

    applyWidgetConfig() {
        const config = this.channelConfig;
        if (!config) return;

        // Apply custom colors via CSS variables
        document.documentElement.style.setProperty("--daisy-primary", config.primary_color);
        document.documentElement.style.setProperty("--daisy-header-text", config.header_text);
    }

    setupAutoGreet() {
        if (!this.channelConfig?.auto_greet?.enabled) return;

        const delay = this.channelConfig.auto_greet.delay * 1000;
        setTimeout(() => {
            this.showAutoGreet(this.channelConfig.auto_greet.message);
        }, delay);
    }

    showAutoGreet(message) {
        // Trigger auto-greet notification
        const event = new CustomEvent("daisy-auto-greet", {
            detail: { message }
        });
        document.dispatchEvent(event);
    }

    async sendMessage(message) {
        this.conversationHistory.push({
            role: "user",
            content: message,
            timestamp: new Date().toISOString()
        });

        try {
            const response = await this.apiCall("/daisy/chat/message", {
                channel_id: this.channelConfig?.channel_id,
                message: message,
                history: this.conversationHistory
            });

            if (response.success && response.response) {
                this.conversationHistory.push({
                    role: "assistant",
                    content: response.response,
                    timestamp: new Date().toISOString(),
                    ai_generated: true
                });

                return {
                    text: response.response,
                    suggestedActions: response.suggested_actions || [],
                    shouldHandoff: response.should_handoff
                };
            }
        } catch (error) {
            console.error("Daisy Chat message failed:", error);
        }

        return null;
    }

    async requestHandoff() {
        return this.apiCall("/daisy/chat/handoff", {
            channel_id: this.channelConfig?.channel_id,
            session_id: this.sessionId,
            conversation_history: this.conversationHistory
        });
    }

    async submitFeedback(rating, comment = null) {
        return this.apiCall("/daisy/chat/feedback", {
            session_id: this.sessionId,
            rating,
            comment
        });
    }

    async apiCall(endpoint, data) {
        const response = await fetch(this.config.serverUrl + endpoint, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params: data,
                id: Date.now()
            })
        });
        const result = await response.json();
        return result.result || result;
    }
}

// Export for global access
window.DaisyChat = DaisyChatManager;
