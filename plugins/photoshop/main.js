"use strict";
const ps = require("photoshop"), uxp = require("uxp");
const P = require("./protocol.js");
const ProfileStore = require("./profile_store.js");
const fs = uxp.storage.localFileSystem, binary = uxp.storage.formats.binary;
const BASE = "http://localhost:18765/api/photoshop/";
const session = `ps-${Date.now()}-${Math.random().toString(36).slice(2)}`;
let token = null, connected = false, running = false, profileWorking = false, commandActive = false;
let captures = new Map(), handled = new Map();
const profileStore = new ProfileStore(fs, binary), selectedProfiles = new Map();
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
    await profileReady;
    P.check(!profileWorking, "Finish selecting an ICC profile in the connector, then Get Image again.");
    const {info, before} = await ps.core.executeAsModal(async context => {
        P.check(!context.isCancelled, "Skin Tools capture cancelled.");
        const doc = ps.app.activeDocument, info = documentInfo(doc);
        return {info, before: marker(doc)};
    }, {commandName: "Prepare Skin Tools capture", timeOut: 1});
    const chosen = selectedProfiles.get(info.index);
    const matches = profileStore.matching(info.profile);
    const selected = chosen && chosen.marker === before && matches.includes(chosen.entry) ? chosen.entry :
        matches.length === 1 ? matches[0] : null;
    P.check(selected, matches.length > 1 ?
        `Multiple saved ICC files match "${info.profile}". In the connector choose the exact file or add it again for this document, then Get Image again.` :
        `No saved ICC for "${info.profile}". In the connector add the exact ICC for this document, then Get Image again.`);
    const profileBytes = await profileStore.read(selected);
    $("profileName").textContent = `Using ${selected.label} for ${info.profile}. A matching name does not prove identical embedded ICC bytes.`;
    function checkSource() {
        const doc = Array.from(ps.app.documents).find(item => item.id === info.index);
        P.check(doc && marker(doc) === before, "Document closed or changed during capture; Get Image again.");
        return doc;
    }
    const rows = P.rows(info.width, 4, info.depth);
    let offset = 0;
    for (let y = 0; y < info.height; y += rows) {
        ensureConnected();
        const height = Math.min(rows, info.height - y);
        const tile = await ps.core.executeAsModal(async context => {
            P.check(!context.isCancelled, "Skin Tools capture cancelled.");
            const doc = checkSource();
            const captured = await ps.imaging.getPixels({documentID: doc.id,
                sourceBounds: {left: 0, top: y, right: info.width, bottom: y + height},
                componentSize: info.depth, applyAlpha: false});
            try {
                P.check(captured.level === 0 && captured.imageData.colorProfile === info.profile,
                    "Photoshop returned downsampled or converted pixels.");
                const data = await captured.imageData.getData({fullRange: true});
                P.check(!context.isCancelled, "Skin Tools capture cancelled.");
                return P.padTile(captured.imageData, data, captured.sourceBounds, info.width, y, height, info.depth);
            } finally { captured.imageData.dispose(); }
        }, {commandName: "Capture Skin Tools pixels", timeOut: 1});
        const data = tile.buffer;
        await request(`chunk?request=${job.request}&name=pixels&offset=${offset}`, undefined, data);
        offset += data.byteLength;
        status(`Capturing ${Math.round((y + height) / info.height * 100)}%`);
    }
    await upload(job, "icc", profileBytes);
    await ps.core.executeAsModal(async context => {
        P.check(!context.isCancelled, "Skin Tools capture cancelled.");
        checkSource();
    }, {commandName: "Verify Skin Tools capture", timeOut: 1});
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    captures.clear();
    captures.set(id, before);
    return {doc: {...info, marker: before, capture: id}};
}

async function stage(job, bundle) {
    const folder = await (await fs.getTemporaryFolder()).createFolder(job.request);
    const tiles = {}, lookups = {};
    const total = Object.values(bundle.blobs).reduce((sum, blob) => sum + blob.bytes, 0);
    let downloaded = 0;
    try {
        for (const [name, blob] of Object.entries(bundle.blobs)) {
            if (blob.kind === "icc-lut") {
                ensureConnected();
                const bytes = await request(`chunk?request=${job.request}&name=${name}&offset=0&length=${blob.bytes}`);
                P.check(bytes.byteLength === blob.bytes, "Incomplete Hue lookup.");
                lookups[name] = P.validateLookup(bytes);
                downloaded += bytes.byteLength;
                continue;
            }
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
        return {folder, tiles, lookups};
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

async function batch(command) {
    const result = await ps.action.batchPlay([command], {});
    P.check(Array.isArray(result) && result.length === 1, "Photoshop returned an invalid action result.");
    const item = result[0];
    P.check(item && String(item._obj || "").toLowerCase() !== "error" &&
        !(typeof item.result === "number" && item.result < 0),
    `Photoshop Color Lookup failed: ${item && item.message || "unknown action error"}`);
    return item;
}

function returnedProfile(profile) {
    if (profile instanceof ArrayBuffer) return profile;
    const value = profile && profile._data;
    if (value instanceof ArrayBuffer) return value;
    if (ArrayBuffer.isView(value)) return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
    if (profile && profile._rawData === "base64" && typeof value === "string" && value.length <= P.CHUNK * 2) {
        const decoded = atob(value), bytes = new Uint8Array(decoded.length);
        for (let i = 0; i < decoded.length; ++i) bytes[i] = decoded.charCodeAt(i);
        return bytes.buffer;
    }
    throw new Error("Photoshop did not return verifiable Hue lookup profile data.");
}

async function createLookup(doc, node, bytes) {
    const existing = new Set();
    function collect(layers) {
        for (const layer of layers) {
            existing.add(layer.id);
            if (layer.kind === ps.constants.LayerKind.GROUP) collect(layer.layers);
        }
    }
    collect(doc.layers);
    await batch({_obj: "make", _target: [{_ref: "adjustmentLayer"}],
        using: {_obj: "adjustmentLayer", type: {_class: "colorLookup"}, name: node.name},
        _options: {dialogOptions: "silent"}});
    const active = doc.activeLayers, layer = active[0];
    P.check(ps.app.activeDocument.id === doc.id && active.length === 1 && layer &&
        Number.isInteger(layer.id) && !existing.has(layer.id) && layer.kind === ps.constants.LayerKind.COLORLOOKUP,
    "Photoshop did not select the new Hue Color Lookup adjustment.");
    await batch({_obj: "set", _target: [{_ref: "adjustmentLayer", _enum: "ordinal", _value: "targetEnum"}],
        to: {_obj: "colorLookup", lookupType: {_enum: "colorLookupType", _value: "deviceLinkProfile"},
            name: "SHP Skin Tools Hue", profile: {_rawData: "binary", _data: bytes}},
        _options: {dialogOptions: "silent"}});
    const info = await batch({_obj: "get", _target: [{_property: "adjustment"},
        {_ref: "layer", _id: layer.id}, {_ref: "document", _id: doc.id}]});
    const entries = Array.isArray(info.adjustment) ? info.adjustment : [info.adjustment];
    const lookup = entries.find(item => item && item._obj === "colorLookup");
    P.check(lookup && lookup.lookupType && lookup.lookupType._value === "deviceLinkProfile",
        "Photoshop did not confirm the Hue device-link adjustment.");
    P.check(P.sameLookupTransform(bytes, returnedProfile(lookup.profile)),
        "Photoshop returned a different Hue lookup transform.");
    return layer;
}

async function apply(job) {
    const bundle = P.validateBundle(job.params, session);
    await ps.core.executeAsModal(async context => {
        P.check(!context.isCancelled, "Skin Tools import cancelled.");
        target(bundle.doc);
    }, {commandName: "Check Skin Tools target", timeOut: 1});
    const staged = await stage(job, bundle);
    let result, importCommitted = false, importFailure;
    try {
        result = await ps.core.executeAsModal(async context => {
            ensureConnected();
            const doc = target(bundle.doc), previous = ps.app.activeDocument;
            const history = await context.hostControl.suspendHistory({documentID: doc.id, name: "Skin Tools"});
            let committed = false, importError;
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
                            node.lookup ? await createLookup(doc, node, staged.lookups[node.lookup]) :
                            await doc.createPixelLayer({name: node.name});
                        if (parent) await layer.move(parent, ps.constants.ElementPlacement.PLACEINSIDE);
                        else if (doc.layers[0].id !== layer.id)
                            await layer.move(doc.layers[0], ps.constants.ElementPlacement.PLACEBEFORE);
                        layer.blendMode = modes[node.blend];
                        layer.opacity = node.opacity;
                        if (node.children) await create(node.children, layer);
                        else if (!node.lookup) await writeTiles(doc, layer, node.pixels, bundle, staged, false, context);
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
            } catch (error) {
                importError = error;
                throw error;
            } finally {
                let rollbackError;
                if (!committed) {
                    try { await context.hostControl.resumeHistory(history, false); }
                    catch (error) { rollbackError = error; }
                }
                if (previous && Array.from(ps.app.documents).some(item => item.id === previous.id))
                    ps.app.activeDocument = previous;
                if (rollbackError)
                    throw new Error(`${importError ? importError.message : "Import failed"}. Rollback failed: ${rollbackError.message}. Inspect the document before retrying.`);
            }
        }, {commandName: "Import Skin Tools layers", timeOut: 1});
    } catch (error) {
        importFailure = error;
        if (importCommitted)
            throw new Error(`Layers were committed, but post-import handling failed: ${error.message}. Inspect the document; do not resend.`);
        throw error;
    } finally {
        try { await cleanup(staged.folder); }
        catch (error) {
            throw new Error(`${importFailure ? `${importFailure.message}. ` : ""}${importCommitted ? "Layers were committed" : "Import did not complete"}; temporary buffer cleanup failed: ${error.message}. Inspect the document; do not resend.`);
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
    commandActive = true;
    let value;
    try {
        if (job.kind === "status") {
            const doc = await ps.core.executeAsModal(async context => {
                P.check(!context.isCancelled, "Skin Tools status cancelled.");
                return ps.app.documents.length ? documentInfo(ps.app.activeDocument) : null;
            }, {commandName: "Check Skin Tools document", timeOut: 1});
            value = {result: {doc}};
        } else if (job.kind === "capture") value = {result: await capture(job)};
        else if (job.kind === "apply") value = {result: await apply(job)};
        else throw new Error("Unsupported connector command.");
    } catch (error) {
        console.error(error);
        value = {error: String(error.message || error).slice(0, 2000)};
    } finally { commandActive = false; }
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
    await request("hello", {session, protocol: P.VERSION, capabilities: ["hue-lut-device-link-v1"]});
    connected = true;
    status("Connected to local Tools. Use Get Image in Skin Tools.");
    void loop();
}

function action(id, callback) {
    $(id).onclick = async () => {
        $(id).disabled = true;
        try { await callback(); }
        catch (error) { console.error(error); status(error.message || String(error)); }
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
async function activeProfile() {
    return ps.core.executeAsModal(async context => {
        P.check(!context.isCancelled, "ICC selection cancelled.");
        const doc = ps.app.activeDocument;
        return {info: documentInfo(doc), marker: marker(doc)};
    }, {commandName: "Select Skin Tools ICC", timeOut: 1});
}

async function chooseProfile(entry, info) {
    const current = await activeProfile();
    P.check(current.info.index === info.index && current.info.profile === info.profile,
        "Active document or its profile changed; select the ICC again.");
    P.check(entry.profile === null || entry.profile === current.info.profile,
        `This saved ICC was selected for "${entry.profile}", not "${current.info.profile}". Add the exact ICC for this document.`);
    const bytes = await profileStore.read(entry);
    const selected = entry.profile === null ? await profileStore.save(bytes, entry.label, current.info.profile, entry) : entry;
    selectedProfiles.set(current.info.index, {entry: selected, marker: current.marker});
    $("profileName").textContent = `Selected ${selected.label} for ${current.info.profile}. Tools checks its description on Get Image; name matching does not prove identical embedded bytes.`;
    renderProfiles();
}

function renderProfiles() {
    const list = $("profiles");
    while (list.firstChild) list.removeChild(list.firstChild);
    for (const entry of profileStore.entries) {
        if (entry.profile === null && profileStore.entries.some(other => other.legacySource)) continue;
        const button = document.createElement("button");
        button.textContent = `${entry.label} (${entry.profile || "previous selection: confirm for active document"}) [${entry.id.slice(-8)}]`;
        button.onclick = async () => {
            if (profileWorking) { status("An ICC selection is already in progress."); return; }
            if (commandActive) { status("Wait for the current Tools operation before selecting an ICC."); return; }
            profileWorking = true;
            try { await profileReady; await chooseProfile(entry, (await activeProfile()).info); }
            catch (error) { console.error(error); status(error.message || String(error)); }
            finally { profileWorking = false; }
        };
        list.appendChild(button);
    }
}

const PROFILE_FOLDER_TOKEN = "profile-folder-token";

async function chooseProfileFolder() {
    status("Choose the Adobe profiles folder, or another folder containing your ICC files.");
    const folder = await fs.getFolder();
    if (!folder) {
        status("No profile folder selected. You can still browse for an ICC file.");
        return null;
    }
    P.check(folder.isFolder, "The selected profile location is not a folder.");
    const persistent = await fs.createPersistentToken(folder);
    P.check(typeof persistent === "string" && persistent.length > 0, "Could not remember the profile folder.");
    localStorage.setItem(PROFILE_FOLDER_TOKEN, persistent);
    status("Profile folder saved. Select the exact ICC for the active document.");
    return folder;
}

async function savedProfileFolder() {
    const persistent = localStorage.getItem(PROFILE_FOLDER_TOKEN);
    if (!persistent) return chooseProfileFolder();
    try {
        const folder = await fs.getEntryForPersistentToken(persistent);
        P.check(folder && folder.isFolder, "The saved location is not a folder.");
        return folder;
    } catch (error) {
        throw new Error(`Saved profile folder is unavailable: ${error.message || String(error)}. Choose or change the profile folder again.`);
    }
}

async function addProfile(selectFile) {
    P.check(!profileWorking, "An ICC selection is already in progress.");
    P.check(!commandActive, "Wait for the current Tools operation before selecting an ICC.");
    profileWorking = true;
    try {
        await profileReady;
        const {info} = await activeProfile();
        const file = await selectFile();
        if (!file) return;
        const metadata = await file.getMetadata();
        P.check(metadata.size >= 128 && metadata.size <= 4 * 1024 * 1024, "ICC file must be 128 bytes to 4 MiB.");
        const bytes = await file.read({format: binary});
        const current = await activeProfile();
        P.check(current.info.index === info.index && current.info.profile === info.profile,
            "Active document or its profile changed; select the ICC again.");
        const selected = await profileStore.save(bytes, file.name, info.profile);
        selectedProfiles.set(info.index, {entry: selected, marker: current.marker});
        $("profileName").textContent = `Selected ${selected.label} for ${info.profile}. Tools checks its description on Get Image; name matching does not prove identical embedded bytes.`;
        status("ICC selected. Use Get Image in Skin Tools.");
        renderProfiles();
    } finally { profileWorking = false; }
}

action("profile", () => addProfile(() => fs.getFileForOpening({types: ["icc", "icm"]})));
action("adobeProfile", () => addProfile(async () => {
    const folder = await savedProfileFolder();
    if (!folder) return null;
    try {
        return await fs.getFileForOpening({types: ["icc", "icm"], initialLocation: folder});
    } catch (error) {
        throw new Error(`Cannot open the saved profile folder: ${error.message || String(error)}. Choose or change the profile folder again, or browse for an ICC file.`);
    }
}));
action("changeProfileFolder", async () => {
    P.check(!profileWorking, "An ICC selection is already in progress.");
    P.check(!commandActive, "Wait for the current Tools operation before selecting an ICC.");
    profileWorking = true;
    try { await chooseProfileFolder(); }
    finally { profileWorking = false; }
});
const profileReady = profileStore.initialize().then(() => {
    renderProfiles();
    if (profileStore.entries.length) $("profileName").textContent = "Saved ICC profiles loaded. Confirm the exact file for each document.";
});
profileReady.catch(error => { console.error(error); status(`Cannot load saved ICC profiles: ${error.message || String(error)}`); });
