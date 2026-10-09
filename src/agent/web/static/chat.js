// src/agent/web/static/chat.js
"use strict";
(() => {
  const { $, make, icon, reduced, request, toast, voice, csrf, rail, live } = window.agent;
  const root = $("[data-chat]");
  if (!root) return;
  const scroller = $("[data-scroll]");
  const inner = $("[data-inner]");
  const feed = $("[data-feed]");
  const hello = $("[data-hello]");
  const form = $("[data-composer]");
  const input = $("[data-input]");
  const send = $("[data-send]");
  const jump = $("[data-jump]");
  const jumpText = $("[data-jump-text]");
  const heading = $("[data-title]");
  const cv = $("[data-cv]");
  const fileBox = $("[data-file]");
  const fileName = $("[data-file-name]");
  const mic = $("[data-mic]");
  const initial = JSON.parse($("[data-initial]").textContent);
  const NEAR_BOTTOM = 72;
  const AUTO_QUIET_MS = 140;
  const POLL_MS = 5000;
  const MAX_INPUT_PX = 160;
  const PLACEHOLDERS = {
    idle: "Name someone, or describe who you need",
    proposed: "Say yes to start, or tell me what to change",
    asking: "Type a number, or say none",
    running: "Ask something while you wait",
  };
  const state = {
    thread: root.dataset.thread,
    phase: initial.thread ? initial.thread.phase : "idle",
    last: 0,
    sending: false,
    stick: true,
    unseen: 0,
    auto: 0,
    typing: null,
    seen: new Set(),
    view: root.dataset.view,
  };

  const gap = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  const toBottom = () => {
    state.auto = performance.now();
    scroller.scrollTop = scroller.scrollHeight;
  };
  const clearJump = () => {
    state.unseen = 0;
    jump.hidden = true;
  };
  scroller.addEventListener("scroll", () => {
    if (performance.now() - state.auto < AUTO_QUIET_MS) return;
    state.stick = gap() < NEAR_BOTTOM;
    if (state.stick) clearJump();
  });
  new ResizeObserver(() => {
    if (state.stick) toBottom();
  }).observe(inner);
  jump.addEventListener("click", () => {
    state.stick = true;
    clearJump();
    toBottom();
  });

  const anchor = (href, label) => {
    const item = make("a", "textlink", label);
    item.href = href;
    item.append(icon("arrow"));
    return item;
  };

  const rest = (message) => {
    const data = message.data || {};
    let text = message.text;
    if (data.task && text.startsWith(data.task)) text = text.slice(data.task.length);
    text = text.trimStart();
    if (data.context && text.startsWith(data.context)) text = text.slice(data.context.length);
    return text.trim();
  };

  const speaker = (message) => {
    const tools = make("div", "msg__tools");
    const button = make("button", "ibtn");
    button.type = "button";
    button.setAttribute("aria-label", "Listen to this message");
    button.append(icon("speaker"));
    button.addEventListener("click", () => voice.play(`/chat/messages/${message.id}/speech`, button));
    tools.append(button);
    return tools;
  };

  const retire = () => {
    feed.querySelectorAll(".quick").forEach((box) => box.remove());
    feed.querySelectorAll(".pick").forEach((box) => box.classList.add("is-done"));
  };

  const plan = (message, active) => {
    const data = message.data || {};
    const card = make("div", "plan");
    card.append(make("p", "plan__task", data.task || message.text));
    if (data.context) card.append(make("p", "plan__context", data.context));
    const parts = [card];
    const tail = rest(message);
    if (tail) parts.push(make("p", "hint", tail));
    if (active) {
      const box = make("div", "quick");
      const yes = make("button", "reply", "Yes, start");
      yes.type = "button";
      yes.addEventListener("click", () => submit("yes"));
      box.append(yes);
      parts.push(box);
    }
    return parts;
  };

  const question = (message, active) => {
    const data = message.data || {};
    const list = make("div", `pick${active ? "" : " is-done"}`);
    const choose = (value) => submit(String(value));
    (data.options || []).forEach((option) => {
      const item = make("button", "pick__item");
      item.type = "button";
      const body = make("span");
      body.append(make("div", "pick__title", option.title || option.url));
      if (option.snippet) body.append(make("div", "pick__snippet", option.snippet));
      body.append(make("div", "pick__url", option.url));
      item.append(make("span", "pick__n", String(option.n)), body);
      item.addEventListener("click", () => choose(option.n));
      list.append(item);
    });
    const none = make("button", "pick__item");
    none.type = "button";
    none.append(make("span", "pick__n", "0"), make("span", "pick__title", "None of these"));
    none.addEventListener("click", () => choose("none"));
    list.append(none);
    return [make("p", "msg__text", message.text), list];
  };

  const job = (message, running) => {
    const data = message.data || {};
    if (!running) {
      const line = make("div", "jobline");
      line.append(make("strong", "", data.title || "Search"), anchor(`/jobs/${data.job_id}`, "Open"));
      return [make("p", "msg__text", message.text), line];
    }
    const box = make("div", "live");
    box.dataset.live = `/jobs/${data.job_id}/events`;
    box.dataset.after = String(data.after || 0);
    box.dataset.running = "1";
    box.dataset.view = state.view;
    box.dataset.title = data.title || "Search";
    return [make("p", "msg__text", message.text), box];
  };

  const result = (message) => {
    const data = message.data || {};
    const card = make("div", `resultcard${data.rating != null ? "" : " resultcard--plain"}`);
    const score = make("div");
    if (data.rating != null) {
      const value = make("div", "resultcard__score");
      value.append(document.createTextNode(Number(data.rating).toFixed(1)), make("small", "", "out of 5"));
      score.append(value, make("div", "resultcard__label", "Overall rating"));
      card.append(score);
    }
    const actions = make("div", "resultcard__actions");
    actions.append(anchor(`/jobs/${data.job_id}`, "Open the full report"), anchor(`/emails?job=${data.job_id}`, "Draft emails"));
    card.append(actions);
    return [make("p", "msg__text", message.text), card];
  };

  const build = (message, context) => {
    const item = make("li", `msg msg--${message.role}`);
    item.dataset.id = String(message.id);
    if (message.role === "user") {
      item.append(make("p", "msg__text", message.text));
      return item;
    }
    const main = make("div", "msg__main");
    let tools = voice.enabled && ["text", "result"].includes(message.kind);
    if (message.kind === "notice") {
      const note = make("div", "notice");
      note.append(icon("warn"), make("span", "", message.text));
      main.append(note);
    } else if (message.kind === "proposal") main.append(...plan(message, context.last));
    else if (message.kind === "question") main.append(...question(message, context.last));
    else if (message.kind === "job") main.append(...job(message, context.live));
    else if (message.kind === "result") main.append(...result(message));
    else main.append(make("p", "msg__text", message.text));
    if (tools) main.append(speaker(message));
    const avatar = make("span", "msg__avatar");
    avatar.setAttribute("aria-hidden", "true");
    item.append(avatar, main);
    return item;
  };

  const syncThread = (thread) => {
    if (!thread) return;
    if (!state.thread) {
      state.thread = thread.id;
      history.replaceState(null, "", `/c/${thread.id}`);
    }
    state.phase = thread.phase;
    heading.textContent = thread.title;
    document.title = `${thread.title} | HMNH`;
    input.placeholder = PLACEHOLDERS[state.phase] || PLACEHOLDERS.idle;
    rail.upsert(thread);
  };

  const showTyping = () => {
    if (state.typing) return;
    const item = make("li", "msg msg--assistant");
    const dots = make("div", "typing");
    dots.append(make("i"), make("i"), make("i"));
    const avatar = make("span", "msg__avatar");
    const main = make("div", "msg__main");
    main.append(dots);
    item.append(avatar, main);
    feed.append(item);
    state.typing = item;
  };
  const hideTyping = () => {
    if (state.typing) state.typing.remove();
    state.typing = null;
  };

  const add = (messages, fresh) => {
    const batch = messages.filter((message) => !state.seen.has(message.id));
    if (!batch.length) return;
    hideTyping();
    hello.hidden = true;
    if (fresh) retire();
    const lastJob = [...batch].reverse().find((message) => message.kind === "job");
    const finalId = batch[batch.length - 1].id;
    let incoming = 0;
    batch.forEach((message, index) => {
      state.seen.add(message.id);
      state.last = Math.max(state.last, message.id);
      const previous = index ? batch[index - 1] : null;
      const item = build(message, {
        last: message.id === finalId,
        live: message === lastJob && state.phase === "running" && message.id === finalId,
      });
      if (message.role === "assistant" && previous && previous.role === "assistant") item.classList.add("msg--follow");
      if (fresh) item.classList.add("is-new");
      feed.append(item);
      if (message.role === "assistant") incoming += 1;
      live.scan(item);
    });
    if (fresh && incoming && !state.stick) {
      state.unseen += incoming;
      jumpText.textContent = state.unseen > 1 ? `${state.unseen} new messages` : "New message";
      jump.hidden = false;
    }
  };

  const fail = (error) => toast(error.message || "The message could not be sent.");

  const submit = async (value) => {
    const text = (value !== undefined ? value : input.value).trim();
    const file = cv.files[0] || null;
    if (state.sending || (!text && !file)) return;
    state.sending = true;
    send.disabled = true;
    const local = make("li", "msg msg--user is-new");
    local.append(make("p", "msg__text", text || file.name));
    hello.hidden = true;
    retire();
    feed.append(local);
    state.stick = true;
    clearJump();
    toBottom();
    showTyping();
    const body = new FormData();
    body.append("text", text);
    body.append("thread", state.thread);
    body.append("csrf_token", csrf);
    if (file) body.append("cv", file);
    if (value === undefined) {
      input.value = "";
      grow();
    }
    clearFile();
    try {
      const response = await fetch("/chat/send", { method: "POST", credentials: "same-origin", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "The message could not be sent.");
      local.remove();
      syncThread(data.thread);
      add(data.messages, true);
    } catch (error) {
      local.remove();
      hideTyping();
      if (value === undefined && !input.value) input.value = text;
      grow();
      fail(error);
    } finally {
      state.sending = false;
      refreshSend();
      input.focus();
    }
  };

  const poll = async () => {
    if (!state.thread || state.sending || document.hidden) return;
    try {
      const response = await request(`/chat/${state.thread}/messages?after=${state.last}`);
      const data = await response.json();
      syncThread(data.thread);
      add(data.messages, true);
    } catch {
      return;
    }
  };

  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_INPUT_PX)}px`;
  };
  const refreshSend = () => {
    send.disabled = state.sending || (!input.value.trim() && !cv.files.length);
  };
  const showFile = () => {
    const file = cv.files[0];
    fileBox.hidden = !file;
    fileName.textContent = file ? file.name : "";
    refreshSend();
  };
  function clearFile() {
    cv.value = "";
    showFile();
  }

  input.addEventListener("input", () => {
    grow();
    refreshSend();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    submit();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  $("[data-attach]").addEventListener("click", () => cv.click());
  $("[data-file-clear]").addEventListener("click", clearFile);
  cv.addEventListener("change", showFile);
  ["dragover", "drop"].forEach((name) =>
    root.addEventListener(name, (event) => {
      if (!event.dataTransfer || ![...event.dataTransfer.types].includes("Files")) return;
      event.preventDefault();
      if (name === "drop" && event.dataTransfer.files.length) {
        cv.files = event.dataTransfer.files;
        showFile();
      }
    }),
  );
  if (mic) {
    mic.addEventListener("click", async () => {
      const spoken = await voice.listen(mic);
      if (spoken) submit(spoken);
    });
  }
  document.querySelectorAll("[data-try]").forEach((button) => button.addEventListener("click", () => submit(button.textContent)));
  feed.addEventListener("live:end", () => setTimeout(poll, reduced ? 50 : 700));
  feed.addEventListener("live:closed", () => setTimeout(poll, reduced ? 50 : 700));
  document.addEventListener("visibilitychange", poll);
  setInterval(() => {
    if (state.phase === "running" || state.phase === "asking") poll();
  }, POLL_MS);

  if (initial.thread) syncThread(initial.thread);
  add(initial.messages, false);
  refreshSend();
  toBottom();
  if (!initial.messages.length) input.focus();
})();
