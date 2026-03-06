/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Go2rtcFeedViewer — WebRTC viewer that connects to Go2RTC via WebSocket.
 *
 * Flow:
 * 1. Open WebSocket to Go2RTC ws_url
 * 2. Create RTCPeerConnection with recvonly transceivers
 * 3. Exchange SDP offer/answer + ICE candidates
 * 4. Render received MediaStream in <video>
 */
export class Go2rtcFeedViewer extends Component {
    static template = "daisydo_agents.Go2rtcFeedViewer";
    static props = {
        feedId: { type: Number },
        feedName: { type: String },
        wsUrl: { type: String },
        streamName: { type: String, optional: true },
        onClose: { type: Function, optional: true },
    };

    setup() {
        this.videoRef = useRef("video");
        this.state = useState({
            status: "connecting", // connecting | live | error
            errorMessage: "",
        });

        this.pc = null;
        this.ws = null;

        onMounted(() => this._connect());
        onWillUnmount(() => this._disconnect());
    }

    // ------------------------------------------------------------------
    // WebRTC + WebSocket signaling
    // ------------------------------------------------------------------

    async _connect() {
        try {
            this.pc = new RTCPeerConnection({
                iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
            });

            // We only receive video/audio
            this.pc.addTransceiver("video", { direction: "recvonly" });
            this.pc.addTransceiver("audio", { direction: "recvonly" });

            this.pc.ontrack = (event) => {
                const video = this.videoRef.el;
                if (video && event.streams[0]) {
                    video.srcObject = event.streams[0];
                    this.state.status = "live";
                }
            };

            this.pc.oniceconnectionstatechange = () => {
                if (this.pc.iceConnectionState === "failed") {
                    this.state.status = "error";
                    this.state.errorMessage = "Connection lost";
                }
            };

            // Open WebSocket to Go2RTC
            this.ws = new WebSocket(this.props.wsUrl);

            this.ws.onopen = async () => {
                const offer = await this.pc.createOffer();
                await this.pc.setLocalDescription(offer);
                this.ws.send(JSON.stringify({
                    type: "webrtc/offer",
                    value: this.pc.localDescription.sdp,
                }));
            };

            this.ws.onmessage = async (event) => {
                const msg = JSON.parse(event.data);

                if (msg.type === "webrtc/answer") {
                    await this.pc.setRemoteDescription({
                        type: "answer",
                        sdp: msg.value,
                    });
                } else if (msg.type === "webrtc/candidate") {
                    if (msg.value) {
                        // Go2RTC sends candidate as a string
                        const parts = msg.value.split(" ");
                        await this.pc.addIceCandidate({
                            candidate: msg.value,
                            sdpMid: msg.sdpMid || "0",
                            sdpMLineIndex: msg.sdpMLineIndex || 0,
                        });
                    }
                }
            };

            this.ws.onerror = () => {
                this.state.status = "error";
                this.state.errorMessage = "WebSocket connection failed";
            };

            this.ws.onclose = () => {
                if (this.state.status === "connecting") {
                    this.state.status = "error";
                    this.state.errorMessage = "Connection closed";
                }
            };

            // Send ICE candidates to Go2RTC
            this.pc.onicecandidate = (event) => {
                if (event.candidate && this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({
                        type: "webrtc/candidate",
                        value: event.candidate.candidate,
                    }));
                }
            };
        } catch (err) {
            this.state.status = "error";
            this.state.errorMessage = err.message || "Failed to connect";
        }
    }

    _disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        if (this.pc) {
            this.pc.close();
            this.pc = null;
        }
    }

    _onClose() {
        this._disconnect();
        if (this.props.onClose) {
            this.props.onClose(this.props.feedId);
        }
    }
}

/**
 * Client action: opens the feed viewer as a standalone page
 * (used by the action_open_viewer button on the feed form).
 */
export class DaisyFeedViewerAction extends Component {
    static template = "daisydo_agents.DaisyFeedViewerAction";
    static components = { Go2rtcFeedViewer };
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.state = useState({
            feedId: this.props.action?.params?.feed_id || 0,
            feedName: this.props.action?.params?.feed_name || "Camera",
            wsUrl: this.props.action?.params?.ws_url || "",
        });
    }

    onClose() {
        this.action.doAction({ type: "ir.actions.act_window_close" });
    }
}

registry.category("actions").add("daisy_feed_viewer", DaisyFeedViewerAction);
