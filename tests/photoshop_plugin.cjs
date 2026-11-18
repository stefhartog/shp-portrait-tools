/* Host-free UXP mocks. These are not evidence of Photoshop rendering/installation fidelity. */
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm"), fs = require("node:fs"), path = require("node:path");
const P = require("../plugins/photoshop/protocol.js");
const ProfileStore = require("../plugins/photoshop/profile_store.js");
const source = fs.readFileSync(path.join(__dirname, "../plugins/photoshop/main.js"), "utf8");
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../plugins/photoshop/manifest.json"), "utf8"));
const baseMatch = source.match(/const BASE = "([^"]+)";/);
assert(baseMatch, "Connector endpoint must be declared");
assert.deepEqual(manifest.requiredPermissions.network.domains, [new URL(baseMatch[1]).origin]);
assert.equal(new URL(baseMatch[1]).origin, "http://localhost:18765");

function documentInfo(id = 7) {
    return {id, name: `Disposable-${id}`, width: 4, height: 3, bitsPerChannel: 16, mode: "RGB",
        colorProfileName: "Test RGB", quickMaskMode: false, saved: false, path: "",
        activeHistoryState: {id: 42}, historyStates: [{id: 42}], layers: []};
}
function element() {
    return {children: [], appendChild(child) { this.children.push(child); },
        removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
        get firstChild() { return this.children[0]; }};
}
function fakeClock() {
    let now = 0, next = 0;
    const timers = new Map();
    return {
        setTimeout(callback, delay) { const id = ++next; timers.set(id, {at: now + delay, callback}); return id; },
        clearTimeout(id) { timers.delete(id); },
        async advance(duration) {
            const end = now + duration;
            while (true) {
                const nextTimer = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
                if (!nextTimer || nextTimer[1].at > end) break;
                now = nextTimer[1].at;
                timers.delete(nextTimer[0]);
                nextTimer[1].callback();
                await new Promise(resolve => setImmediate(resolve));
            }
            now = end;
        }
    };
}
function lookupProfile() {
    const offset = 144, size = 52 + 12 + 33 ** 3 * 3 * 2 + 12;
    const buffer = new ArrayBuffer(offset + size), view = new DataView(buffer);
    const text = (pos, value) => [...value].forEach((ch, index) => view.setUint8(pos + index, ch.charCodeAt(0)));
    view.setUint32(0, buffer.byteLength); text(12, "link"); text(16, "RGB "); text(20, "RGB "); text(36, "acsp");
    view.setUint32(128, 1); text(132, "A2B0"); view.setUint32(136, offset); view.setUint32(140, size);
    text(offset, "mft2"); view.setUint8(offset + 8, 3); view.setUint8(offset + 9, 3); view.setUint8(offset + 10, 33);
    view.setUint16(offset + 48, 2); view.setUint16(offset + 50, 2); view.setUint8(offset + 64, 123);
    return buffer;
}
function harness({failMask = false, cancel = false, onUpload = null, pngFailure = null,
    lookupFailure = null, rollbackFailure = false, cleanupFailure = false,
    profiles = [{id: "test", label: "Test ICC", profile: "Test RGB", file: "test.icc"}]} = {}) {
    const original = documentInfo(), other = documentInfo(8), calls = [], controls = new Map();
    const stored = new Map();
    let sequence = 100, modal = false, allocated = 0, historySnapshot;
    const temp = new Map();
    function folder() {
        const files = new Map();
        return {
            async createFile(name) {
                const file = {name, async write(bytes) { file.bytes = bytes; },
                    async getMetadata() { return {size: file.bytes ? file.bytes.byteLength : 0}; },
                    async read() { return file.bytes; }, async delete() { files.delete(name); }};
                files.set(name, file);
                return file;
            },
            async getEntries() { return [...files.values()]; },
            async delete() { if (cleanupFailure) throw new Error("mock cleanup failure"); temp.delete(this); }
        };
    }
    const localFileSystem = {
        async getTemporaryFolder() { return {async createFolder() { const item = folder(); temp.set(item, true); return item; }}; },
        async getDataFolder() { return folder(); },
        getFsUrl(file) { return file; }
    };
    const nodefs = {
        async open(file) { return file; },
        async read(file, buffer, offset, length, position) {
            if (pngFailure === "read") return {bytesRead: 0};
            const bytes = new Uint8Array(file.bytes);
            const count = Math.min(length, bytes.length - position);
            new Uint8Array(buffer, offset, count).set(bytes.subarray(position, position + count));
            return {bytesRead: count, buffer};
        },
        async close() {}
    };
    function remove(layer) {
        const collection = layer.parent ? layer.parent.layers : original.layers;
        const index = collection.indexOf(layer);
        if (index >= 0) collection.splice(index, 1);
    }
    function create(name, kind = "pixel") {
        assert(modal);
        const layer = {id: ++sequence, name, kind, layers: [], parent: null,
            async move(target, position) {
                remove(layer);
                if (position === "inside") { layer.parent = target; target.layers.unshift(layer); }
                else {
                    layer.parent = target.parent;
                    const collection = layer.parent ? layer.parent.layers : original.layers;
                    collection.splice(collection.indexOf(target), 0, layer);
                }
            }};
        original.layers.unshift(layer);
        return layer;
    }
    original.createLayerGroup = async ({name}) => create(name, "group");
    original.createPixelLayer = async ({name}) => create(name);
    original.duplicate = async (_name, merged) => {
        assert(modal); assert.equal(merged, true);
        calls.push(["duplicate", original.id]);
        if (pngFailure === "same") return original;
        const copy = documentInfo(++sequence);
        copy.saveAs = {async png(file, options, asCopy) {
            assert(modal); assert.equal(asCopy, true);
            assert.equal(ps.app.activeDocument.id, copy.id);
            calls.push(["savePng", copy.id]);
            if (pngFailure === "save") throw new Error("mock PNG save failure");
            if (pngFailure === "changed") original.activeHistoryState = {id: 99};
            if (pngFailure !== "empty") file.bytes = new Uint8Array([137, 80, 78, 71]).buffer;
        }};
        copy.closeWithoutSaving = async () => {
            assert(modal);
            calls.push(["closeCopy", copy.id]);
            ps.app.documents.splice(ps.app.documents.indexOf(copy), 1);
        };
        ps.app.documents.push(copy); ps.app.activeDocument = copy;
        return copy;
    };
    let lookupData;
    const ps = {
        app: {documents: [original, other], activeDocument: original},
        action: {async batchPlay([command]) {
            assert(modal);
            calls.push(["action", command._obj]);
            if (command._obj === "make") {
                if (lookupFailure === "oldSelection") {
                    original.activeLayers = [original.layers.find(layer => layer.name === "Original")];
                    return [{_obj: "make"}];
                }
                const layer = create(command.using.name);
                layer.kind = lookupFailure === "wrongKind" ? "pixel" : "colorLookup";
                original.activeLayers = [layer];
                return [{_obj: "make"}];
            }
            if (command._obj === "set") {
                assert.equal(ps.app.activeDocument.id, original.id);
                assert.equal(original.activeLayers.length, 1);
                assert.equal(original.activeLayers[0].kind, "colorLookup");
                assert.equal(JSON.stringify(command._target), JSON.stringify([
                    {_ref: "adjustmentLayer", _enum: "ordinal", _value: "targetEnum"}]));
                assert.equal(command.to._obj, "colorLookup");
                assert.equal(command.to.lookupType._value, "deviceLinkProfile");
                assert.equal(command.to.profile._rawData, "binary");
                assert(command.to.profile._data.byteLength > 200000);
                if (lookupFailure === "action") return [{_obj: "error", message: "The command \u201cSet\u201d is not currently available."}];
                lookupData = command.to.profile._data;
                return [{_obj: "set"}];
            }
            if (command._obj === "get") {
                if (lookupFailure === "missing") return [{adjustment: [{_obj: "colorLookup"}]}];
                const bytes = lookupData.slice(0);
                if (lookupFailure === "changed" || lookupFailure === "directChanged") new Uint8Array(bytes)[208] ^= 1;
                return [{adjustment: [{_obj: "colorLookup", lookupType: {_value: "deviceLinkProfile"},
                    profile: lookupFailure === "direct" || lookupFailure === "directChanged" ?
                        bytes : {_rawData: "binary", _data: bytes}}]}];
            }
            throw new Error("Unexpected Photoshop action");
        }},
        constants: {BitsPerChannelType: {EIGHT: 8, SIXTEEN: 16}, DocumentMode: {RGB: "RGB"},
            LayerKind: {COLORLOOKUP: "colorLookup", GROUP: "group"},
            BlendMode: {NORMAL: "normal", SOFTLIGHT: "softLight", LINEARLIGHT: "linearLight", PASSTHROUGH: "passThrough"},
            ElementPlacement: {PLACEINSIDE: "inside", PLACEBEFORE: "before"}},
        core: {async executeAsModal(fn) {
            assert(!modal, "Modal scopes must not overlap");
            modal = true;
            try {
                return await fn({isCancelled: cancel, reportProgress(value) { calls.push(["progress", value.value]); }, hostControl: {
                    async suspendHistory(options) { calls.push(["suspend", options]); historySnapshot = [...original.layers]; return 1; },
                    async registerAutoCloseDocument(id) { calls.push(["autoClose", id]); },
                    async unregisterAutoCloseDocument(id) { calls.push(["unregister", id]); },
                    async resumeHistory(id, commit) {
                        calls.push(["resume", commit]);
                        if (!commit && rollbackFailure) throw new Error("mock rollback failure");
                        if (!commit) original.layers.splice(0, original.layers.length, ...historySnapshot);
                        if (commit) {
                            original.activeHistoryState = {id: ++sequence};
                            original.historyStates.push(original.activeHistoryState);
                        }
                    }
                }});
            } finally { modal = false; }
        }},
        imaging: {
            async createImageDataFromBuffer(samples, options) {
                assert(modal); assert.equal(options.fullRange, true);
                ++allocated;
                return {samples, options, dispose() { --allocated; }};
            },
            async putPixels(options) {
                assert.equal(options.documentID, original.id);
                calls.push(["pixels", options.layerID, [...options.imageData.samples]]);
            },
            async putLayerMask(options) {
                assert.equal(options.documentID, original.id);
                if (failMask) throw new Error("mock mask failure");
                calls.push(["mask", options.layerID, [...options.imageData.samples]]);
            },
            async getPixels(options) {
                assert(modal, "Capture pixels inside a modal scope");
                assert.equal(options.applyAlpha, false);
                assert.equal(options.targetSize, undefined);
                const samples = new Uint16Array(4 * 3 * 4);
                for (let i = 0; i < samples.length; i += 4) samples.set([1, 32768, 65535, 65535], i);
                ++allocated;
                return {level: 0, sourceBounds: {left: 0, top: 0, right: 4, bottom: 3},
                    imageData: {width: 4, height: 3, components: 4, componentSize: 16, colorSpace: "RGB",
                        colorProfile: "Test RGB", async getData(args) {
                            assert.equal(args.fullRange, true); return samples;
                        }, dispose() { --allocated; }}};
            }
        }
    };
    let bundle;
    class MockProfileStore {
        constructor() { this.entries = [...profiles]; }
        async initialize() {}
        matching(profile) { return this.entries.filter(entry => entry.profile === profile); }
        async read(entry) { return entry.bytes || new ArrayBuffer(128); }
        async save(bytes, label, profile) {
            const entry = {id: `saved-${this.entries.length}`, label, profile, bytes};
            this.entries.push(entry);
            return entry;
        }
    }
    const context = vm.createContext({
        require(name) {
            if (name === "photoshop") return ps;
            if (name === "uxp") return {storage: {localFileSystem, formats: {binary: "binary"}, secureStorage: {}}};
            if (name === "fs") return nodefs;
            if (name === "./protocol.js") return P;
            if (name === "./profile_store.js") return MockProfileStore;
            throw new Error(`Unexpected dependency ${name}`);
        },
        document: {readyState: "loading",
            getElementById(id) { if (!controls.has(id)) controls.set(id, element()); return controls.get(id); },
            createElement() { return element(); }},
        window: {addEventListener() {}},
        localStorage: {getItem(key) { return stored.get(key) || null; }, setItem(key, value) { stored.set(key, value); }},
        console: {error() {}}, Uint8Array, Uint16Array, ArrayBuffer, setTimeout, clearTimeout,
        async fetch(url, options) {
            assert(!modal, "Network access must not happen inside executeAsModal");
            if (options.method === "POST") {
                calls.push(["upload", url, options.body.byteLength]);
                if (onUpload) onUpload(original, url);
                return {ok: true, json: async () => ({})};
            }
            const query = new URL(url).searchParams;
            const metadata = bundle.blobs[query.get("name")];
            const length = Number(query.get("length"));
            if (metadata.kind === "icc-lut") return {ok: true, arrayBuffer: async () => lookupProfile()};
            const data = new ArrayBuffer(length);
            if (metadata.components === 3) new Uint16Array(data).fill(32768);
            else new Uint8Array(data).fill(128);
            return {ok: true, arrayBuffer: async () => data};
        }
    });
    vm.runInContext(source, context);
    vm.runInContext("connected = true; token = 'unit-test';", context);
    const session = vm.runInContext("session", context);
    const marker = context.marker(original);
    function makeJob() {
        const capture = "unit-capture";
        const info = {index: original.id, name: original.name, width: 4, height: 3, depth: 16,
            mode: "RGB", alpha: "opaque", profile: "Test RGB", session, protocol: 1};
        vm.runInContext(`captures.set(${JSON.stringify(capture)}, {info: ${JSON.stringify(info)}, marker: ${JSON.stringify(marker)}})`, context);
        bundle = {doc: {...info, marker, capture},
            blobs: {
                rgb: {bytes: 4 * 3 * 3 * 2, width: 4, height: 3, components: 3, depth: 16},
                mask: {bytes: 4 * 3, width: 4, height: 3, components: 1, depth: 8}
            },
            layers: [{name: "SkinTools", blend: "Normal", opacity: 100, children: [
                {name: "Detail", blend: "Linear Light", opacity: 100, pixels: "rgb"},
                {name: "Corrections", blend: "Pass Through", opacity: 100, mask: "mask", children: [
                    {name: "D&B", blend: "Soft Light", opacity: 65, pixels: "rgb"},
                    {name: "Nested", blend: "Normal", opacity: 100, children: [
                        {name: "Hue", blend: "Normal", opacity: 100, pixels: "rgb"},
                        {name: "Directional Blur", blend: "Normal", opacity: 100, pixels: "rgb", mask: "mask"}]}]},
                {name: "Color", blend: "Normal", opacity: 100, pixels: "rgb"}
            ]}]};
        return {request: "r1", kind: "apply", protocol: 1, session, params: bundle};
    }
    function enforceScopedReads() {
        for (const doc of [original, other]) {
            Object.defineProperty(doc, "quickMaskMode", {
                get() { assert(modal, "Document metadata must be read inside a modal scope"); return false; }
            });
        }
    }
    return {context, ps, original, other, calls, makeJob, temp, enforceScopedReads, controls, localFileSystem, stored,
        allocated: () => allocated, session};
}

async function testPairingStatus() {
    let finishStartup;
    const startup = new Promise(resolve => { finishStartup = resolve; });
    const controls = new Map(), requests = [], saved = new Map();
    let rejectPair = true;
    class MockProfileStore {
        constructor() { this.entries = []; }
        async initialize() { await startup; }
    }
    const context = vm.createContext({
        require(name) {
            if (name === "photoshop") return {};
            if (name === "./protocol.js") return P;
            if (name === "./profile_store.js") return MockProfileStore;
            if (name === "uxp") return {storage: {
                localFileSystem: {async getDataFolder() { return {getEntries: () => startup}; }},
                formats: {binary: "binary"},
                secureStorage: {async setItem(key, value) { saved.set(key, value); }}
            }};
            throw new Error(`Unexpected dependency ${name}`);
        },
        document: {readyState: "loading", getElementById(id) { if (!controls.has(id)) controls.set(id, element()); return controls.get(id); }},
        window: {addEventListener() {}},
        console: {error() {}}, Uint8Array, ArrayBuffer, setTimeout, clearTimeout,
        async fetch(url, options) {
            const route = url.split("/").pop();
            requests.push(route);
            if (route === "pair") {
                if (rejectPair) return {ok: false, status: 400, text: async () => "Incorrect pairing code"};
                return {ok: true, json: async () => ({token: "test-token"})};
            }
            if (route === "hello") {
                assert.equal(options.headers.Authorization, "Bearer test-token");
                return {ok: true, json: async () => ({protocol: 1})};
            }
            if (route === "poll") return new Promise(() => {});
            throw new Error(`Unexpected route ${route}`);
        }
    });
    vm.runInContext(source, context);
    context.document.getElementById("code").value = "test-code";
    await controls.get("pair").onclick();
    assert.match(controls.get("status").textContent, /HTTP 400: Incorrect pairing code/);
    assert.equal(saved.size, 0);
    rejectPair = false;
    await controls.get("pair").onclick();
    assert.match(controls.get("status").textContent, /^Connected/);
    assert.equal(controls.get("code").value, "");
    assert.equal(saved.get("tools-token"), "test-token");
    assert.deepEqual(requests, ["pair", "pair", "hello", "poll"]);
    finishStartup([]);
    await new Promise(resolve => setImmediate(resolve));
    assert.match(controls.get("status").textContent, /^Connected/, "Late startup must not overwrite connection status");
}

async function testAutoConnect() {
    function setup({storedToken = "test-token", responses = []} = {}) {
        const clock = fakeClock(), controls = new Map(), requests = [], listeners = new Map();
        const lifecycle = {};
        class MockProfileStore {
            constructor() { this.entries = []; }
            async initialize() {}
        }
        const tokenBytes = storedToken ? Uint8Array.from(storedToken, char => char.charCodeAt(0)) : null;
        const context = vm.createContext({
            require(name) {
                if (name === "photoshop") return {};
                if (name === "./protocol.js") return P;
                if (name === "./profile_store.js") return MockProfileStore;
                if (name === "uxp") return {entrypoints: {setup(value) { lifecycle.setup = value; }},
                    storage: {localFileSystem: {}, formats: {binary: "binary"},
                    secureStorage: {length: tokenBytes ? 1 : 0, async getItem(key) {
                        assert.equal(key, "tools-token");
                        return tokenBytes;
                    }}}};
                throw new Error(`Unexpected dependency ${name}`);
            },
            document: {readyState: "loading",
                getElementById(id) { if (!controls.has(id)) controls.set(id, element()); return controls.get(id); },
                createElement() { return element(); }},
            window: {addEventListener(name, callback) {
                if (!listeners.has(name)) listeners.set(name, []);
                listeners.get(name).push(callback);
            }},
            console: {error() {}}, Uint8Array, ArrayBuffer,
            setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
            async fetch(url, options) {
                const route = url.split("/").pop();
                requests.push(route);
                if (route === "hello") {
                    assert.equal(options.headers.Authorization, `Bearer ${storedToken}`);
                    const response = responses.shift() || {ok: true};
                    return response.ok ? {ok: true, json: async () => ({protocol: 1})} :
                        {ok: false, status: response.status, text: async () => response.text || "Tools unavailable"};
                }
                if (route === "poll") return new Promise(() => {});
                throw new Error(`Unexpected route ${route}`);
            }
        });
        vm.runInContext(source, context);
        return {context, clock, controls, requests, lifecycle,
            fire(name) { for (const callback of listeners.get(name) || []) callback(); }};
    }

    const recovering = setup({responses: [
        {ok: false, status: 503, text: "Tools offline"}, {ok: true}
    ]});
    assert.equal(typeof recovering.lifecycle.setup.panels.skinTools.create, "function");
    assert.equal(typeof recovering.lifecycle.setup.panels.skinTools.show, "function");
    recovering.lifecycle.setup.panels.skinTools.create();
    recovering.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(recovering.requests, ["hello"], "Startup must try the saved credential without pairing");
    assert.match(recovering.controls.get("status").textContent, /Retrying in 1s/);
    await recovering.clock.advance(999);
    assert.deepEqual(recovering.requests, ["hello"], "Retry must wait for its backoff");
    await recovering.clock.advance(1);
    assert.deepEqual(recovering.requests, ["hello", "hello", "poll"]);
    assert.match(recovering.controls.get("status").textContent, /^Connected/);
    recovering.fire("unload");
    await recovering.clock.advance(120000);
    assert.equal(recovering.requests.filter(route => route === "hello").length, 2,
        "Unloading the panel must stop future connection attempts");

    const noCredential = setup({storedToken: null});
    noCredential.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(noCredential.requests, []);
    assert.match(noCredential.controls.get("status").textContent, /Not paired/);
    await noCredential.clock.advance(120000);
    assert.deepEqual(noCredential.requests, [], "Missing credentials must not create pairing or retry");

    const rejected = setup({responses: [{ok: false, status: 401, text: "Unauthorized"}]});
    rejected.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(rejected.requests, ["hello"]);
    assert.match(rejected.controls.get("status").textContent, /rejected the saved pairing/);
    await rejected.clock.advance(120000);
    assert.deepEqual(rejected.requests, ["hello"], "Rejected credentials must not be retried indefinitely");

    const capped = setup({responses: Array.from({length: 8}, () => ({ok: false, status: 503}))});
    capped.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    for (const delay of [1000, 2000, 4000, 8000, 16000]) {
        await capped.clock.advance(delay);
        await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(capped.requests.filter(route => route === "hello").length, 6);
    assert.match(capped.controls.get("status").textContent, /Retrying in 30s/);
    await capped.clock.advance(29999);
    assert.equal(capped.requests.filter(route => route === "hello").length, 6);
    await capped.clock.advance(1);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(capped.requests.filter(route => route === "hello").length, 7,
        "Retry backoff must remain capped at 30 seconds");

    const disconnected = setup({responses: [{ok: false, status: 503}]});
    disconnected.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    await disconnected.controls.get("disconnect").onclick();
    await disconnected.clock.advance(120000);
    assert.deepEqual(disconnected.requests, ["hello"], "Manual disconnect must cancel pending retries");

    const unloaded = setup({responses: [{ok: false, status: 503}]});
    unloaded.fire("load");
    await new Promise(resolve => setImmediate(resolve));
    unloaded.fire("unload");
    await unloaded.clock.advance(120000);
    assert.deepEqual(unloaded.requests, ["hello"], "Panel unload must cancel pending retries");
}

async function testProfileStore() {
    const files = new Map();
    const folder = {
        async getEntries() { return [...files.values()]; },
        async getEntry(name) {
            if (!files.has(name)) throw new Error("Missing file");
            return files.get(name);
        },
        async createFile(name, options) {
            if (files.has(name) && !options.overwrite) throw new Error("File exists");
            const file = {name, content: null,
                async write(data) { file.content = typeof data === "string" ? data : data.slice(0); files.set(name, file); },
                async read() { return typeof file.content === "string" ? file.content : file.content.slice(0); },
                async getMetadata() { return {size: typeof file.content === "string" ? Buffer.byteLength(file.content) : file.content.byteLength}; }};
            return file;
        }
    };
    const legacy = await folder.createFile("source-profile.icc", {overwrite: false});
    const first = new Uint8Array(128).fill(1).buffer, second = new Uint8Array(128).fill(2).buffer;
    await legacy.write(first);
    const store = new ProfileStore({async getDataFolder() { return folder; }}, "binary");
    await store.initialize();
    assert.equal(store.entries.length, 1);
    assert.equal(store.entries[0].profile, null, "Legacy file must not be assigned to an unconfirmed profile");
    assert.equal(store.matching("Profile A").length, 0);
    const old = store.entries[0];
    const migrated = await store.save(await store.read(old), old.label, "Profile A", old);
    assert.notEqual(migrated.file, old.file, "Confirmed legacy selections must be copied into an independent cache file");
    const same = await store.save(first, "another name.icc", "Profile A");
    assert.equal(same.id, migrated.id, "Byte-identical selections should not create ambiguity");
    const different = await store.save(second, "different.icc", "Profile A");
    assert.notEqual(different.id, migrated.id);
    assert.equal(store.matching("Profile A").length, 2);
    await store.save(second, "other.icc", "Profile B");
    const restarted = new ProfileStore({async getDataFolder() { return folder; }}, "binary");
    await restarted.initialize();
    assert.equal(restarted.matching("Profile A").length, 2);
    assert.equal(restarted.matching("Profile B").length, 1);
    assert.deepEqual([...new Uint8Array(await legacy.read())], [...new Uint8Array(first)], "Legacy bytes must remain intact");
    await legacy.write(second);
    assert.deepEqual([...new Uint8Array(await restarted.read(restarted.matching("Profile A").find(entry => entry.legacySource)))],
        [...new Uint8Array(first)], "Changing the old cache must not change the confirmed profile");
    files.delete(different.file);
    const broken = new ProfileStore({async getDataFolder() { return folder; }}, "binary");
    await assert.rejects(broken.initialize(), /invalid or its file is missing/);
}

async function testProfileFolderShortcut() {
    const h = harness({profiles: []});
    const folder = {isFolder: true, name: "Recommended"};
    const file = {name: "selected.icc", async getMetadata() { return {size: 128}; },
        async read() { return new ArrayBuffer(128); }};
    const options = [];
    let folderCalls = 0, savedToken = null, pickerFile = file;
    h.localFileSystem.getFolder = async () => { ++folderCalls; return folder; };
    h.localFileSystem.createPersistentToken = async entry => {
        assert.equal(entry, folder);
        return "folder-token";
    };
    h.localFileSystem.getEntryForPersistentToken = async token => {
        savedToken = token;
        return folder;
    };
    h.localFileSystem.getFileForOpening = async option => { options.push(option); return pickerFile; };

    await h.controls.get("adobeProfile").onclick();
    assert.equal(folderCalls, 1, "The first use must request a folder grant");
    assert.equal(h.stored.get("profile-folder-token"), "folder-token");
    assert.equal(options[0].initialLocation, folder);
    assert.deepEqual(Array.from(options[0].types), ["icc", "icm"]);
    assert.match(h.controls.get("profileName").textContent, /selected.icc for Test RGB/);
    assert.match(h.controls.get("status").textContent, /ICC selected/);

    pickerFile = null;
    await h.controls.get("adobeProfile").onclick();
    assert.equal(folderCalls, 1, "The remembered folder should not require another selection");
    assert.equal(savedToken, "folder-token");
    assert.equal(options[1].initialLocation, folder);
    await h.controls.get("profile").onclick();
    assert.equal(options[2].initialLocation, undefined, "Manual browsing remains available");

    h.localFileSystem.getEntryForPersistentToken = async () => { throw new Error("grant expired"); };
    await h.controls.get("adobeProfile").onclick();
    assert.match(h.controls.get("status").textContent, /grant expired.*Choose or change/);
    assert.equal(folderCalls, 1, "A stale grant must not silently open or replace a folder");
    await h.controls.get("changeProfileFolder").onclick();
    assert.equal(folderCalls, 2);
    assert.equal(h.stored.get("profile-folder-token"), "folder-token");

    const cancelled = harness({profiles: []});
    cancelled.localFileSystem.getFolder = async () => null;
    cancelled.localFileSystem.getFileForOpening = async () => { throw new Error("Picker should not open"); };
    await cancelled.controls.get("adobeProfile").onclick();
    assert.match(cancelled.controls.get("status").textContent, /No profile folder selected/);
    assert.equal(cancelled.stored.size, 0);

    h.localFileSystem.getEntryForPersistentToken = async () => folder;
    h.localFileSystem.getFileForOpening = async () => { throw new Error("permission revoked"); };
    await h.controls.get("adobeProfile").onclick();
    assert.match(h.controls.get("status").textContent, /permission revoked.*Choose or change/);

    h.localFileSystem.getFileForOpening = async () => {
        h.ps.app.activeDocument = h.other;
        return file;
    };
    const before = vm.runInContext("profileStore.entries.length", h.context);
    await h.controls.get("adobeProfile").onclick();
    assert.match(h.controls.get("status").textContent, /Active document or its profile changed/);
    assert.equal(vm.runInContext("profileStore.entries.length", h.context), before);

    vm.runInContext("commandActive = true", h.context);
    await h.controls.get("changeProfileFolder").onclick();
    assert.match(h.controls.get("status").textContent, /Wait for the current Tools operation/);
    assert.equal(folderCalls, 2);
}

async function test() {
    await testAutoConnect();
    await testPairingStatus();
    await testProfileStore();
    await testProfileFolderShortcut();
    const validLookup = lookupProfile(), invalidLookup = validLookup.slice(0);
    new DataView(invalidLookup).setUint32(136, validLookup.byteLength + 1);
    assert.throws(() => P.validateLookup(invalidLookup), /bounds/);
    assert(P.sameLookupTransform(validLookup, validLookup.slice(0)));
    for (const failure of [null, "direct", "directChanged", "action", "missing", "changed", "wrongKind", "oldSelection"]) {
        const fixture = harness({lookupFailure: failure}), job = fixture.makeJob(), bytes = lookupProfile();
        const shouldFail = failure && failure !== "direct";
        if (shouldFail) fixture.original.layers.push({id: 5, name: "Original", kind: "colorLookup"});
        job.params.blobs.lut = {kind: "icc-lut", bytes: bytes.byteLength};
        job.params.layers[0].children.unshift({name: "Hue", blend: "Normal", opacity: 63, lookup: "lut", mask: "mask"});
        const invalid = structuredClone(job.params);
        invalid.layers[0].children[0].pixels = "rgb";
        assert.throws(() => P.validateBundle(invalid, fixture.session), /lookup layer/);
        const result = await fixture.context.perform(job);
        if (shouldFail) {
            assert(result.error, `Must reject ${failure} lookup`);
            if (failure === "action") assert.match(result.error, /The command .Set. is not currently available/);
            assert(fixture.calls.some(item => item[0] === "resume" && item[1] === false));
            assert.deepEqual(fixture.original.layers.map(layer => layer.name), ["Original"]);
        } else {
            assert(!result.error, result.error);
            assert.deepEqual(fixture.calls.filter(item => item[0] === "action").map(item => item[1]), ["make", "set", "get"]);
            const hue = fixture.original.layers[0].layers[0];
            assert.equal(hue.name, "Hue");
            assert.equal(hue.opacity, 63);
            assert(fixture.calls.some(item => item[0] === "mask" && item[1] === hue.id));
            assert(fixture.calls.some(item => item[0] === "resume" && item[1] === true));
        }
        assert.equal(fixture.temp.size, 0);
    }
    {
        const fixture = harness({lookupFailure: "action", rollbackFailure: true}), job = fixture.makeJob(), bytes = lookupProfile();
        job.params.blobs.lut = {kind: "icc-lut", bytes: bytes.byteLength};
        job.params.layers[0].children.unshift({name: "Hue", blend: "Normal", opacity: 63, lookup: "lut"});
        const result = await fixture.context.perform(job);
        assert.match(result.error, /The command .Set. is not currently available/);
        assert.match(result.error, /Rollback failed: mock rollback failure/);
        assert.match(result.error, /Inspect the document before retrying/);
    }
    {
        const fixture = harness({lookupFailure: "directChanged", rollbackFailure: true}), job = fixture.makeJob(), bytes = lookupProfile();
        job.params.blobs.lut = {kind: "icc-lut", bytes: bytes.byteLength};
        job.params.layers[0].children.unshift({name: "Hue", blend: "Normal", opacity: 63, lookup: "lut"});
        const result = await fixture.context.perform(job);
        assert.match(result.error, /different Hue lookup transform/);
        assert.match(result.error, /Rollback failed: mock rollback failure/);
        assert.doesNotMatch(result.error, /import rolled back/i);
    }
    {
        const fixture = harness({lookupFailure: "action", cleanupFailure: true}), job = fixture.makeJob(), bytes = lookupProfile();
        job.params.blobs.lut = {kind: "icc-lut", bytes: bytes.byteLength};
        job.params.layers[0].children.unshift({name: "Hue", blend: "Normal", opacity: 63, lookup: "lut"});
        const result = await fixture.context.perform(job);
        assert.match(result.error, /The command .Set. is not currently available/);
        assert.match(result.error, /temporary buffer cleanup failed: mock cleanup failure/);
    }
    const rgba = new Uint16Array([0, 32768, 65535, 65535, 65535, 1, 0, 65535]);
    const image = {width: 2, height: 1, componentSize: 16, components: 4, colorSpace: "RGB"};
    const bounds = {left: 0, top: 0, right: 2, bottom: 1};
    assert.deepEqual([...P.padTile(image, rgba, bounds, 2, 0, 1, 16)], [0, 32768, 65535, 65535, 1, 0]);
    rgba[3] = 123;
    assert.throws(() => P.padTile(image, rgba, bounds, 2, 0, 1, 16), /Non-opaque/);
    assert.throws(() => P.padTile(image, rgba, bounds, 3, 0, 1, 16), /Transparent/);
    for (const [depth, Type, max] of [[8, Uint8ClampedArray, 255], [16, Uint16Array, 65535]]) {
        const rgb = new Type([99, 0, 1, max, max, 128, 0, 99]).subarray(1, 7);
        const packed = P.padTile({...image, componentSize: depth, components: 3}, rgb, bounds, 2, 0, 1, depth);
        assert.deepEqual([...packed], [0, 1, max, max, 128, 0]);
        rgb.fill(0);
        assert.deepEqual([...packed], [0, 1, max, max, 128, 0], "tile owns its data");
        assert.throws(() => P.padTile({...image, componentSize: depth, components: 3},
            rgb.subarray(0, 5), bounds, 2, 0, 1, depth), /Invalid trimmed/);
    }

    const h = harness(), job = h.makeJob();
    h.enforceScopedReads();
    P.validateBundle(job.params, h.session);
    const invalid = structuredClone(job.params);
    invalid.layers[0].children[0].blend = "arbitrary";
    assert.throws(() => P.validateBundle(invalid, h.session), /metadata/);
    invalid.layers[0].children[0].blend = "Normal"; invalid.blobs.rgb.bytes++;
    assert.throws(() => P.validateBundle(invalid, h.session), /metadata/);
    assert.throws(() => P.validateBundle(job.params, "expired"), /expired/);
    h.ps.app.activeDocument = h.other;
    const first = await h.context.perform(job), second = await h.context.perform(job);
    assert(first.result, first.error);
    for (const phase of ["download", "stage", "import"])
        assert(Number.isFinite(first.result.timing[phase]) && first.result.timing[phase] >= 0);
    assert.equal(first, second, "Duplicate request must reuse its outcome without inserting again");
    assert.equal(h.original.layers.length, 1);
    assert.deepEqual(h.original.layers[0].layers.map(layer => layer.name), ["Detail", "Corrections", "Color"]);
    const corrections = h.original.layers[0].layers[1];
    assert.deepEqual(corrections.layers.map(layer => layer.name), ["D&B", "Nested"]);
    assert.deepEqual(corrections.layers[1].layers.map(layer => layer.name), ["Hue", "Directional Blur"]);
    assert(h.calls.some(item => item[0] === "mask" && item[1] === corrections.id),
        "Skin mask must attach to Corrections, not SkinTools");
    assert(!h.calls.some(item => item[0] === "mask" && item[1] === h.original.layers[0].id),
        "Parent SkinTools folder must not receive the skin mask");
    assert.equal(h.ps.app.activeDocument, h.other, "Restore the user's active tab");
    assert.equal(h.calls.filter(item => item[0] === "resume" && item[1] === true).length, 1);
    assert.equal(h.allocated(), 0);
    assert.equal(h.temp.size, 0);
    const firstGroup = h.original.layers[0];
    const anotherSend = await h.context.perform({...job, request: "r2"});
    assert.equal(anotherSend.result.repeatable, true, anotherSend.error);
    assert.equal(h.original.layers.length, 2);
    assert.equal(h.original.layers[1], firstGroup, "Prior group must remain intact");
    assert.notEqual(h.original.layers[0].id, firstGroup.id);
    assert.equal(h.ps.app.activeDocument, h.other);
    assert.equal(await h.context.perform(job), first);
    assert.equal(h.original.layers.length, 2, "Replayed requests must not insert again");

    for (const options of [{failMask: true}, {cancel: true}]) {
        const test = harness(options), result = await test.context.perform(test.makeJob());
        assert(result.error);
        assert.equal(test.original.layers.length, 0, "Failed/cancelled imports must roll back");
        if (options.failMask) assert(test.calls.some(item => item[0] === "resume" && item[1] === false));
        else assert(!test.calls.some(item => item[0] === "suspend"), "Preflight cancellation must not start an import");
        assert.equal(test.allocated(), 0);
        assert.equal(test.temp.size, 0);
    }
    const stale = harness(), staleJob = stale.makeJob();
    stale.original.activeHistoryState = {id: 99};
    assert((await stale.context.perform(staleJob)).result, "Content edits are intentionally not detected");
    assert.equal(stale.original.layers.length, 1);
    assert.equal(stale.temp.size, 0);
    const closed = harness(), closedJob = closed.makeJob();
    closed.ps.app.documents = [closed.other];
    assert.match((await closed.context.perform(closedJob)).error, /closed/);
    const resized = harness(), resizedJob = resized.makeJob();
    resized.original.width++;
    assert.match((await resized.context.perform(resizedJob)).error, /changed/);
    for (const [field, value] of [["colorProfileName", "Other RGB"], ["mode", "CMYK"], ["bitsPerChannel", 8]]) {
        const candidate = harness(), operation = candidate.makeJob();
        candidate.original[field] = value;
        assert((await candidate.context.perform(operation)).error, `${field} changes must fail`);
        assert.equal(candidate.original.layers.length, 0);
    }
    for (const [field, value] of [["name", "Other"], ["profile", "Other RGB"], ["width", 3], ["marker", "tampered"]]) {
        const candidate = harness(), operation = candidate.makeJob();
        operation.params.doc[field] = value;
        assert((await candidate.context.perform(operation)).error, `Tampered ${field} must fail`);
        assert.equal(candidate.original.layers.length, 0);
    }
    {
        const candidate = harness(), operation = candidate.makeJob();
        operation.params.layers = [{name: "Result", blend: "Normal", opacity: 100, pixels: "rgb", mask: "mask"}];
        const result = await candidate.context.perform(operation);
        assert(result.result, result.error);
        assert.equal(candidate.original.layers[0].name, "SkinTools");
        assert.equal(candidate.original.layers[0].blendMode, "passThrough");
        assert.deepEqual(candidate.original.layers[0].layers.map(layer => layer.name), ["Result"]);
        assert.equal(candidate.calls.filter(item => item[0] === "progress").at(-1)[1], 1);
    }
    {
        const candidate = harness(), operation = candidate.makeJob();
        assert((await candidate.context.perform(operation)).result);
        await candidate.controls.get("disconnect").onclick();
        vm.runInContext("connected = true", candidate.context);
        assert.match((await candidate.context.perform({...operation, request: "r2"})).error, /expired/);
    }
    const capture = harness();
    capture.enforceScopedReads();
    const status = await capture.context.perform({request: "status", kind: "status", protocol: 1, session: capture.session});
    assert.equal(status.result.doc.index, capture.original.id);
    const captured = await capture.context.capture({request: "capture"});
    assert.equal(captured.doc.depth, 16);
    assert.equal(captured.pixel_tiles.length, 1);
    const [components, readback, packing] = captured.pixel_tiles[0];
    assert.equal(components, 4);
    assert([readback, packing].every(value => Number.isFinite(value) && value >= 0));
    assert.equal(captured.timing.serialize, readback + packing);
    assert.equal(capture.allocated(), 0);
    assert.equal(capture.calls.filter(item => item[0] === "upload").length, 2);
    assert.equal(capture.calls.find(item => item[0] === "upload")[2], 4 * 3 * 3 * 2);
    for (const failure of [null, "save", "empty", "read", "changed", "same"]) {
        const candidate = harness({pngFailure: failure, profiles: []});
        candidate.enforceScopedReads();
        const job = {request: "capture-png", params: {format: "png"}};
        if (failure) {
            const expected = {save: /mock PNG save failure/, empty: /empty or too large/,
                read: /truncated/, changed: /changed the source/i, same: /independent capture copy/};
            await assert.rejects(candidate.context.capture(job), expected[failure]);
        } else {
            const result = await candidate.context.capture(job);
            assert.equal(result.png_bytes, 4);
            for (const phase of ["export", "file_read", "upload"])
                assert(Number.isFinite(result.timing[phase]) && result.timing[phase] >= 0);
            assert.equal(candidate.calls.filter(item => item[0] === "upload").length, 1);
            assert.match(candidate.calls.find(item => item[0] === "upload")[1], /name=png/);
        }
        assert.equal(candidate.temp.size, 0);
        assert(candidate.ps.app.documents.includes(candidate.original));
        assert.equal(candidate.ps.app.activeDocument, candidate.original);
        if (failure !== "same") {
            assert(candidate.calls.some(item => item[0] === "autoClose"));
            assert(candidate.calls.some(item => item[0] === "closeCopy"));
        }
    }
    const interrupted = harness({onUpload(doc) { doc.activeHistoryState = {id: 99}; }});
    await assert.rejects(interrupted.context.capture({request: "capture"}), /changed/);
    assert.equal(interrupted.allocated(), 0);
    const cancelled = harness({cancel: true});
    await assert.rejects(cancelled.context.capture({request: "capture"}), /cancelled/);
    assert.equal(cancelled.calls.filter(item => item[0] === "upload").length, 0);
    const selecting = harness();
    vm.runInContext("profileWorking = true", selecting.context);
    await assert.rejects(selecting.context.capture({request: "capture"}), /Finish selecting/);
    assert.equal(selecting.calls.filter(item => item[0] === "upload").length, 0);
    const ambiguous = harness({profiles: [
        {id: "a", label: "first.icc", profile: "Test RGB", file: "a.icc"},
        {id: "b", label: "second.icc", profile: "Test RGB", file: "b.icc"}
    ]});
    await assert.rejects(ambiguous.context.capture({request: "capture"}), /Multiple saved ICC/);
    assert.equal(ambiguous.calls.filter(item => item[0] === "upload").length, 0);
    const entries = vm.runInContext("profileStore.entries", ambiguous.context);
    await ambiguous.context.chooseProfile(entries[1], {index: ambiguous.original.id, profile: "Test RGB"});
    assert((await ambiguous.context.capture({request: "capture"})).doc);
    ambiguous.original.activeHistoryState = {id: 55};
    await assert.rejects(ambiguous.context.capture({request: "capture"}), /Multiple saved ICC/);
    ambiguous.original.colorProfileName = "Other RGB";
    await assert.rejects(ambiguous.context.capture({request: "capture"}), /No saved ICC/);
    console.log("Photoshop connector host-free tests passed: saved-token auto-connect/retry, ICC cache and migration, scoped full-range capture, original target, layers, masks, rollback, cancellation, stale sources, no replay and buffer disposal.");
}
test().catch(error => { console.error(error); process.exitCode = 1; });
