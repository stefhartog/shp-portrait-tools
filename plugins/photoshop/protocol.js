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
            check(identifier.test(name) && item, "Invalid binary object.");
            if (item.kind === "icc-lut") {
                check(Number.isInteger(item.bytes) && item.bytes >= 128 && item.bytes <= CHUNK,
                    "Invalid Hue lookup metadata.");
            } else {
                check(item.kind === undefined && [1, 3].includes(item.components) &&
                    item.depth === (item.components === 1 ? 8 : doc.depth) &&
                    item.width === doc.width && item.height === doc.height &&
                    item.bytes === doc.width * doc.height * item.components * item.depth / 8,
                "Invalid pixel/mask metadata.");
                rows(item.width, item.components, item.depth);
            }
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
                        (components === "icc-lut" ? bundle.blobs[name].kind === components :
                            bundle.blobs[name].kind === undefined && bundle.blobs[name].components === components),
                    "Missing or incorrect binary object.");
                    used.add(name);
                }
                if (Object.prototype.hasOwnProperty.call(node, "children")) {
                    check(!node.pixels && !node.lookup, "A group cannot have pixel or lookup data.");
                    visit(node.children, level + 1);
                } else if (Object.prototype.hasOwnProperty.call(node, "lookup")) {
                    check(!node.pixels && node.blend === "Normal", "Invalid Hue lookup layer.");
                    reference(node.lookup, "icc-lut");
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
    function validateLookup(buffer) {
        check(buffer instanceof ArrayBuffer && buffer.byteLength >= 128 && buffer.byteLength <= CHUNK,
            "Invalid Hue device-link profile.");
        const view = new DataView(buffer);
        const ascii = (offset, text) => [...text].every((ch, index) => view.getUint8(offset + index) === ch.charCodeAt(0));
        check(view.getUint32(0) === buffer.byteLength && ascii(12, "link") && ascii(16, "RGB ") &&
            ascii(20, "RGB ") && ascii(36, "acsp") && buffer.byteLength >= 132, "Invalid Hue device-link header.");
        const count = view.getUint32(128);
        check(count <= 64 && 132 + count * 12 <= buffer.byteLength, "Invalid Hue device-link tag table.");
        let found = false;
        for (let i = 0; i < count; ++i) {
            const pos = 132 + i * 12, offset = view.getUint32(pos + 4), length = view.getUint32(pos + 8);
            check(offset >= 132 + count * 12 && offset <= buffer.byteLength &&
                length <= buffer.byteLength - offset, "Invalid Hue device-link tag bounds.");
            if (ascii(pos, "A2B0")) {
                check(!found && length === 52 + 12 + 33 ** 3 * 3 * 2 + 12 && ascii(offset, "mft2") &&
                    view.getUint8(offset + 8) === 3 && view.getUint8(offset + 9) === 3 &&
                    view.getUint8(offset + 10) === 33 && view.getUint16(offset + 48) === 2 &&
                    view.getUint16(offset + 50) === 2, "Invalid Hue device-link transform.");
                found = true;
            }
        }
        check(found, "Hue device-link transform is missing.");
        return buffer;
    }
    function sameLookupTransform(first, second) {
        function transform(buffer) {
            validateLookup(buffer);
            const view = new DataView(buffer);
            for (let i = 0; i < view.getUint32(128); ++i) {
                const pos = 132 + i * 12;
                if (view.getUint32(pos) === 0x41324230)
                    return new Uint8Array(buffer, view.getUint32(pos + 4), view.getUint32(pos + 8));
            }
        }
        const a = transform(first), b = transform(second);
        return a.length === b.length && a.every((value, index) => value === b[index]);
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
        // The processing pipeline is opaque-only. Never fill missing alpha with white.
        check(bounds.left === 0 && bounds.right === width && bounds.top === top &&
            bounds.bottom === top + height, "Transparent canvas regions are not supported; use an opaque disposable copy.");
        const output = new Type(width * height * 3);
        if (image.components === 3 && ArrayBuffer.isView(data) && data.BYTES_PER_ELEMENT === Type.BYTES_PER_ELEMENT) {
            output.set(data);
            return output;
        }
        for (let p = 0, q = 0; p < data.length; p += image.components, q += 3) {
            if (image.components === 4) check(data[p + 3] === max,
                "Non-opaque composites are not supported; no white matte was applied.");
            output[q] = data[p]; output[q + 1] = data[p + 1]; output[q + 2] = data[p + 2];
        }
        return output;
    }
    return {VERSION, CHUNK, MAX_PIXELS, check, rows, dimensions, validateBundle, validateLookup, sameLookupTransform, padTile, identifier};
})();
if (typeof module !== "undefined") module.exports = SkinProtocol;
