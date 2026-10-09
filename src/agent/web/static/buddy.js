// src/agent/web/static/buddy.js
"use strict";
(() => {
  const root = document.querySelector("[data-buddy]");
  if (!root) return;
  const { $, make, icon, reduced, request, postJson, toast, voice } = window.agent;
  const config = JSON.parse(root.dataset.config);
  const tie = $("svg.tie", root);
  const look = $("[data-look]", root);
  const opener = $("[data-tie-open]", root);
  const hint = $("[data-tie-hint]", root);
  const panel = $("[data-tiepanel]");
  const feed = $("[data-tiefeed]");
  const form = $("[data-tieform]");
  const field = $("textarea", form);
  const mic = $("[data-tie-mic]");
  const status = $("[data-tie-status]");
  const clock = $("[data-clock]");
  const focusButton = $("[data-focus-toggle]");
  const nudgeButton = $("[data-nudge-toggle]");
  const voiceButton = $("[data-voice-toggle]");
  const clearButton = $("[data-tie-clear]");
  const bubble = $("[data-nudge]");
  const lineBox = $("[data-line]");
  const spoken = $("[data-sr]");
  const baseTitle = document.title;
  const STORE = { greeted: "tie-greeted", focus: "tie-focus", detours: "tie-detours", queue: "tie-queue", pending: "tie-pending" };
  const TERMINAL = { done: "happy", failed: "worried", question: "worried" };
  const TITLES = { done: "Done: ", failed: "Stopped: ", question: "Your pick: " };
  const FOCUS_KEEPS = ["question", "detour", "focus_start", "focus_break", "focus_end", "focus_stop"];
  const MINUTE = 60000;
  const NEAR_BOTTOM = 80;
  const MAX_FIELD_PX = 112;
  const IDLE_HINT = "Ready to talk";
  const QUIET_HINT = "Quiet, tap to talk";
  const state = {
    mode: root.dataset.mode,
    running: Boolean(document.querySelector("[data-live][data-running='1']")),
    sources: 0,
    findings: 0,
    away: null,
    stamps: [],
    writing: false,
    typing: 0,
    bubbleTimer: 0,
    moodTimer: 0,
    wiggleTimer: 0,
    playing: false,
    last: "",
    lastKey: "",
    focus: null,
    active: Date.now(),
    idleSaid: false,
    loaded: false,
    waiting: false,
    open: false,
  };
  const aim = { x: 0, y: 0, tx: 0, ty: 0, frame: 0 };

  const read = (key) => {
    try {
      return JSON.parse(sessionStorage.getItem(key));
    } catch {
      return null;
    }
  };
  const write = (key, value) => {
    try {
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      return;
    }
  };
  const format = (template, values) => template.replace(/\{(\w+)\}/g, (whole, key) => (key in values ? String(values[key]) : whole));
  const clockText = (milliseconds) => {
    const total = Math.max(0, Math.ceil(milliseconds / 1000));
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  };
  const restart = (node, name) => {
    node.classList.remove(name);
    void node.getBoundingClientRect();
    node.classList.add(name);
  };
  const nudging = () => state.mode !== "off";

  const baseMood = () => {
    if (document.hidden) return "sleepy";
    if (state.focus && state.focus.kind === "break") return "happy";
    if (state.focus || state.running || state.waiting) return "working";
    return "idle";
  };
  const setMood = (mood, hold = false) => {
    clearTimeout(state.moodTimer);
    if (tie.dataset.mood === mood) {
      tie.removeAttribute("data-mood");
      void tie.getBoundingClientRect();
    }
    tie.dataset.mood = mood;
    if (!hold && mood !== baseMood()) state.moodTimer = setTimeout(() => setMood(baseMood(), true), config.mood_seconds * 1000);
  };
  const talking = () => {
    if (state.writing || state.playing) tie.dataset.talking = "1";
    else tie.removeAttribute("data-talking");
  };

  const type = (text) => {
    clearInterval(state.typing);
    spoken.textContent = text;
    if (reduced || !config.type_ms) {
      lineBox.textContent = text;
      state.writing = false;
      talking();
      return;
    }
    let shown = 0;
    lineBox.textContent = "";
    state.writing = true;
    talking();
    state.typing = setInterval(() => {
      shown += 1;
      lineBox.textContent = text.slice(0, shown);
      if (shown < text.length) return;
      clearInterval(state.typing);
      state.writing = false;
      talking();
    }, config.type_ms);
  };

  const hideBubble = () => {
    clearTimeout(state.bubbleTimer);
    bubble.hidden = true;
  };
  const scheduleHide = (text) => {
    clearTimeout(state.bubbleTimer);
    if (document.hidden) return;
    state.bubbleTimer = setTimeout(hideBubble, config.bubble_seconds * 1000 + text.length * config.read_ms);
  };

  const near = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < NEAR_BOTTOM;
  const bubbleUp = (role, text, id) => {
    const stick = near() || role === "you";
    const item = make("li", `tm tm--${role} is-new`, text);
    if (role === "tie" && id && voice.enabled) {
      const say = make("button", "tm__say");
      say.type = "button";
      say.setAttribute("aria-label", "Listen to this reply");
      say.append(icon("speaker"));
      say.addEventListener("click", () => voice.play(`/chat/messages/${id}/speech`, say, setPlaying));
      item.append(say);
    }
    feed.append(item);
    if (stick) feed.scrollTop = feed.scrollHeight;
    return item;
  };
  const setPlaying = (playing) => {
    state.playing = playing;
    talking();
  };
  const play = (url) => voice.play(url, null, setPlaying);

  const allowed = () => {
    const now = Date.now();
    state.stamps = state.stamps.filter((stamp) => now - stamp < MINUTE);
    return state.stamps.length < config.max_lines_per_minute;
  };
  const queueLine = (key) => {
    const queue = read(STORE.queue) || [];
    if (!queue.includes(key)) write(STORE.queue, [...queue, key]);
    root.classList.add("is-alert");
  };
  const say = (key, values = {}, options = {}) => {
    const template = config.lines[key];
    if (!template || !nudging()) return;
    if (state.focus && state.focus.kind === "focus" && !FOCUS_KEEPS.includes(key)) {
      if (key === "done" || key === "failed") queueLine(key);
      return;
    }
    if (!options.force && !allowed()) return;
    const text = format(template, values);
    state.stamps.push(Date.now());
    state.last = text;
    state.lastKey = key;
    if (state.loaded) bubbleUp("tie", text);
    if (!state.open) {
      bubble.hidden = false;
      type(text);
      scheduleHide(text);
    } else spoken.textContent = text;
    setMood(options.mood || baseMood());
    if (state.mode === "voice" && config.voice_lines.includes(key)) play(`/buddy/speech/${key}`);
  };
  const flushQueue = () => {
    const queue = read(STORE.queue) || [];
    write(STORE.queue, null);
    root.classList.remove("is-alert");
    queue.slice(-2).forEach((key, index) => setTimeout(() => say(key, {}, { force: true, mood: TERMINAL[key] }), (index + 1) * config.queue_gap_ms));
  };

  const renderHint = () => {
    if (state.focus) hint.textContent = `${state.focus.kind === "break" ? "Break" : "Focus"} ${clockText(state.focus.end - Date.now())}`;
    else hint.textContent = nudging() ? IDLE_HINT : QUIET_HINT;
  };
  const renderFocus = () => {
    const active = Boolean(state.focus);
    clock.hidden = !active;
    root.classList.toggle("is-break", active && state.focus.kind === "break");
    focusButton.textContent = active ? "Stop focus" : `Focus ${config.focus_minutes} min`;
    focusButton.setAttribute("aria-pressed", String(active));
    if (active) clock.textContent = clockText(state.focus.end - Date.now());
    renderHint();
  };
  const setFocus = (next) => {
    state.focus = next;
    write(STORE.focus, next);
    renderFocus();
  };
  const startFocus = () => {
    write(STORE.detours, 0);
    setFocus({ kind: "focus", end: Date.now() + config.focus_minutes * MINUTE });
    say("focus_start", {}, { force: true, mood: "working" });
  };
  const stopFocus = () => {
    setFocus(null);
    say("focus_stop", {}, { force: true });
    flushQueue();
  };
  const tick = () => {
    if (!state.focus) return;
    if (Date.now() < state.focus.end) {
      clock.textContent = clockText(state.focus.end - Date.now());
      renderHint();
      return;
    }
    if (state.focus.kind === "focus") {
      setFocus({ kind: "break", end: Date.now() + config.break_minutes * MINUTE });
      say("focus_break", {}, { force: true, mood: "happy" });
      flushQueue();
      return;
    }
    setFocus(null);
    say("focus_end", {}, { force: true });
  };

  const returned = () => {
    const away = state.away;
    state.away = null;
    if (!away || Date.now() - away.at < config.away_seconds * 1000) return;
    if (state.focus && state.focus.kind === "focus") {
      const count = (read(STORE.detours) || 0) + 1;
      write(STORE.detours, count);
      say("detour", { count }, { force: true });
      return;
    }
    const sources = state.sources - away.sources;
    const findings = state.findings - away.findings;
    if (sources + findings > 0) say("away_back", { sources, findings }, { mood: "happy" });
    else if (state.running) say("away_quiet", {}, {});
  };
  const markTitle = (key) => {
    if (document.hidden && TITLES[key]) document.title = TITLES[key] + baseTitle;
  };

  const choose = async (mode) => {
    try {
      await postJson("/buddy", { mode });
      return true;
    } catch (error) {
      toast(error.message);
      return false;
    }
  };
  const applyMode = (mode) => {
    state.mode = mode;
    root.dataset.mode = mode;
    nudgeButton.setAttribute("aria-pressed", String(mode !== "off"));
    nudgeButton.textContent = mode === "off" ? "Nudges are off" : "Nudges are on";
    if (voiceButton) voiceButton.setAttribute("aria-pressed", String(mode === "voice"));
    if (mode === "off") hideBubble();
    root.classList.toggle("is-alert", mode !== "off" && Boolean(read(STORE.queue)));
    renderHint();
  };

  const follow = () => {
    aim.x += (aim.tx - aim.x) * 0.18;
    aim.y += (aim.ty - aim.y) * 0.18;
    look.setAttribute("transform", `translate(${aim.x.toFixed(2)} ${aim.y.toFixed(2)})`);
    aim.frame = Math.abs(aim.tx - aim.x) + Math.abs(aim.ty - aim.y) > 0.02 ? requestAnimationFrame(follow) : 0;
  };
  const wiggle = () => {
    if (!document.hidden && tie.dataset.mood === "idle") restart(tie, "is-wiggle");
    state.wiggleTimer = setTimeout(wiggle, (config.wiggle_seconds + Math.random() * config.wiggle_spread_seconds) * 1000);
  };

  const grow = () => {
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, MAX_FIELD_PX)}px`;
  };
  const currentJob = () => {
    const onPage = location.pathname.match(/^\/jobs\/([^/]+)/);
    if (onPage) return onPage[1];
    const links = [...document.querySelectorAll(".resultcard a[href^='/jobs/'], [data-live]")];
    const last = links[links.length - 1];
    const text = last ? last.getAttribute("href") || last.dataset.live : "";
    const found = text && text.match(/\/jobs\/([^/]+)/);
    return found ? found[1] : "";
  };
  const showTyping = () => {
    const item = make("li", "tm tm--tie is-new");
    const dots = make("span", "typing");
    dots.append(make("i"), make("i"), make("i"));
    item.append(dots);
    feed.append(item);
    feed.scrollTop = feed.scrollHeight;
    return item;
  };

  const load = async () => {
    if (state.loaded) return;
    state.loaded = true;
    try {
      const data = await (await request("/tie/messages")).json();
      data.messages.forEach((item) => bubbleUp(item.role === "user" ? "you" : item.kind === "notice" ? "note" : "tie", item.text, item.role === "assistant" ? item.id : 0));
      if (!data.messages.length) bubbleUp("tie", data.hello);
    } catch (error) {
      state.loaded = false;
      toast(error.message);
    }
  };
  const talk = async (text) => {
    if (state.waiting || !text.trim()) return;
    state.waiting = true;
    status.textContent = "Thinking";
    setMood("working", true);
    bubbleUp("you", text.trim());
    field.value = "";
    grow();
    const dots = showTyping();
    try {
      const data = await postJson("/tie/send", { text, job: currentJob(), page: root.dataset.page });
      dots.remove();
      const reply = data.messages.filter((item) => item.role === "assistant");
      reply.forEach((item) => bubbleUp(item.kind === "notice" ? "note" : "tie", item.text, item.id));
      const last = reply[reply.length - 1];
      state.last = last ? last.text : state.last;
      if (last && last.kind !== "notice") setMood("happy");
      if (last && state.mode === "voice" && last.kind !== "notice") play(`/chat/messages/${last.id}/speech`);
    } catch (error) {
      dots.remove();
      bubbleUp("note", error.message);
      setMood("worried");
    } finally {
      state.waiting = false;
      status.textContent = "Here to think it through with you";
      setMood(baseMood(), true);
    }
  };

  const openPanel = () => {
    state.open = true;
    panel.hidden = false;
    opener.setAttribute("aria-expanded", "true");
    hideBubble();
    root.classList.remove("is-alert");
    setMood("surprised");
    load().then(() => {
      feed.scrollTop = feed.scrollHeight;
    });
    field.focus();
  };
  const closePanel = () => {
    state.open = false;
    panel.hidden = true;
    opener.setAttribute("aria-expanded", "false");
    voice.hush();
    opener.focus();
  };

  const touch = () => {
    state.active = Date.now();
  };
  ["pointerdown", "keydown", "scroll"].forEach((name) => addEventListener(name, touch, { passive: true, capture: true }));
  addEventListener(
    "pointermove",
    (event) => {
      touch();
      if (reduced || document.hidden) return;
      const box = tie.getBoundingClientRect();
      const dx = event.clientX - (box.left + box.width / 2);
      const dy = event.clientY - (box.top + box.height * 0.45);
      const length = Math.hypot(dx, dy) || 1;
      const strength = Math.min(1, length / config.look_distance);
      aim.tx = (dx / length) * config.look_range * strength;
      aim.ty = (dy / length) * config.look_range * strength;
      if (!aim.frame) aim.frame = requestAnimationFrame(follow);
    },
    { passive: true },
  );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.open) closePanel();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      state.away = { at: Date.now(), sources: state.sources, findings: state.findings };
      setMood("sleepy", true);
      return;
    }
    document.title = baseTitle;
    const welcome = !bubble.hidden && Object.hasOwn(TERMINAL, state.lastKey);
    setMood(welcome ? TERMINAL[state.lastKey] : baseMood(), !welcome);
    returned();
    if (!bubble.hidden) scheduleHide(state.last);
  });
  document.addEventListener("agent:event", ({ detail }) => {
    if (detail.kind === "source") state.sources += 1;
    else if (detail.kind === "finding") state.findings += 1;
    else if (Object.hasOwn(TERMINAL, detail.kind)) {
      state.running = false;
      if (document.querySelector("[data-reload]")) write(STORE.pending, detail.kind);
      else {
        markTitle(detail.kind);
        say(detail.kind, {}, { force: true, mood: TERMINAL[detail.kind] });
      }
      return;
    }
    state.running = true;
  });

  opener.addEventListener("click", () => (state.open ? closePanel() : openPanel()));
  $("[data-tie-close]").addEventListener("click", closePanel);
  bubble.addEventListener("click", openPanel);
  focusButton.addEventListener("click", () => (state.focus ? stopFocus() : startFocus()));
  nudgeButton.addEventListener("click", async () => {
    const next = nudging() ? "off" : voiceButton && voiceButton.getAttribute("aria-pressed") === "true" ? "voice" : "text";
    if (await choose(next)) applyMode(next);
  });
  if (voiceButton) {
    voiceButton.addEventListener("click", async () => {
      const next = state.mode === "voice" ? "text" : "voice";
      if (!(await choose(next))) return;
      applyMode(next);
      if (next !== "voice") voice.hush();
    });
  }
  if (clearButton) {
    clearButton.addEventListener("click", async () => {
      try {
        await request("/tie/clear", { method: "POST" });
      } catch (error) {
        return toast(error.message);
      }
      feed.replaceChildren();
      state.loaded = false;
      load();
    });
  }
  if (mic) {
    mic.addEventListener("click", async () => {
      const heard = await voice.listen(mic);
      if (heard) talk(heard);
    });
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    talk(field.value);
  });
  field.addEventListener("input", grow);
  field.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    talk(field.value);
  });

  const saved = read(STORE.focus);
  if (saved && (saved.kind === "focus" || saved.kind === "break") && Number.isFinite(saved.end)) state.focus = saved;
  renderFocus();
  if (reduced && tie.pauseAnimations) tie.pauseAnimations();
  setInterval(tick, 1000);
  setInterval(() => {
    if (state.running && !document.hidden) say("progress", { sources: state.sources, findings: state.findings }, { mood: "working" });
  }, config.nudge_seconds * 1000);
  setInterval(() => {
    const quiet = !state.idleSaid && !state.running && !state.focus && !document.hidden && !state.open;
    if (!quiet || root.dataset.page !== "chat") return;
    if (Date.now() - state.active < config.idle_seconds * 1000) return;
    state.idleSaid = true;
    say("idle", {}, {});
  }, 5000);
  if (!reduced) state.wiggleTimer = setTimeout(wiggle, (config.wiggle_seconds + Math.random() * config.wiggle_spread_seconds) * 1000);

  const pending = read(STORE.pending);
  write(STORE.pending, null);
  if (read(STORE.queue) && state.focus) root.classList.add("is-alert");
  setMood(baseMood(), true);
  if (pending) {
    markTitle(pending);
    say(pending, {}, { force: true, mood: TERMINAL[pending] });
  } else if (!read(STORE.greeted)) {
    write(STORE.greeted, true);
    say("greeting", {}, { force: true, mood: "happy" });
  } else if (state.running) {
    say("working", {}, {});
  }
})();
