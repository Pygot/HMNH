// src/agent/web/static/e2ee.js
"use strict";
(() => {
  const supported = Boolean(window.crypto && window.crypto.subtle);
  if (!supported) {
    window.agentCrypto = { supported };
    return;
  }
  const subtle = crypto.subtle;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const CURVE = { name: "ECDH", namedCurve: "P-256" };
  const IV_BYTES = 12;
  const KEY_BYTES = 32;
  const SALT_BYTES = 16;
  const ID_BYTES = 9;
  const MIN_ITERATIONS = 600000;
  const FINGERPRINT_CHARS = 32;
  const GROUP = 4;
  const CHUNK = 8192;

  const b64 = (bytes) => {
    const view = new Uint8Array(bytes);
    let text = "";
    for (let start = 0; start < view.length; start += CHUNK) {
      text += String.fromCharCode(...view.subarray(start, start + CHUNK));
    }
    return btoa(text);
  };
  const unb64 = (text) => Uint8Array.from(atob(text), (char) => char.charCodeAt(0));
  const bytesOf = (text) => encoder.encode(text);
  const canonical = (jwk) => ({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y });
  const same = (first, second) => JSON.stringify(canonical(first)) === JSON.stringify(canonical(second));

  const passphraseKey = async (passphrase, salt, iterations) => {
    const base = await subtle.importKey("raw", bytesOf(passphrase.normalize("NFKC")), "PBKDF2", false, ["deriveKey"]);
    return subtle.deriveKey(
      { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
      base,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
  };
  const seal = async (key, bytes, context) => {
    const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
    const sealed = await subtle.encrypt({ name: "AES-GCM", iv, additionalData: bytesOf(context) }, key, bytes);
    return { iv: b64(iv), ct: b64(sealed) };
  };
  const open = async (key, box, context) =>
    new Uint8Array(
      await subtle.decrypt({ name: "AES-GCM", iv: unb64(box.iv), additionalData: bytesOf(context) }, key, unb64(box.ct)),
    );

  const fingerprint = async (publicJwk) => {
    const digest = await subtle.digest("SHA-256", bytesOf(JSON.stringify(canonical(publicJwk))));
    const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    return hex.slice(0, FINGERPRINT_CHARS).match(new RegExp(`.{${GROUP}}`, "g")).join(" ");
  };

  const createIdentity = async (passphrase, iterations) => {
    const rounds = Math.max(Number(iterations) || 0, MIN_ITERATIONS);
    const pair = await subtle.generateKey(CURVE, true, ["deriveBits"]);
    const publicJwk = canonical(await subtle.exportKey("jwk", pair.publicKey));
    const privateJwk = await subtle.exportKey("jwk", pair.privateKey);
    const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
    const wrapper = await passphraseKey(passphrase, salt, rounds);
    const box = await seal(wrapper, bytesOf(JSON.stringify(privateJwk)), "identity");
    return {
      public_key: JSON.stringify(publicJwk),
      wrapped_private: JSON.stringify(box),
      salt: b64(salt),
      iterations: rounds,
    };
  };

  const restoreIdentity = async (privateJwk) => ({
    privateKey: await subtle.importKey("jwk", privateJwk, CURVE, false, ["deriveBits"]),
    privateJwk,
    publicJwk: canonical(privateJwk),
  });

  const unlockIdentity = async (bundle, passphrase) => {
    const wrapper = await passphraseKey(passphrase, unb64(bundle.salt), bundle.iterations);
    const plain = await open(wrapper, JSON.parse(bundle.wrapped_private), "identity");
    const identity = await restoreIdentity(JSON.parse(decoder.decode(plain)));
    if (!same(identity.publicJwk, JSON.parse(bundle.public_key))) {
      throw new Error("The public key the server shows for you is not the one your private key belongs to.");
    }
    return identity;
  };

  const sharedKey = async (privateKey, theirPublicJwk, context) => {
    const theirs = await subtle.importKey("jwk", canonical(theirPublicJwk), CURVE, false, []);
    const bits = await subtle.deriveBits({ name: "ECDH", public: theirs }, privateKey, 256);
    const material = await subtle.importKey("raw", bits, "HKDF", false, ["deriveKey"]);
    return subtle.deriveKey(
      { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(KEY_BYTES), info: bytesOf(context) },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
  };

  const newConversationKey = () => crypto.getRandomValues(new Uint8Array(KEY_BYTES));
  const newConversationId = () => b64(crypto.getRandomValues(new Uint8Array(ID_BYTES))).replace(/\+/g, "-").replace(/\//g, "_");
  const wrapContext = (conversation, userId, version) => `wrap:${conversation}:${userId}:${version}`;

  const wrap = async (raw, identity, theirPublicJwk, conversation, userId, version) => {
    const context = wrapContext(conversation, userId, version);
    const wrapper = await sharedKey(identity.privateKey, theirPublicJwk, context);
    const box = await seal(wrapper, raw, context);
    return { wrapped: JSON.stringify(box), sender_key: JSON.stringify(identity.publicJwk) };
  };

  const unwrap = async (entry, identity, conversation, userId) => {
    const context = wrapContext(conversation, userId, entry.version);
    const wrapper = await sharedKey(identity.privateKey, JSON.parse(entry.sender_key), context);
    return open(wrapper, JSON.parse(entry.wrapped), context);
  };

  const importConversationKey = (raw) => subtle.importKey("raw", raw, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  const messageContext = (conversation, sender, version) => `message:${conversation}:${sender}:${version}`;

  const encryptMessage = async (key, text, conversation, sender, version) =>
    JSON.stringify(await seal(key, bytesOf(text), messageContext(conversation, sender, version)));

  const decryptMessage = async (key, cipher, conversation, sender, version) =>
    decoder.decode(await open(key, JSON.parse(cipher), messageContext(conversation, sender, version)));

  const ivOf = (cipher) => {
    try {
      return String(JSON.parse(cipher).iv || "");
    } catch {
      return "";
    }
  };

  window.agentCrypto = {
    supported,
    createIdentity,
    unlockIdentity,
    restoreIdentity,
    fingerprint,
    newConversationKey,
    newConversationId,
    wrap,
    unwrap,
    importConversationKey,
    encryptMessage,
    decryptMessage,
    ivOf,
    canonical,
  };
})();
