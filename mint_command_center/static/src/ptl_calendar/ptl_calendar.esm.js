/** @odoo-module **/

import { calendarView } from "@web/views/calendar/calendar_view";
import { CalendarRenderer } from "@web/views/calendar/calendar_renderer";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    useEffect,
    useRef,
    useState,
} from "@odoo/owl";

/**
 * Side pane that lists approved PTL deals for the calendar's current date
 * range + market filter. Cards are HTML5-draggable; FullCalendar day cells
 * (selected by .fc-daygrid-day[data-date]) act as drop targets. On drop,
 * we call mint.ptl.day.schedule_deal and reload the calendar.
 */
export class PtlDragboard extends Component {
    static template = "mint_command_center.PtlDragboard";
    static props = { model: Object };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({ deals: [], loading: false, search: "" });
        this.rootRef = useRef("root");

        // Listeners we attach to the calendar's day cells. We track them so
        // we can clean up on unmount (calendar re-renders frequently).
        this._cellListeners = [];
        this._mutationObs = null;

        onWillStart(async () => {
            await this._fetchDeals();
        });

        // Re-fetch when the calendar's date range changes.
        useEffect(
            () => {
                this._fetchDeals();
            },
            () => [this.rangeFrom, this.rangeTo, this.marketId],
        );

        onMounted(() => {
            this._wireCalendarDropTargets();
        });

        onWillUnmount(() => {
            this._clearCellListeners();
            if (this._mutationObs) {
                this._mutationObs.disconnect();
                this._mutationObs = null;
            }
        });
    }

    // ─── Calendar-state accessors ─────────────────────────────────────────

    get rangeFrom() {
        const d = this.props.model.rangeStart;
        return d ? this._isoDate(d) : null;
    }

    get rangeTo() {
        const d = this.props.model.rangeEnd;
        return d ? this._isoDate(d) : null;
    }

    /**
     * Try to read the market filter from the active search domain. The PTL
     * calendar view's search has `market_id` as a groupable field; if the
     * user has set a Market filter we'll find a (market_id, =, id) leaf.
     */
    get marketId() {
        const domain = this.env.searchModel?.domain || [];
        for (const leaf of domain) {
            if (Array.isArray(leaf) && leaf.length === 3 && leaf[0] === "market_id" && leaf[1] === "=") {
                return leaf[2];
            }
        }
        return null;
    }

    get filteredDeals() {
        const q = (this.state.search || "").trim().toLowerCase();
        if (!q) return this.state.deals;
        return this.state.deals.filter(
            (d) =>
                (d.name || "").toLowerCase().includes(q) ||
                (d.brand || "").toLowerCase().includes(q) ||
                (d.category || "").toLowerCase().includes(q),
        );
    }

    // ─── Data fetch ────────────────────────────────────────────────────────

    async _fetchDeals() {
        this.state.loading = true;
        try {
            const deals = await this.orm.call(
                "mint.ptl.deal",
                "search_approved_for_dragboard",
                [],
                {
                    date_from: this.rangeFrom,
                    date_to: this.rangeTo,
                    market_id: this.marketId || false,
                },
            );
            this.state.deals = deals || [];
        } catch (err) {
            this.state.deals = [];
            this.notification.add(
                this.env._t ? this.env._t("Could not load approved deals.") : "Could not load approved deals.",
                { type: "danger" },
            );
            // eslint-disable-next-line no-console
            console.error("PtlDragboard fetch failed", err);
        } finally {
            this.state.loading = false;
        }
    }

    // ─── DnD: card → calendar cell ────────────────────────────────────────

    onCardDragStart(ev, deal) {
        if (!ev.dataTransfer) return;
        ev.dataTransfer.effectAllowed = "copy";
        ev.dataTransfer.setData("text/plain", String(deal.id));
        ev.dataTransfer.setData("application/x-mint-ptl-deal", String(deal.id));
        ev.currentTarget.classList.add("o_ptl_card_dragging");
    }

    onCardDragEnd(ev) {
        ev.currentTarget.classList.remove("o_ptl_card_dragging");
    }

    /**
     * After the calendar renders, find day cells and wire dragover/drop.
     * The calendar re-renders on navigation, so we observe and re-bind.
     */
    _wireCalendarDropTargets() {
        const container = document.querySelector(".o_calendar_renderer") || document.body;

        const bind = () => {
            this._clearCellListeners();
            const cells = container.querySelectorAll(".fc-daygrid-day[data-date], .fc-day[data-date]");
            cells.forEach((cell) => {
                const over = (ev) => {
                    if (ev.dataTransfer?.types?.includes("application/x-mint-ptl-deal")) {
                        ev.preventDefault();
                        ev.dataTransfer.dropEffect = "copy";
                        cell.classList.add("o_ptl_drop_hover");
                    }
                };
                const leave = () => cell.classList.remove("o_ptl_drop_hover");
                const drop = (ev) => {
                    ev.preventDefault();
                    cell.classList.remove("o_ptl_drop_hover");
                    const dealId = parseInt(
                        ev.dataTransfer?.getData("application/x-mint-ptl-deal") ||
                            ev.dataTransfer?.getData("text/plain"),
                        10,
                    );
                    const date = cell.getAttribute("data-date");
                    if (!dealId || !date) return;
                    this._scheduleDealOnDate(dealId, date);
                };
                cell.addEventListener("dragover", over);
                cell.addEventListener("dragleave", leave);
                cell.addEventListener("drop", drop);
                this._cellListeners.push({ cell, over, leave, drop });
            });
        };

        bind();
        // Re-bind whenever the calendar swaps its DOM (navigation, scale change).
        this._mutationObs = new MutationObserver(() => bind());
        this._mutationObs.observe(container, { childList: true, subtree: true });
    }

    _clearCellListeners() {
        for (const { cell, over, leave, drop } of this._cellListeners) {
            cell.removeEventListener("dragover", over);
            cell.removeEventListener("dragleave", leave);
            cell.removeEventListener("drop", drop);
        }
        this._cellListeners = [];
    }

    async _scheduleDealOnDate(dealId, date) {
        // Resolve market for the new day. Prefer the search-bar market
        // filter; fall back to the deal's own market.
        let marketId = this.marketId;
        if (!marketId) {
            const deal = this.state.deals.find((d) => d.id === dealId);
            marketId = deal?.market_id || null;
        }
        if (!marketId) {
            this.notification.add(
                "Pick a Market in the search bar before scheduling — or set Market on the deal.",
                { type: "warning" },
            );
            return;
        }
        try {
            await this.orm.call("mint.ptl.day", "schedule_deal", [], {
                deal_id: dealId,
                date,
                market_id: marketId,
            });
            this.notification.add("Deal scheduled.", { type: "success" });
            // Reload the calendar so the new/updated day appears.
            await this.props.model.load();
        } catch (err) {
            this.notification.add(
                err?.data?.message || err?.message || "Could not schedule the deal.",
                { type: "danger" },
            );
        }
    }

    // ─── helpers ──────────────────────────────────────────────────────────

    _isoDate(d) {
        // Accept Date, luxon DateTime, or any object with toISODate/toFormat.
        if (!d) return null;
        if (typeof d.toISODate === "function") return d.toISODate();
        if (typeof d.toFormat === "function") return d.toFormat("yyyy-MM-dd");
        if (d instanceof Date) {
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, "0");
            const day = String(d.getDate()).padStart(2, "0");
            return `${y}-${m}-${day}`;
        }
        return null;
    }
}

/**
 * Renderer wrapper: lays out the standard CalendarRenderer next to a
 * PtlDragboard side pane. Forwards all props to the original renderer.
 */
export class PtlCalendarRenderer extends Component {
    static template = "mint_command_center.PtlCalendarRenderer";
    static components = { CalendarRenderer, PtlDragboard };
    static props = CalendarRenderer.props;
}

registry.category("views").add("ptl_calendar", {
    ...calendarView,
    Renderer: PtlCalendarRenderer,
});
