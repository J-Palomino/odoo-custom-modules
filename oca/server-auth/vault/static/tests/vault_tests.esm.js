// © 2021 Florian Kantelberg - initOS GmbH
// License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import {describe, test, expect, beforeAll} from "@odoo/hoot";
import {makeMockEnv} from "@web/../tests/web_test_helpers";

let utils = null;

describe("vault", () => {
    beforeAll(async () => {
        const env = await makeMockEnv();
        utils = env.services.vault_utils;
        utils.askpass = async function () {
            return {
                password: "test",
                keyfile: "",
            };
        };
    });

    function is_keypair(keys) {
        expect(keys.publicKey instanceof CryptoKey).toBe(true);
        expect(keys.publicKey.type).toBe("public");
        expect(keys.privateKey instanceof CryptoKey).toBe(true);
        expect(keys.privateKey.type).toBe("private");
    }

    test("vault: Test conversion utils", async () => {
        let text = "hello world";
        let buf = utils.fromBinary(text);
        expect(buf instanceof ArrayBuffer).toBe(true);
        expect(utils.toBinary(buf)).toBe(text);
        expect(utils.toBinary(false)).toBe("");

        text = "ImhlbGxvIHdvcmxkIg==";
        buf = utils.fromBase64(text);
        expect(buf instanceof ArrayBuffer).toBe(true);
        expect(utils.toBase64(buf)).toBe(text);
        expect(utils.toBase64(false)).toBe("");

        expect(utils.capitalize("hello world")).toBe("Hello World");
    });

    test("vault: Test generation utils", async () => {
        let data = utils.generate_bytes(5);
        expect(data instanceof Uint8Array).toBe(true);
        expect(data.length).toBe(5);
        data = utils.generate_bytes(10);
        expect(data.length).toBe(10);

        data = utils.generate_iv_base64();
        expect(typeof data).toBe("string");
        expect(data).not.toBe(utils.generate_iv_base64());

        data = await utils.generate_key();
        expect(data instanceof CryptoKey).toBe(true);

        data = await utils.generate_key_pair();
        is_keypair(data);

        data = utils.generate_secret(10, "01");
        expect(data.length).toBe(10);
        let valid = true;
        for (const c of data) if ("01".indexOf(c) < 0) valid = false;
        expect(valid).toBe(true);
    });

    test("vault: Test asymmetric encryption", async () => {
        const text = "hello world";
        const key = await utils.generate_key_pair();

        const crypted = await utils.asym_encrypt(key.publicKey, text);
        expect(typeof crypted).toBe("string");
        expect(await utils.asym_decrypt(key.privateKey, crypted)).toBe(text);
    });

    test("vault: Test symmetric encryption", async () => {
        const text = "hello world";
        const key = await utils.generate_key();
        const iv = utils.generate_iv_base64();

        const crypted = await utils.sym_encrypt(key, text, iv);
        expect(typeof crypted).toBe("string");
        expect(await utils.sym_decrypt(key, crypted, iv)).toBe(text);
    });

    test("vault: Test import/export", async () => {
        const key = await utils.generate_key_pair();
        let exported = await utils.export_public_key(key.publicKey);
        let tmp = await utils.load_public_key(exported);
        expect(tmp).toEqual(key.publicKey);

        const iv = utils.generate_bytes(10);
        const salt = utils.generate_bytes(10);
        const wrapper = await utils.derive_key("test", salt, 4000);
        exported = await utils.export_private_key(key.privateKey, wrapper, iv);
        tmp = await utils.load_private_key(exported, wrapper, utils.toBase64(iv));
        expect(tmp).toEqual(key.privateKey);

        const master_key = await utils.generate_key();
        exported = await utils.wrap(master_key, key.publicKey);
        tmp = await utils.unwrap(exported, key.privateKey);
        expect(tmp).toEqual(master_key);
    });

    test("vault: Test vault class", async () => {
        const env = await makeMockEnv();
        var vault = env.services.vault;

        await vault._initialize_keys();
        is_keypair(vault.keys);

        vault.keys = undefined;
        await vault._import_from_store();
        is_keypair(vault.keys);

        vault.keys = undefined;
        await vault._import_from_database();
        is_keypair(vault.keys);
    });

    test("vault: Importer/exporter", async () => {
        // The exporter won't skip empty keys
        const child = {
            uuid: "42a",
            note: "test note child",
            name: "test child",
            url: "child.example.org",
            fields: [],
            files: [],
            childs: [],
        };

        const data = {
            type: "raw",
            data: [
                child,
                {
                    uuid: "42",
                    note: "test note",
                    name: "test name",
                    url: "test.example.org",
                    fields: [
                        {name: "a", value: "Hello World"},
                        {name: "secret", value: "dlrow olleh"},
                    ],
                    files: [],
                    childs: [child, child],
                },
                child,
            ],
        };

        const env = await makeMockEnv();
        var Exporter = env.services.vault_export;
        var Importer = env.services.vault_import;
        var vault = env.services.vault;

        await vault._initialize_keys();

        const master_key = await utils.generate_key();
        const importer = new Importer();
        const imported = await importer.import(
            master_key,
            "test.json",
            JSON.stringify(data)
        );

        const exporter = new Exporter();
        const exported = await exporter.export(
            master_key,
            "test.json",
            JSON.stringify(imported)
        );
        expect(exported.type).toBe("encrypted");

        const pass = await utils.derive_key(
            "test",
            utils.fromBase64(exported.salt),
            exported.iterations
        );

        const tmp = JSON.parse(
            await utils.sym_decrypt(pass, exported.data, exported.iv)
        );
        expect(tmp).toEqual(data.data);
    });
});
