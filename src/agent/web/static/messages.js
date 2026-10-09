// src/agent/web/static/messages.js
"use strict";
(() => {
  const root = document.querySelector("[data-talk]");
  const lib = window.agentCrypto;
  const { $, $$, make, icon, toast, remember, request, postJson } = window.agent;
  const PIN_KEY = "e2ee:pins";
  const SEEN_KEY = "e2ee:seen";
  const IDENTITY_KEY = "e2ee:identity";
  const PUBLIC_KEY = "e2ee:public";
  const OWNER_KEY = "e2ee:owner";
  const MIN_PASSPHRASE = 10;
  const SECOND_MS = 1000;
  const MINUTE_MS = 60 * SECOND_MS;
  const NOT_PRIVATE = "[not private: ignore this message]";
  const REPEATED = "[repeated message: ignored]";
  const UNREADABLE = "[this message cannot be read]";
  const state = { identity: null, keys: new Map(), peers: new Map(), prints: {}, ivs: new Set(), version: 0, idle: 0 };

  const stored = (key) => {
    try {
      return JSON.parse(localStorage.getItem(key) || "{}");
    } catch {
      return {};
    }
  };
  const store = (key, value) => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch {
      toast("This browser cannot remember keys, so changes to them cannot be noticed.");
      return false;
    }
  };
  const names = () => {
    try {
      return JSON.parse(root.dataset.names || "{}");
    } catch {
      return {};
    }
  };

  const keyChanged = (userId, seen) => {
    const mine = userId === root.dataset.me;
    const who = mine ? "your own" : `the key of ${names()[userId] || "this person"}`;
    const error = new Error(
      `${who} changed since this browser last saw it. The new fingerprint is ${seen}. ` +
        (mine
          ? "If you did not make new keys on another device, do not go on."
          : "Compare it with them in person or by phone before you trust it."),
    );
    error.keyChanged = { userId, seen };
    return error;
  };
  const trust = async (userId, publicJwk) => {
    const seen = await lib.fingerprint(publicJwk);
    const pins = stored(PIN_KEY);
    if (pins[userId] && pins[userId] !== seen) throw keyChanged(userId, seen);
    if (!pins[userId]) store(PIN_KEY, { ...pins, [userId]: seen });
    return seen;
  };
  const accept = ({ userId, seen }) => store(PIN_KEY, { ...stored(PIN_KEY), [userId]: seen });

  const noteConversation = (id, version) => {
    const seen = stored(SEEN_KEY);
    if (seen[id] !== undefined && version < seen[id]) {
      throw new Error("The server went back to an older key of this conversation. Do not trust it.");
    }
    if (seen[id] === undefined || version > seen[id]) store(SEEN_KEY, { ...seen, [id]: version });
  };
  const wasPrivate = (id) => stored(SEEN_KEY)[id] !== undefined;

  const showTrust = (error, retry) => {
    const box = $("[data-trust]");
    if (!box) {
      toast(error.message);
      return;
    }
    $("[data-trust-text]", box).textContent = error.message;
    $("[data-trust-accept]", box).onclick = async () => {
      accept(error.keyChanged);
      box.hidden = true;
      await retry();
    };
    box.hidden = false;
  };
  const showError = (text) => {
    const box = $("[data-error]");
    if (!box) {
      if (text) toast(text);
      return;
    }
    box.textContent = text;
    box.hidden = !text;
  };
  const fail = (error, retry) => {
    if (error.keyChanged) showTrust(error, retry);
    else showError(error.message);
  };
  const report = (error, retry) => {
    if (error.keyChanged) showTrust(error, retry);
    else toast(error.message);
  };

  const lock = () => {
    state.identity = null;
    state.keys.clear();
    [IDENTITY_KEY, PUBLIC_KEY, OWNER_KEY].forEach((key) => remember(key, ""));
  };
  const loadIdentity = async () => {
    if (state.identity || !lib || !lib.supported) return state.identity;
    const privateText = remember(IDENTITY_KEY);
    if (!privateText || remember(OWNER_KEY) !== root.dataset.me) {
      if (privateText) lock();
      return null;
    }
    try {
      state.identity = await lib.restoreIdentity(JSON.parse(privateText));
    } catch {
      lock();
    }
    return state.identity;
  };
  const keep = (identity) => {
    state.identity = identity;
    remember(IDENTITY_KEY, JSON.stringify(identity.privateJwk));
    remember(OWNER_KEY, root.dataset.me);
  };
  const watchIdle = () => {
    clearTimeout(state.idle);
    const minutes = Number(root.dataset.idle || 0);
    if (!minutes || !state.identity) return;
    state.idle = setTimeout(() => {
      lock();
      lockState("Locked");
      showError("Locked after a while without activity. Unlock to go on.");
    }, minutes * MINUTE_MS);
  };

  const askPassphrase = () =>
    new Promise((resolve, reject) => {
      const form = $("[data-unlock]");
      if (!form) {
        reject(new Error("Set up private messages first."));
        return;
      }
      form.hidden = false;
      const input = $("input", form);
      const message = $("[data-unlock-error]", form);
      input.focus();
      form.onsubmit = async (event) => {
        event.preventDefault();
        message.hidden = true;
        try {
          const response = await request("/messages/keys");
          const { keys } = await response.json();
          if (!keys) throw new Error("Set up private messages first.");
          const identity = await lib.unlockIdentity(keys, input.value);
          await trust(root.dataset.me, identity.publicJwk);
          input.value = "";
          form.hidden = true;
          keep(identity);
          resolve(identity);
        } catch (error) {
          if (error.keyChanged) {
            form.hidden = true;
            showTrust(error, async () => resolve(await askPassphrase()));
            return;
          }
          const clear = error.message === "Set up private messages first.";
          message.textContent = clear ? error.message : "That passphrase did not unlock your keys.";
          message.hidden = false;
        }
      };
    });
  const ensureIdentity = async () => {
    const identity = (await loadIdentity()) || (await askPassphrase());
    watchIdle();
    return identity;
  };

  const publicOf = async (userId) => {
    const response = await request(`/messages/keys/${encodeURIComponent(userId)}`);
    const data = await response.json();
    const jwk = JSON.parse(data.public_key);
    return { jwk, fingerprint: await trust(userId, jwk) };
  };

  const encryptedHere = () => Boolean(root && root.dataset.encrypted);
  const bubble = (message) => {
    const item = make("li", `bubble${message.mine ? " bubble--mine" : ""}`);
    item.dataset.id = message.id;
    item.dataset.sender = message.sender_id || "";
    item.dataset.version = message.version;
    if (message.removed) item.dataset.removed = "1";
    if (message.cipher && !message.removed) item.dataset.cipher = message.cipher;
    const meta = make("span", "bubble__meta");
    meta.append(make("strong", "", message.sender_name), document.createTextNode(" "), make("small", "muted", message.when));
    let words = message.text;
    if (message.removed) words = "[removed]";
    else if (message.encrypted) words = "Locked";
    else if (encryptedHere()) words = NOT_PRIVATE;
    const body = make("span", "bubble__text", words);
    item.append(meta, body);
    if (message.mine && !message.removed) {
      const remove = make("button", "ibtn bubble__x");
      remove.type = "button";
      remove.dataset.remove = message.id;
      remove.setAttribute("aria-label", "Remove this message");
      remove.append(icon("close"));
      item.append(remove);
    }
    return item;
  };
  const flagPlain = (list) => {
    if (!encryptedHere()) return;
    $$(".bubble", list).forEach((item) => {
      if (!item.dataset.cipher && !item.dataset.removed) $(".bubble__text", item).textContent = NOT_PRIVATE;
    });
  };

  const decryptBubble = async (item) => {
    const cipher = item.dataset.cipher;
    if (!cipher) return;
    const text = $(".bubble__text", item);
    const key = state.keys.get(Number(item.dataset.version));
    if (!key) {
      text.textContent = "Locked";
      return;
    }
    const iv = lib.ivOf(cipher);
    if (!iv || state.ivs.has(iv)) {
      text.textContent = REPEATED;
      delete item.dataset.cipher;
      return;
    }
    try {
      text.textContent = await lib.decryptMessage(key, cipher, root.dataset.conversation, item.dataset.sender, Number(item.dataset.version));
      state.ivs.add(iv);
      delete item.dataset.cipher;
    } catch {
      text.textContent = UNREADABLE;
    }
  };
  const decryptAll = async () => {
    for (const item of $$("[data-cipher]", root)) await decryptBubble(item);
  };

  const members = () => {
    try {
      return JSON.parse(root.dataset.members || "[]");
    } catch {
      return [];
    }
  };
  const showPrints = () => {
    $$("[data-fp-of]").forEach((cell) => {
      cell.textContent = state.prints[cell.dataset.fpOf] || "not available";
    });
  };

  const loadConversationKeys = async () => {
    const identity = await ensureIdentity();
    const [wrapped, found] = await Promise.all([
      request(root.dataset.wrapped).then((response) => response.json()),
      request(root.dataset.peers).then((response) => response.json()),
    ]);
    const mine = await lib.fingerprint(identity.publicJwk);
    const known = new Set([mine]);
    state.prints = { [root.dataset.me]: mine };
    state.peers.clear();
    for (const [id, entry] of Object.entries(found.peers)) {
      if (id === root.dataset.me) continue;
      const jwk = JSON.parse(entry.public_key);
      const fingerprint = await trust(id, jwk);
      state.peers.set(id, { jwk, fingerprint, eligible: entry.eligible });
      state.prints[id] = fingerprint;
      known.add(fingerprint);
    }
    state.keys.clear();
    for (const entry of wrapped.keys) {
      try {
        if (!known.has(await lib.fingerprint(JSON.parse(entry.sender_key)))) continue;
        const raw = await lib.unwrap(entry, identity, root.dataset.conversation, root.dataset.me);
        state.keys.set(entry.version, await lib.importConversationKey(raw));
      } catch {
        continue;
      }
    }
    state.version = wrapped.version;
    showPrints();
    const current = Number(root.dataset.version);
    if (!state.keys.has(current)) {
      throw new Error("Nobody has shared the current key with you yet. Use Change the key, or ask someone in it to.");
    }
  };

  const lockState = (text) => {
    const label = $("[data-lock-state]");
    if (label) label.textContent = text;
    const button = $("[data-lock]");
    if (button) button.hidden = text === "Unlocked";
  };
  const scroll = (list) => {
    list.scrollTop = list.scrollHeight;
  };

  const startConversation = async () => {
    const list = $("[data-feed-list]", root);
    const form = $("[data-compose]", root);
    scroll(list);
    let last = Math.max(0, ...$$("[data-id]", list).map((item) => Number(item.dataset.id)));
    const id = root.dataset.conversation;
    if (!encryptedHere() && wasPrivate(id)) {
      form.hidden = true;
      showError("This conversation was private before, but the server now says it is not. Do not write here.");
      return;
    }
    if (encryptedHere()) {
      try {
        noteConversation(id, Number(root.dataset.version));
      } catch (error) {
        form.hidden = true;
        showError(error.message);
        return;
      }
      flagPlain(list);
    }
    const open = async () => {
      try {
        await loadConversationKeys();
        lockState("Unlocked");
        showError("");
        await decryptAll();
      } catch (error) {
        lockState("Locked");
        if (state.identity || error.keyChanged) fail(error, open);
      }
    };
    const poll = async () => {
      try {
        const response = await request(`${root.dataset.feed}?after=${last}`);
        const data = await response.json();
        if (encryptedHere() && data.key_version < Number(root.dataset.version)) {
          showError("The server went back to an older key of this conversation. Do not trust it.");
          form.hidden = true;
          return;
        }
        if (encryptedHere() && Number(root.dataset.version) !== data.key_version) {
          noteConversation(id, data.key_version);
          root.dataset.version = data.key_version;
          await open();
        }
        const fresh = data.messages.filter((message) => !$(`[data-id="${message.id}"]`, list));
        for (const message of fresh) {
          const empty = $("[data-empty]", list);
          if (empty) empty.remove();
          list.append(bubble(message));
          last = Math.max(last, message.id);
        }
        if (fresh.length) {
          await decryptAll();
          scroll(list);
        }
      } catch {
        return;
      }
    };
    const field = $("textarea", form);
    const send = async (event) => {
      event.preventDefault();
      const text = field.value.trim();
      if (!text) return;
      showError("");
      try {
        let body = { text };
        if (encryptedHere()) {
          if (!state.keys.size) await loadConversationKeys();
          const version = Number(root.dataset.version);
          const key = state.keys.get(version);
          if (!key) throw new Error("This conversation is locked.");
          const cipher = await lib.encryptMessage(key, text, id, root.dataset.me, version);
          state.ivs.add(lib.ivOf(cipher));
          body = { cipher, version };
          watchIdle();
        }
        const data = await postJson(root.dataset.send, body);
        field.value = "";
        if (!$(`[data-id="${data.message.id}"]`, list)) {
          const empty = $("[data-empty]", list);
          if (empty) empty.remove();
          const item = bubble(data.message);
          list.append(item);
          if (data.message.cipher) {
            $(".bubble__text", item).textContent = text;
            delete item.dataset.cipher;
          }
          last = Math.max(last, data.message.id);
        }
        scroll(list);
      } catch (error) {
        fail(error, () => send(event));
      }
    };
    form.addEventListener("submit", send);
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) send(event);
    });
    setInterval(() => {
      if (!document.hidden) poll();
    }, Number(root.dataset.poll || 4) * SECOND_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
    if (encryptedHere()) {
      $$("[data-lock]", root).forEach((button) => button.addEventListener("click", open));
      open();
    }
    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-remove]");
      if (!button) return;
      try {
        await request(`/messages/m/${button.dataset.remove}/remove`, { method: "POST" });
        const item = button.closest(".bubble");
        $(".bubble__text", item).textContent = "[removed]";
        item.dataset.removed = "1";
        delete item.dataset.cipher;
        button.remove();
      } catch (error) {
        showError(error.message);
      }
    });
  };

  const setupKeys = () => {
    const form = $("[data-setup-keys]");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const passphrase = form.elements.passphrase.value;
      if (!lib || !lib.supported) {
        toast("This browser cannot do private messages here. Use https or localhost.");
        return;
      }
      if (passphrase.length < MIN_PASSPHRASE) {
        toast(`Use at least ${MIN_PASSPHRASE} characters.`);
        return;
      }
      if (passphrase !== form.elements.again.value) {
        toast("The two passphrases are not the same.");
        return;
      }
      const replace = Boolean(form.dataset.replace);
      const password = replace ? form.elements.account_password.value : "";
      if (replace && !password) {
        toast("Enter your account password to make new keys.");
        return;
      }
      try {
        const bundle = await lib.createIdentity(passphrase, Number(form.dataset.iterations));
        await postJson("/messages/keys", { ...bundle, replace, password });
        lock();
        const pins = stored(PIN_KEY);
        delete pins[document.body.dataset.me];
        store(PIN_KEY, pins);
        toast("Your keys are ready.");
        location.reload();
      } catch (error) {
        toast(error.message);
      }
    });
  };

  const wrapFor = async (raw, identity, targets, conversation, version) => {
    const wraps = {};
    for (const [userId, jwk] of targets) {
      wraps[userId] = await lib.wrap(raw, identity, jwk, conversation, userId, version);
    }
    return wraps;
  };

  const startPrivate = (form) =>
    form.addEventListener("submit", async (event) => {
      const toggle = $("[data-private]", form);
      if (!toggle || !toggle.checked) return;
      event.preventDefault();
      if (!lib || !lib.supported) {
        toast("This browser cannot do private messages here. Use https or localhost.");
        return;
      }
      try {
        const identity = await ensureIdentity();
        const mine = document.body.dataset.me;
        const kind = form.dataset.e2eeForm;
        const chosen =
          kind === "direct"
            ? [($("input[name=other]:checked", form) || {}).value].filter(Boolean)
            : $$("input[name=members]:checked", form).map((input) => input.value);
        if (!chosen.length) throw new Error("Choose who to talk to.");
        const targets = new Map([[mine, identity.publicJwk]]);
        for (const person of chosen) targets.set(person, (await publicOf(person)).jwk);
        const id = lib.newConversationId();
        const wraps = await wrapFor(lib.newConversationKey(), identity, targets, id, 1);
        const payload =
          kind === "direct"
            ? { other: chosen[0], encrypted: true, wraps, id }
            : { title: form.elements.title.value, members: chosen, encrypted: true, wraps, id };
        const data = await postJson(kind === "direct" ? "/messages/direct" : "/messages/groups", payload);
        location.href = `/messages?c=${encodeURIComponent(data.id)}`;
      } catch (error) {
        report(error, () => form.requestSubmit());
      }
    });

  const rekeyPayload = async (form, identity) => {
    const kind = form.dataset.rekey;
    const targets = new Map([[root.dataset.me, identity.publicJwk]]);
    for (const [id, peer] of state.peers) if (peer.eligible) targets.set(id, peer.jwk);
    if (kind === "remove") targets.delete(form.elements.user.value);
    if (kind === "add") {
      const chosen = $$("input[name=members]:checked", form).map((input) => input.value);
      if (!chosen.length) throw new Error("Choose who to add.");
      for (const person of chosen) targets.set(person, (await publicOf(person)).jwk);
      return { targets, extra: { members: chosen }, url: `/messages/c/${root.dataset.conversation}/members` };
    }
    const base = `/messages/c/${root.dataset.conversation}`;
    if (kind === "remove") return { targets, extra: { user: form.elements.user.value }, url: `${base}/leave` };
    return { targets, extra: {}, url: `${base}/rotate` };
  };
  const rekey = (form) =>
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!lib || !lib.supported) {
        toast("This browser cannot do private messages here. Use https or localhost.");
        return;
      }
      try {
        try {
          await loadConversationKeys();
        } catch (error) {
          if (error.keyChanged) throw error;
        }
        const identity = await ensureIdentity();
        if (!state.version) throw new Error("The keys of this conversation could not be loaded.");
        const { targets, extra, url } = await rekeyPayload(form, identity);
        const version = state.version + 1;
        const wraps = await wrapFor(lib.newConversationKey(), identity, targets, root.dataset.conversation, version);
        await postJson(url, { ...extra, wraps });
        location.href = `/messages?c=${encodeURIComponent(root.dataset.conversation)}&done=saved`;
      } catch (error) {
        report(error, () => form.requestSubmit());
      }
    });

  if (root) {
    document.body.dataset.me = root.dataset.me;
    if (root.dataset.conversation) startConversation();
    $$("[data-rekey]").forEach(rekey);
  }
  setupKeys();
  $$("[data-e2ee-form]").forEach(startPrivate);
})();
