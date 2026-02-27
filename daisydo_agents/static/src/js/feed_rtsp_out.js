/** @odoo-module **/

import { Component, useState, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * DaisyRtspOutPush -- captures a Discuss call's MediaStream and pushes it
 * to Go2RTC via WHIP so the call is available as an RTSP output stream
 * for store displays and recorders.
 *
 * Usage: include in a call toolbar. Pass the feed_id for an rtsp_out feed
 * and the MediaStream from the call.
 */
export class DaisyRtspOutPush extends Component {
    static template = "daisydo_agents.DaisyRtspOutPush";
    static props = {
        feedId: { type: Number },
        feedName: { type: String },
        mediaStream: { type: Object, optional: true },
        onClose: { type: Function, optional: true },
    };

    setup() {
        this.rpc = useService("rpc");
        this.notification = useService("notification");

        this.state = useState({
            status: "idle", // idle | connecting | broadcasting | error
            rtspUrl: "",
            errorMessage: "",
        });

        this.pc = null;

        onWillUnmount(() => this._stop());
    }

    // ------------------------------------------------------------------
    // Push WebRTC stream to Go2RTC
    // ------------------------------------------------------------------

    async startBroadcast() {
        if (!this.props.mediaStream) {
            this.state.status = "error";
            this.state.errorMessage = "No media stream available";
            return;
        }

        this.state.status = "connecting";

        try {
            // Create peer connection and add tracks from the call stream
            this.pc = new RTCPeerConnection({
                iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
            });

            for (const track of this.props.mediaStream.getTracks()) {
                this.pc.addTrack(track, this.props.mediaStream);
            }

            // Create offer
            const offer = await this.pc.createOffer();
            await this.pc.setLocalDescription(offer);

            // Wait for ICE gathering to complete (or timeout after 3s)
            await this._waitForIceGathering(3000);

            // Send to server which proxies to Go2RTC WHIP
            const result = await this.rpc(
                `/daisy/feeds/${this.props.feedId}/push-webrtc`,
                { sdp_offer: this.pc.localDescription.sdp },
            );

            if (result.error) {
                throw new Error(result.error);
            }

            // Apply the SDP answer
            await this.pc.setRemoteDescription({
                type: "answer",
                sdp: result.sdp_answer,
            });

            this.state.status = "broadcasting";
            this.state.rtspUrl = result.rtsp_url || "";

            this.notification.add(
                `Broadcasting to ${this.props.feedName}`,
                { type: "success" },
            );
        } catch (err) {
            this.state.status = "error";
            this.state.errorMessage = err.message || "Broadcast failed";
        }
    }

    _waitForIceGathering(timeout) {
        return new Promise((resolve) => {
            if (this.pc.iceGatheringState === "complete") {
                return resolve();
            }
            const timer = setTimeout(resolve, timeout);
            this.pc.addEventListener("icegatheringstatechange", () => {
                if (this.pc.iceGatheringState === "complete") {
                    clearTimeout(timer);
                    resolve();
                }
            });
        });
    }

    stopBroadcast() {
        this._stop();
        this.state.status = "idle";
        this.state.rtspUrl = "";
    }

    _stop() {
        if (this.pc) {
            this.pc.close();
            this.pc = null;
        }
    }

    _onClose() {
        this._stop();
        if (this.props.onClose) {
            this.props.onClose(this.props.feedId);
        }
    }

    copyRtspUrl() {
        if (this.state.rtspUrl) {
            navigator.clipboard.writeText(this.state.rtspUrl).then(() => {
                this.notification.add("RTSP URL copied!", { type: "info" });
            });
        }
    }
}
