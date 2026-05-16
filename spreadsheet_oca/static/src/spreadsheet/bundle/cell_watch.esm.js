import {helpers} from "@odoo/o-spreadsheet";

const {toCartesian} = helpers;
const DEBOUNCE_MS = 5000;

function parseCellRef(ref) {
    const m = /^(?:'([^']+)'|([^!]+))!([A-Z]+\d+)$/.exec(ref.trim());
    if (!m) return null;
    return {sheetName: m[1] || m[2], xc: m[3]};
}

function findSheetId(model, sheetName) {
    for (const id of model.getters.getSheetIds()) {
        if (model.getters.getSheetName(id) === sheetName) return id;
    }
    return null;
}

function readWatchValue(model, watch) {
    const parsed = parseCellRef(watch.cell_ref);
    if (!parsed) return null;
    const sheetId = findSheetId(model, parsed.sheetName);
    if (!sheetId) return null;
    const [col, row] = toCartesian(parsed.xc);
    const cell = model.getters.getEvaluatedCell({sheetId, col, row});
    if (cell.type === "empty") return null;
    if (watch.value_type === "number") return Number(cell.value) || 0;
    if (watch.value_type === "bool") return Boolean(cell.value);
    return cell.formattedValue ?? String(cell.value ?? "");
}

export function attachCellWatcher({model, orm, http, spreadsheetId}) {
    let watches = [];
    let lastSent = new Map();
    let pending = null;

    const refreshWatches = async () => {
        watches = await orm.searchRead(
            "spreadsheet.cell.watch",
            [["spreadsheet_id", "=", spreadsheetId], ["active", "=", true]],
            ["id", "cell_ref", "value_type"]
        );
        lastSent = new Map();
    };

    const sync = async () => {
        if (!watches.length) return;
        const payload = [];
        for (const w of watches) {
            const value = readWatchValue(model, w);
            const key = `${w.id}`;
            const prev = lastSent.get(key);
            if (value === null) continue;
            if (prev !== undefined && prev === value) continue;
            payload.push({watch_id: w.id, value, type: w.value_type});
            lastSent.set(key, value);
        }
        if (!payload.length) return;
        try {
            await http.post("/spreadsheet_oca/watch/sync", {
                params: {spreadsheet_id: spreadsheetId, values: payload},
            });
        } catch (e) {
            console.warn("[cell_watch] sync failed", e);
        }
    };

    const schedule = () => {
        if (pending) clearTimeout(pending);
        pending = setTimeout(sync, DEBOUNCE_MS);
    };

    refreshWatches().then(schedule);
    model.on("update", null, schedule);

    return {
        refresh: refreshWatches,
        flush: () => {
            if (pending) clearTimeout(pending);
            return sync();
        },
    };
}
