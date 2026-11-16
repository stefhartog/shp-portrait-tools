/* Host-free UXP mocks. These are not evidence of Photoshop rendering/installation fidelity. */
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm"), fs = require("node:fs"), path = require("node:path");
const P = require("../plugins/photoshop/protocol.js");
const source = fs.readFileSync(path.join(__dirname, "../plugins/photoshop/main.js"), "utf8");

function documentInfo(id = 7) {
    return {id, name: `Disposable-${id}`, width: 4, height: 3, bitsPerChannel: 16, mode: "RGB",
        colorProfileName: "Test RGB", quickMaskMode: false,
        activeHistoryState: {id: 42}, historyStates: [{id: 42}], layers: []};
}
function harness({failMask = false, cancel = false} = {}) {
    const original = documentInfo(), other = documentInfo(8), calls = [], controls = new Map();
    let sequence = 100, modal = false, allocated = 0;
    const temp = new Map();
    function folder() {
        const files = new Map();
        return {
            async createFile(name) {
                const file = {name, async write(bytes) { file.bytes = bytes; },
                    async read() { return file.bytes; }, async delete() { files.delete(name); }};
                files.set(name, file);
                return file;
            },
            async getEntries() { return [...files.values()]; },
            async delete() { temp.delete(this); }
        };
    }
    const localFileSystem = {
        async getTemporaryFolder() { return {async createFolder() { const item = folder(); temp.set(item, true); return item; }}; },
        async getDataFolder() { return folder(); }
    };
    function remove(layer) {
        const collection = layer.parent ? layer.parent.layers : original.layers;
        const index = collection.indexOf(layer);
        if (index >= 0) collection.splice(index, 1);
    }
    function create(name) {
        assert(modal);
        const layer = {id: ++sequence, name, layers: [], parent: null,
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
    original.createLayerGroup = async ({name}) => create(name);
    original.createPixelLayer = async ({name}) => create(name);
    const ps = {
        app: {documents: [original, other], activeDocument: original},
        constants: {BitsPerChannelType: {EIGHT: 8, SIXTEEN: 16}, DocumentMode: {RGB: "RGB"},
            BlendMode: {NORMAL: "normal", SOFTLIGHT: "softLight", LINEARLIGHT: "linearLight", PASSTHROUGH: "passThrough"},
            ElementPlacement: {PLACEINSIDE: "inside", PLACEBEFORE: "before"}},
        core: {async executeAsModal(fn) {
            modal = true;
            try {
                return await fn({isCancelled: cancel, reportProgress(value) { calls.push(["progress", value.value]); }, hostControl: {
                    async suspendHistory(options) { calls.push(["suspend", options]); return 1; },
                    async resumeHistory(id, commit) {
                        calls.push(["resume", commit]);
                        if (!commit) original.layers.length = 0;
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
    const context = vm.createContext({
        require(name) {
            if (name === "photoshop") return ps;
            if (name === "uxp") return {storage: {localFileSystem, formats: {binary: "binary"}, secureStorage: {}}};
            if (name === "./protocol.js") return P;
            throw new Error(`Unexpected dependency ${name}`);
        },
        document: {getElementById(id) { if (!controls.has(id)) controls.set(id, {}); return controls.get(id); }},
        console: {error() {}}, Uint8Array, Uint16Array, ArrayBuffer, setTimeout, clearTimeout,
        async fetch(url, options) {
            assert(!modal, "Network access must not happen inside executeAsModal");
            if (options.method === "POST") {
                calls.push(["upload", url, options.body.byteLength]);
                return {ok: true, json: async () => ({})};
            }
            const query = new URL(url).searchParams;
            const metadata = bundle.blobs[query.get("name")];
            const length = Number(query.get("length"));
            const data = new ArrayBuffer(length);
            if (metadata.components === 3) new Uint16Array(data).fill(32768);
            else new Uint8Array(data).fill(128);
            return {ok: true, arrayBuffer: async () => data};
        }
    });
    vm.runInContext(source, context);
    vm.runInContext("connected = true; token = 'unit-test'; profileBytes = new ArrayBuffer(128);", context);
    const session = vm.runInContext("session", context);
    const marker = context.marker(original);
    function makeJob() {
        const capture = "unit-capture";
        vm.runInContext(`captures.set(${JSON.stringify(capture)}, ${JSON.stringify(marker)})`, context);
        bundle = {doc: {index: original.id, name: original.name, width: 4, height: 3, depth: 16,
            mode: "RGB", alpha: "opaque", profile: "Test RGB", marker, session, capture, protocol: 1},
            blobs: {
                rgb: {bytes: 4 * 3 * 3 * 2, width: 4, height: 3, components: 3, depth: 16},
                mask: {bytes: 4 * 3, width: 4, height: 3, components: 1, depth: 8}
            },
            layers: [{name: "SkinTools", blend: "Normal", opacity: 100, mask: "mask", children: [
                {name: "D&B", blend: "Soft Light", opacity: 65, pixels: "rgb"},
                {name: "Nested", blend: "Normal", opacity: 100, children: [
                    {name: "Detail", blend: "Linear Light", opacity: 100, pixels: "rgb", mask: "mask"},
                    {name: "Color", blend: "Normal", opacity: 100, pixels: "rgb"}]}
            ]}]};
        return {request: "r1", kind: "apply", protocol: 1, session, params: bundle};
    }
    return {context, ps, original, other, calls, makeJob, temp,
        allocated: () => allocated, session};
}

async function test() {
    const rgba = new Uint16Array([0, 32768, 65535, 65535, 65535, 1, 0, 65535]);
    const image = {width: 2, height: 1, componentSize: 16, components: 4, colorSpace: "RGB"};
    const bounds = {left: 0, top: 0, right: 2, bottom: 1};
    assert.deepEqual([...P.padTile(image, rgba, bounds, 2, 0, 1, 16)], [0, 32768, 65535, 65535, 1, 0]);
    rgba[3] = 123;
    assert.throws(() => P.padTile(image, rgba, bounds, 2, 0, 1, 16), /Non-opaque/);
    assert.throws(() => P.padTile(image, rgba, bounds, 3, 0, 1, 16), /Transparent/);

    const h = harness(), job = h.makeJob();
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
    assert.equal(first, second, "Duplicate request must reuse its outcome without inserting again");
    assert.equal(h.original.layers.length, 1);
    assert.deepEqual(h.original.layers[0].layers.map(layer => layer.name), ["D&B", "Nested"]);
    assert.deepEqual(h.original.layers[0].layers[1].layers.map(layer => layer.name), ["Detail", "Color"]);
    assert.equal(h.ps.app.activeDocument, h.other, "Restore the user's active tab");
    assert.equal(h.calls.filter(item => item[0] === "resume" && item[1] === true).length, 1);
    assert.equal(h.allocated(), 0);
    assert.equal(h.temp.size, 0);

    for (const options of [{failMask: true}, {cancel: true}]) {
        const test = harness(options), result = await test.context.perform(test.makeJob());
        assert(result.error);
        assert.equal(test.original.layers.length, 0, "Failed/cancelled imports must roll back");
        assert(test.calls.some(item => item[0] === "resume" && item[1] === false));
        assert.equal(test.allocated(), 0);
        assert.equal(test.temp.size, 0);
    }
    const stale = harness(), staleJob = stale.makeJob();
    stale.original.activeHistoryState = {id: 99};
    assert.match((await stale.context.perform(staleJob)).error, /changed/);
    assert.equal(stale.original.layers.length, 0);
    assert.equal(stale.temp.size, 0);
    const closed = harness(), closedJob = closed.makeJob();
    closed.ps.app.documents = [closed.other];
    assert.match((await closed.context.perform(closedJob)).error, /closed/);
    const resized = harness(), resizedJob = resized.makeJob();
    resized.original.width++;
    assert.match((await resized.context.perform(resizedJob)).error, /changed/);
    const capture = harness();
    const captured = await capture.context.capture({request: "capture"});
    assert.equal(captured.doc.depth, 16);
    assert.equal(capture.allocated(), 0);
    assert.equal(capture.calls.filter(item => item[0] === "upload").length, 2);
    assert.equal(capture.calls.find(item => item[0] === "upload")[2], 4 * 3 * 3 * 2);
    console.log("Photoshop connector host-free tests passed: validation, full-range capture, original target, nested ordering, masks, rollback, cancellation, stale/closed/resized source, no replay, buffer disposal.");
}
test().catch(error => { console.error(error); process.exitCode = 1; });
