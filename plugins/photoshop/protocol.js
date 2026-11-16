/* Pure validation and alignment helpers, shared with the host-free connector tests. */
"use strict";
const SkinProtocol = (() => {
    const VERSION = 1, CHUNK = 2 * 1024 * 1024, MAX_PIXELS = 40000000;
    const identifier = /^[a-zA-Z0-9_-]{1,80}$/;
    const blends = new Set(["Normal", "Soft Light", "Linear Light", "Pass Through"]);
    function check(condition, message) { if (!condition) throw new Error(message); }
    function dimensions(width, height) {
        check(Number.isInteger(width) && Number.isInteger(height) && width > 0 && height > 0 &&
            width * height <= MAX_PIXELS, "Invalid canvas; maximum 40 megapixels.");
    }
    function rows(width, components, depth) {
        const row = width * components * depth / 8;
        check(Number.isInteger(row) && row > 0 && row <= CHUNK, "Canvas row exceeds transfer limit.");
        return Math.max(1, Math.floor(CHUNK / row));
    }
    function validateBundle(bundle, session) {
        const doc = bundle.doc;
        check(doc && doc.session === session && doc.protocol === VERSION &&
            Number.isInteger(doc.index) && typeof doc.marker === "string" &&
            typeof doc.capture === "string" && typeof doc.profile === "string" &&
            doc.mode === "RGB" && [8, 16].includes(doc.depth) && doc.alpha === "opaque",
        "Invalid or expired Photoshop source.");
        dimensions(doc.width, doc.height);
        check(bundle.blobs && typeof bundle.blobs === "object" && !Array.isArray(bundle.blobs), "Missing binary objects.");
        const names = Object.keys(bundle.blobs), used = new Set();
        check(names.length > 0 && names.length <= 64, "Too many binary objects.");
        let total = 0, count = 0;
        for (const name of names) {
            const item = bundle.blobs[name];
            check(identifier.test(name) && item && [1, 3].includes(item.components) &&
                item.depth === (item.components === 1 ? 8 : doc.depth) &&
                item.width === doc.width && item.height === doc.height &&
                item.bytes === doc.width * doc.height * item.components * item.depth / 8,
            "Invalid pixel/mask metadata.");
            rows(item.width, item.components, item.depth);
            total += item.bytes;
        }
        check(total <= 2 * 1024 ** 3, "Layer bundle exceeds 2 GiB limit.");
        function visit(nodes, level) {
            check(Array.isArray(nodes) && nodes.length > 0 && level <= 8, "Invalid layer tree.");
            for (const node of nodes) {
                check(++count <= 32 && node && typeof node.name === "string" && node.name.length > 0 &&
                    node.name.length <= 120 && blends.has(node.blend) &&
                    Number.isFinite(node.opacity) && node.opacity >= 0 && node.opacity <= 100,
                "Invalid layer metadata.");
                function reference(name, components) {
                    check(typeof name === "string" && Object.prototype.hasOwnProperty.call(bundle.blobs, name) &&
                        bundle.blobs[name].components === components, "Missing or incorrect binary object.");
                    used.add(name);
                }
                if (Object.prototype.hasOwnProperty.call(node, "children")) {
                    check(!node.pixels, "A group cannot have pixel data.");
                    visit(node.children, level + 1);
                } else {
                    check(node.blend !== "Pass Through", "Pass Through is only valid on groups.");
                    reference(node.pixels, 3);
                }
                if (node.mask !== undefined) reference(node.mask, 1);
            }
        }
        visit(bundle.layers, 0);
        check(used.size === names.length, "Unreferenced binary objects.");
        return bundle;
    }
    function padTile(image, data, bounds, width, top, height, depth) {
        check(image.componentSize === depth && image.colorSpace === "RGB" &&
            [3, 4].includes(image.components), "Unexpected Photoshop pixel format.");
        check(Number.isInteger(bounds.left) && Number.isInteger(bounds.top) &&
            Number.isInteger(bounds.right) && Number.isInteger(bounds.bottom) &&
            bounds.left >= 0 && bounds.right <= width && bounds.top >= top &&
            bounds.bottom <= top + height && bounds.right - bounds.left === image.width &&
            bounds.bottom - bounds.top === image.height &&
            data.length === image.width * image.height * image.components, "Invalid trimmed pixel bounds.");
        const Type = depth === 16 ? Uint16Array : Uint8Array, max = depth === 16 ? 65535 : 255;
        const output = new Type(width * height * 3);
        // The processing pipeline is opaque-only. Never fill missing alpha with white.
        check(bounds.left === 0 && bounds.right === width && bounds.top === top &&
            bounds.bottom === top + height, "Transparent canvas regions are not supported; use an opaque disposable copy.");
        for (let p = 0, q = 0; p < data.length; p += image.components, q += 3) {
            if (image.components === 4) check(data[p + 3] === max,
                "Non-opaque composites are not supported; no white matte was applied.");
            output[q] = data[p]; output[q + 1] = data[p + 1]; output[q + 2] = data[p + 2];
        }
        return output;
    }
    return {VERSION, CHUNK, MAX_PIXELS, check, rows, dimensions, validateBundle, padTile, identifier};
})();
if (typeof module !== "undefined") module.exports = SkinProtocol;
