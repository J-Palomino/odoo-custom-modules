/** @odoo-module **/

import { Component, useState, onWillUnmount } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Go2rtcFeedViewer } from "./feed_viewer";

/**
 * DaisyFeedCallButton -- a component added to the Discuss call action bar
 * that shows a "Camera Feeds" dropdown. When a feed is selected, it opens
 * a Go2rtcFeedViewer panel alongside the call.
 */
export class DaisyFeedCallButton extends Component {
    static template = "daisydo_agents.DaisyFeedCallButton";
    static components = { Go2rtcFeedViewer };
    static props = {
        channelId: { type: Number },
    };

    setup() {
        this.rpc = useService("rpc");
        this.notification = useService("notification");

        this.state = useState({
            feeds: [],
            openFeeds: [], // feeds currently being viewed
            loading: false,
            dropdownOpen: false,
        });

        this._pollInterval = null;
    }

    // ------------------------------------------------------------------
    // Feed discovery
    // ------------------------------------------------------------------

    async toggleDropdown() {
        if (this.state.dropdownOpen) {
            this.state.dropdownOpen = false;
            return;
        }

        this.state.loading = true;
        this.state.dropdownOpen = true;

        try {
            const result = await this.rpc("/daisy/feeds/by-channel/" + this.props.channelId, {});
            this.state.feeds = result.feeds || [];
        } catch (err) {
            this.notification.add("Could not load camera feeds", { type: "warning" });
            this.state.feeds = [];
        } finally {
            this.state.loading = false;
        }
    }

    // ------------------------------------------------------------------
    // Open / close feed viewers
    // ------------------------------------------------------------------

    openFeed(feed) {
        // Don't open the same feed twice
        if (this.state.openFeeds.some((f) => f.feed_id === feed.feed_id)) {
            return;
        }
        this.state.openFeeds.push({ ...feed });
        this.state.dropdownOpen = false;
    }

    closeFeed(feedId) {
        const idx = this.state.openFeeds.findIndex((f) => f.feed_id === feedId);
        if (idx >= 0) {
            this.state.openFeeds.splice(idx, 1);
        }
    }

    get hasFeeds() {
        return this.state.feeds.length > 0;
    }

    get openFeedCount() {
        return this.state.openFeeds.length;
    }
}

/**
 * Registry-based approach: register a systray-like component that injects
 * into Discuss. This avoids patching internal Discuss OWL components directly,
 * which is fragile across Odoo versions.
 *
 * The component watches for active Discuss calls and shows the camera button
 * when a call is in progress.
 */
export class DaisyFeedCallManager extends Component {
    static template = "daisydo_agents.DaisyFeedCallManager";
    static components = { DaisyFeedCallButton };
    static props = {};

    setup() {
        this.state = useState({
            activeChannelId: null,
        });

        // Listen for Discuss call state changes via bus
        this.bus = this.env.bus;
        this._onCallState = this._onCallState.bind(this);
        this.bus.addEventListener("discuss.call.state", this._onCallState);

        onWillUnmount(() => {
            this.bus.removeEventListener("discuss.call.state", this._onCallState);
        });
    }

    _onCallState(ev) {
        const { channelId, active } = ev.detail || {};
        this.state.activeChannelId = active ? channelId : null;
    }

    get isInCall() {
        return !!this.state.activeChannelId;
    }
}

// Register as a systray item so it's always available in the web client
registry.category("systray").add("daisydo_agents.FeedCallManager", {
    Component: DaisyFeedCallManager,
    isDisplayed: () => true,
}, { sequence: 5 });
