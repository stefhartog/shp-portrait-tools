"use strict";
const ps = require("photoshop"), uxp = require("uxp");
const P = require("./protocol.js");
const fs = uxp.storage.localFileSystem, binary = uxp.storage.formats.binary;
const BASE = "http://127.0.0.1:18765/api/photoshop/";
const session = `ps-${Date.now()}-${Math.random().toString(36).slice(2)}`;
let token = null, connected = false, running = false, profileBytes = null;
let captures = new Map(), handled = new Map();
const $ = id => document.getElementById(id);
function status(text) { $("status").textContent = text; }
function ensureConnected() { P.check(connected, "Connector disconnected; operation cancelled."); }

async function request(route, value, bytes) {
    const headers = {"X-SHP-Session": session};
    if (token) headers.Authorization = `Bearer ${token}`;
    const options = {method: value === undefined && bytes === undefined ? "GET" : "POST", headers};
    if (bytes !== undefined) {
        headers["Content-Type"] = "application/octet-stream";
        options.body = bytes;
    } else if (value !== undefined) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(value);
    }
    const response = await fetch(BASE + route, options);
    if (!response.ok) {
        const text = await response.text();
        throw new Error(`Local Tools HTTP ${response.status}: ${text.slice(0, 1000)}`);
    }
    if (route.startsWith("chunk?") && options.method === "GET") return response.arrayBuffer();
    return response.json();
}

function documentInfo(doc) {
    P.check(doc, "Open a disposable RGB document first.");
    const depth = doc.bitsPerChannel === ps.constants.BitsPerChannelType.EIGHT ? 8 :
        doc.bitsPerChannel === ps.constants.BitsPerChannelType.SIXTEEN ? 16 : 0;
    P.check(doc.mode === ps.constants.DocumentMode.RGB && depth,
        "Only RGB 8/16-bit documents are supported. CMYK, Lab and 32-bit/HDR are not converted.");
    P.dimensions(doc.width, doc.height);
    P.check(doc.colorProfileName && doc.colorProfileName !== "None", "Assign an explicit RGB profile to a disposable copy first.");
    P.check(!doc.quickMaskMode, "Leave Quick Mask before using the connector.");
    return {index: doc.id, name: doc.name, width: doc.width, height: doc.height,
        depth, mode: "RGB", profile: doc.colorProfileName, alpha: "opaque", session, protocol: P.VERSION};
}

function marker(doc) {
    const info = documentInfo(doc);
    return JSON.stringify([info.index, info.width, info.height, info.depth, info.mode, info.profile,
        doc.activeHistoryState.id, Array.from(doc.historyStates).map(state => state.id)]);
}

function target(source) {
    P.check(source.session === session && captures.get(source.capture) === source.marker,
        "Source snapshot expired; Get Image again.");
    const doc = Array.from(ps.app.documents).find(item => item.id === source.index);
    P.check(doc && marker(doc) === source.marker, "Source closed or changed since Get Image; no layers inserted.");
    return doc;
}

async function upload(job, name, bytes) {
    for (let offset = 0; offset < bytes.byteLength; offset += P.CHUNK) {
        ensureConnected();
        await request(`chunk?request=${job.request}&name=${name}&offset=${offset}`, undefined,
            bytes.slice(offset, offset + P.CHUNK));
    }
}

async function capture(job) {
    const doc = ps.app.activeDocument, info = documentInfo(doc);
    P.check(profileBytes, "Select the exact document ICC file in this panel first. Tools verifies its description.");
    const before = marker(doc);
    const rows = P.rows(info.width, 4, info.depth);
    let offset = 0;
    for (let y = 0; y < info.height; y += rows) {
        ensureConnected();
        P.check(marker(doc) === before, "Document changed during capture; Get Image again.");
        const height = Math.min(rows, info.height - y);
        const captured = await ps.imaging.getPixels({documentID: doc.id,
            sourceBounds: {left: 0, top: y, right: info.width, bottom: y + height},
            componentSize: info.depth, applyAlpha: false});
        let tile;
        try {
            P.check(captured.level === 0 && captured.imageData.colorProfile === info.profile,
                "Photoshop returned downsampled or converted pixels.");
            tile = P.padTile(captured.imageData, await captured.imageData.getData({fullRange: true}),
                captured.sourceBounds, info.width, y, height, info.depth);
        } finally { captured.imageData.dispose(); }
        const data = tile.buffer;
        await request(`chunk?request=${job.request}&name=pixels&offset=${offset}`, undefined, data);
        offset += data.byteLength;
        status(`Capturing ${Math.round((y + height) / info.height * 100)}%`);
    }
    await upload(job, "icc", profileBytes);
    P.check(marker(doc) === before, "Document changed during capture; Get Image again.");
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    captures.clear();
    captures.set(id, before);
    return {doc: {...info, marker: before, capture: id}};
}

async function stage(job, bundle) {
    const folder = await (await fs.getTemporaryFolder()).createFolder(job.request);
    const tiles = {};
    const total = Object.values(bundle.blobs).reduce((sum, blob) => sum + blob.bytes, 0);
    let downloaded = 0;
    try {
        for (const [name, blob] of Object.entries(bundle.blobs)) {
            tiles[name] = [];
            const stride = blob.width * blob.components * blob.depth / 8;
            const rows = P.rows(blob.width, blob.components, blob.depth);
            for (let y = 0; y < blob.height; y += rows) {
                ensureConnected();
                const height = Math.min(rows, blob.height - y), length = height * stride;
                const bytes = await request(`chunk?request=${job.request}&name=${name}&offset=${y * stride}&length=${length}`);
                P.check(bytes.byteLength === length, "Incomplete layer tile.");
                const file = await folder.createFile(`${name}-${y}`, {overwrite: false});
                await file.write(bytes, {format: binary});
                tiles[name].push({file, y, height});
                downloaded += length;
                status(`Receiving editable layers ${Math.round(downloaded / total * 100)}%`);
            }
        }
        return {folder, tiles};
    } catch (error) {
        await cleanup(folder);
        throw error;
    }
}

async function cleanup(folder) {
    for (const entry of await folder.getEntries()) await entry.delete();
    await folder.delete();
}

async function writeTiles(doc, layer, name, bundle, staged, mask, context) {
    const blob = bundle.blobs[name];
    for (const [index, tile] of staged.tiles[name].entries()) {
        ensureConnected();
        P.check(!context.isCancelled, "Skin Tools import cancelled.");
        const data = await tile.file.read({format: binary});
        const samples = blob.depth === 16 ? new Uint16Array(data) : new Uint8Array(data);
        const image = await ps.imaging.createImageDataFromBuffer(samples, {
            width: blob.width, height: tile.height, components: blob.components,
            chunky: true, fullRange: true,
            colorSpace: mask ? "Grayscale" : "RGB",
            colorProfile: mask ? "Gray Gamma 2.2" : bundle.doc.profile
        });
        try {
            const options = {documentID: doc.id, layerID: layer.id, imageData: image,
                replace: index === 0, targetBounds: {left: 0, top: tile.y}};
            if (mask) await ps.imaging.putLayerMask(options);
            else await ps.imaging.putPixels(options);
        } finally { image.dispose(); }
    }
}

async function apply(job) {
    const bundle = P.validateBundle(job.params, session);
    target(bundle.doc);
    const staged = await stage(job, bundle);
    let result, importCommitted = false;
    try {
        result = await ps.core.executeAsModal(async context => {
            ensureConnected();
            const doc = target(bundle.doc), previous = ps.app.activeDocument;
            const history = await context.hostControl.suspendHistory({documentID: doc.id, name: "Skin Tools"});
            let committed = false;
            try {
                ps.app.activeDocument = doc;
                const modes = {"Normal": ps.constants.BlendMode.NORMAL, "Soft Light": ps.constants.BlendMode.SOFTLIGHT,
                    "Linear Light": ps.constants.BlendMode.LINEARLIGHT, "Pass Through": ps.constants.BlendMode.PASSTHROUGH};
                const count = nodes => nodes.reduce((sum, node) => sum + 1 + (node.children ? count(node.children) : 0), 0);
                const total = count(bundle.layers);
                let completed = 0;
                async function create(nodes, parent) {
                    const created = [];
                    // Photoshop inserts above the current layer. Build bottom-to-top, then explicitly place.
                    for (const node of [...nodes].reverse()) {
                        P.check(!context.isCancelled, "Skin Tools import cancelled.");
                        const layer = node.children ? await doc.createLayerGroup({name: node.name}) :
                            await doc.createPixelLayer({name: node.name});
                        if (parent) await layer.move(parent, ps.constants.ElementPlacement.PLACEINSIDE);
                        else if (doc.layers[0].id !== layer.id)
                            await layer.move(doc.layers[0], ps.constants.ElementPlacement.PLACEBEFORE);
                        layer.blendMode = modes[node.blend];
                        layer.opacity = node.opacity;
                        if (node.children) await create(node.children, layer);
                        else await writeTiles(doc, layer, node.pixels, bundle, staged, false, context);
                        if (node.mask) await writeTiles(doc, layer, node.mask, bundle, staged, true, context);
                        context.reportProgress({value: ++completed / total, commandName: `Importing ${node.name}`});
                        created.unshift(layer.id);
                    }
                    return created;
                }
                const ids = await create(bundle.layers, null);
                ensureConnected();
                P.check(!context.isCancelled, "Skin Tools import cancelled.");
                await context.hostControl.resumeHistory(history, true);
                committed = true;
                importCommitted = true;
                captures.delete(bundle.doc.capture);
                return {document: doc.name, layers: ids, request: job.request, sent: true};
            } finally {
                if (!committed) await context.hostControl.resumeHistory(history, false);
                if (previous && Array.from(ps.app.documents).some(item => item.id === previous.id))
                    ps.app.activeDocument = previous;
            }
        }, {commandName: "Import Skin Tools layers", timeOut: 1});
    } catch (error) {
        if (importCommitted)
            throw new Error(`Layers were committed, but post-import handling failed: ${error.message}. Inspect the document; do not resend.`);
        throw error;
    } finally {
        try { await cleanup(staged.folder); }
        catch (error) {
            throw new Error(`${importCommitted ? "Layers were committed" : "Import did not complete"}; temporary buffer cleanup failed: ${error.message}. Inspect the document; do not resend.`);
        }
    }
    return result;
}

async function perform(job) {
    P.check(job.protocol === P.VERSION && job.session === session && P.identifier.test(job.request),
        "Invalid command identity.");
    if (handled.has(job.request)) return handled.get(job.request);
    // Record before executing: even if reporting fails, this command is never rerun.
    handled.set(job.request, {error: "Command outcome uncertain; inspect the document. Not replayed."});
    let value;
    try {
        if (job.kind === "status") {
            const doc = ps.app.documents.length ? documentInfo(ps.app.activeDocument) : null;
            value = {result: {doc}};
        } else if (job.kind === "capture") value = {result: await capture(job)};
        else if (job.kind === "apply") value = {result: await apply(job)};
        else throw new Error("Unsupported connector command.");
    } catch (error) {
        console.error(error);
        value = {error: String(error.message || error).slice(0, 2000)};
    }
    handled.set(job.request, value);
    while (handled.size > 32) handled.delete(handled.keys().next().value);
    return value;
}

async function loop() {
    if (running) return;
    running = true;
    try {
        while (connected) {
            const job = await request("poll");
            if (job) {
                const value = await perform(job);
                // Never replay a mutation on a lost reply. Tools exposes request status for inspection.
                await request("reply", {request: job.request, value});
                status(value.error || "Completed. Get Image again before sending another result.");
            }
            await new Promise(resolve => setTimeout(resolve, 750));
        }
    } catch (error) {
        connected = false;
        status(`${error.message}\nDisconnected. Inspect any in-flight import before reconnecting; it will not be replayed.`);
        console.error(error);
    } finally { running = false; }
}

async function connect() {
    if (running) return;
    if (!token) token = String.fromCharCode(...await uxp.storage.secureStorage.getItem("tools-token"));
    await request("hello", {session, protocol: P.VERSION});
    connected = true;
    status("Connected to local Tools. Use Get Image in Skin Tools.");
    void loop();
}

function action(id, callback) {
    $(id).onclick = async () => {
        $(id).disabled = true;
        try { await callback(); }
        catch (error) { console.error(error); status(error.message); }
        finally { $(id).disabled = false; }
    };
}
action("pair", async () => {
    P.check(!running, "Disconnect before pairing again.");
    const response = await request("pair", {code: $("code").value.trim()});
    token = response.token;
    await uxp.storage.secureStorage.setItem("tools-token", token);
    $("code").value = "";
    await connect();
});
action("connect", connect);
action("disconnect", async () => {
    connected = false;
    captures.clear();
    status("Disconnected locally. Forget pairing in Tools to revoke the stored credential.");
});
action("profile", async () => {
    P.check(!running, "Disconnect before changing the ICC file, then reconnect.");
    const file = await fs.getFileForOpening({types: ["icc", "icm"]});
    if (!file) return;
    const metadata = await file.getMetadata();
    P.check(metadata.size >= 128 && metadata.size <= 4 * 1024 * 1024, "ICC file must be 128 bytes to 4 MiB.");
    profileBytes = await file.read({format: binary});
    const saved = await (await fs.getDataFolder()).createFile("source-profile.icc", {overwrite: true});
    await saved.write(profileBytes, {format: binary});
    $("profileName").textContent = `ICC selected: ${file.name}. Tools verifies exact description on Get Image.`;
});
async function initialize() {
    // Only plugin-owned storage is read at startup; never inspect a user's open documents.
    const entries = await (await fs.getDataFolder()).getEntries();
    const file = entries.find(entry => entry.name === "source-profile.icc");
    if (file) {
        const metadata = await file.getMetadata();
        P.check(metadata.size >= 128 && metadata.size <= 4 * 1024 * 1024, "Cached ICC file is invalid; select it again.");
        profileBytes = await file.read({format: binary});
        $("profileName").textContent = "Saved ICC loaded. Tools verifies it against the captured document.";
    }
    status("Select the source ICC file if needed, then Pair or Connect. Use disposable RGB documents only (unverified preview).");
}
initialize().catch(error => { console.error(error); status(error.message); });
