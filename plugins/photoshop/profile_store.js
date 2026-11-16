"use strict";

const MAX_PROFILE = 4 * 1024 * 1024;
const MAX_ENTRIES = 64;
const META = /^icc-entry-([a-zA-Z0-9_-]+)\.json$/;
const DATA = /^icc-data-[a-zA-Z0-9_-]+\.icc$/;

function check(condition, message) {
    if (!condition) throw new Error(message);
}

class ProfileStore {
    constructor(fs, binary) {
        this.fs = fs;
        this.binary = binary;
        this.entries = [];
    }

    async initialize() {
        this.folder = await this.fs.getDataFolder();
        const files = await this.folder.getEntries();
        const byName = new Map(files.map(file => [file.name, file]));
        for (const file of files) {
            const match = META.exec(file.name);
            if (!match) continue;
            const metadata = await file.getMetadata();
            check(metadata.size <= 8192, "Cached ICC metadata is too large; remove the invalid entry in plugin storage.");
            const value = JSON.parse(await file.read());
            check(value && typeof value.label === "string" && value.label.length > 0 && value.label.length <= 512 &&
                (value.profile === null || (typeof value.profile === "string" && value.profile.length > 0 && value.profile.length <= 512)) &&
                (value.legacySource === undefined || typeof value.legacySource === "boolean") &&
                (value.file === "source-profile.icc" || DATA.test(value.file)) && byName.has(value.file),
                "Cached ICC metadata is invalid or its file is missing; select the profile again.");
            this.entries.push({id: match[1], label: value.label, profile: value.profile, file: value.file,
                legacySource: value.legacySource === true});
        }
        check(this.entries.length <= MAX_ENTRIES, "Too many saved ICC profiles; remove unused plugin cache entries.");
        if (byName.has("source-profile.icc") && !this.entries.some(entry => entry.file === "source-profile.icc")) {
            await this.addMetadata({id: "legacy", label: "Previously selected ICC", profile: null, file: "source-profile.icc"});
        }
        return this.entries;
    }

    async read(entry) {
        const file = await this.folder.getEntry(entry.file);
        const metadata = await file.getMetadata();
        check(metadata.size >= 128 && metadata.size <= MAX_PROFILE, "Saved ICC is invalid; select the exact profile again.");
        const bytes = await file.read({format: this.binary});
        check(bytes instanceof ArrayBuffer && bytes.byteLength === metadata.size, "Saved ICC could not be read completely.");
        return bytes;
    }

    async addMetadata(entry) {
        check(this.entries.length < MAX_ENTRIES, "The saved ICC profile limit has been reached.");
        const metadata = await this.folder.createFile(`icc-entry-${entry.id}.json`, {overwrite: false});
        await metadata.write(JSON.stringify({label: entry.label, profile: entry.profile, file: entry.file,
            legacySource: entry.legacySource === true}));
        this.entries.push(entry);
        return entry;
    }

    async save(bytes, label, profile, sourceEntry = null) {
        check(bytes instanceof ArrayBuffer && bytes.byteLength >= 128 && bytes.byteLength <= MAX_PROFILE,
            "ICC file must be 128 bytes to 4 MiB.");
        check(typeof label === "string" && label.length > 0 && label.length <= 512 &&
            typeof profile === "string" && profile.length > 0 && profile.length <= 512,
            "ICC filename or document profile name is invalid.");
        for (const entry of this.entries.filter(item => item.profile === profile)) {
            const saved = new Uint8Array(await this.read(entry)), candidate = new Uint8Array(bytes);
            if (saved.length === candidate.length && saved.every((value, index) => value === candidate[index])) return entry;
        }
        check(this.entries.length < MAX_ENTRIES, "The saved ICC profile limit has been reached.");
        const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        if (sourceEntry) {
            check(this.entries.includes(sourceEntry) && sourceEntry.profile === null && sourceEntry.file === "source-profile.icc",
                "Previous ICC entry is no longer available.");
        }
        const filename = `icc-data-${id}.icc`;
        const file = await this.folder.createFile(filename, {overwrite: false});
        await file.write(bytes, {format: this.binary});
        return this.addMetadata({id, label, profile, file: filename, legacySource: Boolean(sourceEntry)});
    }

    matching(profile) {
        return this.entries.filter(entry => entry.profile === profile);
    }
}

module.exports = ProfileStore;
