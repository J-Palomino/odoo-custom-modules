/** @odoo-module **/

/**
 * WebUSB transport for the Zebra ZD410 (vendorId 0x0A5F).
 *
 * Sends raw ZPL straight to a USB-attached Zebra from the browser. Works in
 * Chromium-based browsers only and requires HTTPS (the POS is). The first
 * pairing needs a user gesture (requestDevice); afterwards getDevices()
 * returns the already-permitted device with no prompt.
 *
 * Note on Windows: the OS print queue may hold the interface. If
 * claimInterface fails, the printer's driver must be replaced with WinUSB
 * (Zadig) — see the module README. On macOS/Linux this generally just works.
 */

const ZEBRA_VENDOR_ID = 0x0a5f;

function isSupported() {
    return typeof navigator !== "undefined" && !!navigator.usb;
}

async function getPairedZebra() {
    if (!isSupported()) {
        return null;
    }
    const devices = await navigator.usb.getDevices();
    return devices.find((d) => d.vendorId === ZEBRA_VENDOR_ID) || null;
}

async function requestZebra() {
    if (!isSupported()) {
        throw new Error("WebUSB is not supported in this browser");
    }
    return navigator.usb.requestDevice({ filters: [{ vendorId: ZEBRA_VENDOR_ID }] });
}

function findOutEndpoint(device) {
    const config = device.configuration;
    if (!config) {
        return null;
    }
    for (const iface of config.interfaces) {
        for (const alt of iface.alternates) {
            for (const ep of alt.endpoints) {
                if (ep.direction === "out") {
                    return {
                        interfaceNumber: iface.interfaceNumber,
                        endpoint: ep.endpointNumber,
                    };
                }
            }
        }
    }
    return null;
}

async function send(device, zpl) {
    if (!device.opened) {
        await device.open();
    }
    if (device.configuration === null) {
        await device.selectConfiguration(1);
    }
    const out = findOutEndpoint(device);
    if (!out) {
        throw new Error("No USB OUT endpoint found on the Zebra device");
    }
    await device.claimInterface(out.interfaceNumber);
    const data = new TextEncoder().encode(zpl);
    await device.transferOut(out.endpoint, data);
}

export const ZebraWebUsb = { isSupported, getPairedZebra, requestZebra, send };
