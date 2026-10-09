// src/agent/web/static/app.js
"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const page = document.body;
  const toastBox = $(".toast");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SVG = "http://www.w3.org/2000/svg";
  const CONFIRM_MS = 3200;
  const PRIVATE_KEYS = ["e2ee:identity", "e2ee:public", "e2ee:owner"];
  let toastTimer = 0;
  let recording = null;
  let player = null;
  let pending = null;
  let ticket = 0;

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const icon = (name) => {
    const holder = document.createElementNS(SVG, "svg");
    holder.setAttribute("class", "i");
    holder.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG, "use");
    use.setAttribute("href", `#i-${name}`);
    holder.append(use);
    return holder;
  };
  const toast = (message) => {
    toastBox.textContent = message;
    toastBox.classList.add("is-shown");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.classList.remove("is-shown"), 3600);
  };
  const remember = (key, value) => {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      sessionStorage.setItem(key, value);
    } catch {
      return null;
    }
    return value;
  };
  const request = async (url, options = {}) => {
    const headers = { "X-CSRF-Token": page.dataset.csrf, ...(options.headers || {}) };
    const response = await fetch(url, { credentials: "same-origin", ...options, headers });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Something went wrong. Please try again.");
    }
    return response;
  };
  const postJson = async (url, body) => {
    const response = await request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return response.status === 204 ? null : response.json();
  };

  const selectBox = (box) => {
    const input = $("input", box);
    const button = $("button", box);
    const items = $$("li", box);
    const open = (state) => {
      box.classList.toggle("is-open", state);
      button.setAttribute("aria-expanded", state);
    };
    const choose = (item, close) => {
      items.forEach((other) => other.setAttribute("aria-selected", other === item));
      $("span", button).textContent = item.textContent;
      input.value = item.dataset.value;
      if (close) open(false);
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };
    button.addEventListener("click", () => open(!box.classList.contains("is-open")));
    items.forEach((item) => item.addEventListener("click", () => choose(item, true)));
    box.addEventListener("keydown", (event) => {
      const index = items.findIndex((item) => item.getAttribute("aria-selected") === "true");
      if (event.key === "Escape") open(false);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        choose(items[Math.min(items.length - 1, Math.max(0, index + step))], false);
      }
    });
  };

  const tabs = (root) => {
    const buttons = $$("[data-tab]", root);
    const panels = new Map($$("[data-panel]", root).map((panel) => [panel.dataset.panel, panel]));
    const key = `tab:${location.pathname}`;
    const show = (name, focus) => {
      buttons.forEach((button) => {
        const on = button.dataset.tab === name;
        button.setAttribute("aria-selected", on);
        button.tabIndex = on ? 0 : -1;
        if (on && focus) button.focus();
      });
      panels.forEach((panel, label) => (panel.hidden = label !== name));
      remember(key, name);
      document.dispatchEvent(new CustomEvent("tabs:show", { detail: { name, panel: panels.get(name) } }));
    };
    const target = () => {
      const id = decodeURIComponent(location.hash.slice(1));
      if (panels.has(id)) return id;
      const inside = id ? document.getElementById(id) : null;
      const owner = inside ? inside.closest("[data-panel]") : null;
      if (owner && panels.has(owner.dataset.panel)) return owner.dataset.panel;
      const saved = remember(key);
      return panels.has(saved) ? saved : buttons[0].dataset.tab;
    };
    buttons.forEach((button, index) => {
      button.addEventListener("click", () => show(button.dataset.tab, false));
      button.addEventListener("keydown", (event) => {
        const horizontal = root.classList.contains("tabs--side") ? ["ArrowUp", "ArrowDown"] : ["ArrowLeft", "ArrowRight"];
        const step = event.key === horizontal[1] ? 1 : event.key === horizontal[0] ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        show(buttons[(index + step + buttons.length) % buttons.length].dataset.tab, true);
      });
    });
    addEventListener("hashchange", () => show(target(), false));
    show(target(), false);
  };

  const railApi = { upsert: () => undefined };
  const rail = () => {
    const shell = $("[data-shell]");
    if (!shell) return;
    const toggles = $$("[data-rail-toggle]");
    const set = (open) => {
      shell.classList.toggle("rail-open", open);
      toggles.forEach((button) => button.setAttribute("aria-expanded", open));
    };
    toggles.forEach((button) => button.addEventListener("click", () => set(!shell.classList.contains("rail-open"))));
    document.addEventListener("keydown", (event) => event.key === "Escape" && set(false));
    $$(".rail a").forEach((link) => link.addEventListener("click", () => set(false)));

    const finder = $("[data-find]");
    const list = $("[data-threads]");
    if (!finder || !list) return;
    const rows = () => $$("[data-thread-row]");
    const none = $("[data-none]");
    const empty = $("[data-threads-empty]");
    const filter = () => {
      const needle = finder.value.trim().toLowerCase();
      let shown = 0;
      rows().forEach((row) => {
        const match = !needle || row.textContent.toLowerCase().includes(needle);
        row.hidden = !match;
        shown += match ? 1 : 0;
      });
      none.hidden = !rows().length || shown > 0;
    };
    finder.addEventListener("input", filter);
    finder.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      finder.value = "";
      filter();
      finder.blur();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "/" || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
      event.preventDefault();
      set(true);
      finder.focus();
    });

    const timers = new Map();
    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-delete-thread]");
      if (!button) return;
      if (!button.classList.contains("is-confirm")) {
        button.classList.add("is-confirm");
        timers.set(button, setTimeout(() => button.classList.remove("is-confirm"), CONFIRM_MS));
        return;
      }
      clearTimeout(timers.get(button));
      const id = button.dataset.deleteThread;
      try {
        await request(`/chat/${id}/delete`, { method: "POST" });
      } catch (error) {
        return toast(error.message);
      }
      button.closest("[data-thread-row]").remove();
      empty.hidden = rows().length > 0;
      if (location.pathname === `/c/${id}`) location.assign("/");
    });
    railApi.upsert = (thread) => {
      let row = rows().find((item) => item.dataset.threadRow === thread.id);
      if (!row) {
        row = make("li");
        row.dataset.threadRow = thread.id;
        const link = make("a", "thread");
        link.href = `/c/${thread.id}`;
        link.append(make("span"));
        const remove = make("button", "thread__x");
        remove.type = "button";
        remove.dataset.deleteThread = thread.id;
        remove.setAttribute("aria-label", "Delete this chat");
        remove.append(icon("close"));
        row.append(link, remove);
        list.prepend(row);
      }
      $("a", row).classList.add("is-active");
      rows().forEach((other) => other !== row && $("a", other).classList.remove("is-active"));
      $("span", row).textContent = thread.title;
      empty.hidden = true;
    };
    empty.hidden = rows().length > 0;
  };

  const copyText = async (button) => {
    const target = button.dataset.copy ? document.getElementById(button.dataset.copy) : null;
    const draft = button.closest("[data-draft]");
    const source = target ? target.textContent : draft ? draft.dataset.draft : $("pre", button.parentElement).textContent;
    try {
      await navigator.clipboard.writeText(source);
      toast("Copied to the clipboard.");
    } catch {
      toast("Copy is not available here.");
    }
  };

  const record = async (button) => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/webm", "audio/mp4", "audio/ogg"].find((kind) => MediaRecorder.isTypeSupported(kind));
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const chunks = [];
    recorder.addEventListener("dataavailable", (event) => chunks.push(event.data));
    const finished = new Promise((resolve) =>
      recorder.addEventListener("stop", () => {
        stream.getTracks().forEach((track) => track.stop());
        resolve(new Blob(chunks, { type: recorder.mimeType.split(";")[0] }));
      }),
    );
    recording = recorder;
    recorder.start();
    button.classList.add("is-live");
    const timer = setTimeout(() => recorder.stop(), Number(page.dataset.seconds) * 1000);
    const blob = await finished;
    clearTimeout(timer);
    recording = null;
    button.classList.remove("is-live");
    return blob;
  };

  const listen = async (button) => {
    if (recording) {
      recording.stop();
      return "";
    }
    try {
      const blob = await record(button);
      button.classList.add("is-busy");
      const response = await request("/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": blob.type },
        body: blob,
      });
      return (await response.json()).text || "";
    } catch (error) {
      toast(error.name === "NotAllowedError" ? "Microphone access was denied." : error.message);
      return "";
    } finally {
      button.classList.remove("is-busy", "is-live");
    }
  };

  const hush = () => {
    ticket += 1;
    pending = null;
    if (!player) return;
    player.audio.pause();
    URL.revokeObjectURL(player.audio.src);
    player.done();
    player = null;
  };
  const play = async (url, button, onchange) => {
    const same = Boolean(button) && ((player !== null && player.button === button) || pending === button);
    hush();
    if (same) return;
    const mine = ticket;
    pending = button;
    if (button) button.classList.add("is-busy");
    try {
      const response = await request(url);
      const blob = await response.blob();
      if (mine !== ticket) return;
      pending = null;
      const audio = new Audio(URL.createObjectURL(blob));
      const done = () => {
        if (button) button.classList.remove("is-playing");
        if (onchange) onchange(false);
      };
      player = { audio, button, done };
      audio.addEventListener("ended", () => {
        if (player && player.audio === audio) {
          URL.revokeObjectURL(audio.src);
          player = null;
        }
        done();
      });
      await audio.play();
      if (mine !== ticket) return;
      if (button) button.classList.add("is-playing");
      if (onchange) onchange(true);
    } catch (error) {
      if (mine === ticket) {
        player = null;
        pending = null;
        if (button) button.classList.remove("is-playing");
        if (onchange) onchange(false);
        toast(error.message);
      }
    } finally {
      if (button) button.classList.remove("is-busy");
    }
  };

  window.agent = {
    $,
    $$,
    make,
    icon,
    toast,
    remember,
    request,
    postJson,
    reduced,
    csrf: page.dataset.csrf,
    voice: { enabled: Boolean(page.dataset.voice), listen, play, hush },
    rail: railApi,
  };

  $$("[data-select]").forEach(selectBox);
  $$("[data-tabs]").forEach(tabs);
  $$("[data-copy]").forEach((button) => button.addEventListener("click", () => copyText(button)));
  $$("[data-speak]").forEach((button) => button.addEventListener("click", () => play(button.dataset.speak, button)));
  rail();
  document.addEventListener("visibilitychange", () =>
    document.documentElement.toggleAttribute("data-paused", document.hidden),
  );
  document.addEventListener("toast", (event) => toast(event.detail));
  $$("[data-filepick]").forEach((box) => {
    const input = $("[data-file-input]", box);
    const label = $("[data-file-name]", box);
    box.addEventListener("click", () => input.click());
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", () => {
      label.textContent = input.files.length ? input.files[0].name : "No file chosen";
    });
  });
  document.addEventListener("tabs:show", (event) => {
    const bar = $(".savebar");
    if (bar && event.detail.panel) bar.hidden = event.detail.panel.hasAttribute("data-nosave");
  });
  document.addEventListener("submit", (event) => {
    if (event.target.matches("form[action='/logout']")) PRIVATE_KEYS.forEach((key) => remember(key, ""));
  });
  document.addEventListener("click", (event) =>
    $$("[data-select].is-open").forEach((box) => {
      if (!box.contains(event.target)) {
        box.classList.remove("is-open");
        $("button", box).setAttribute("aria-expanded", false);
      }
    }),
  );
})();
