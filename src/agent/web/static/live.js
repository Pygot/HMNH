// src/agent/web/static/live.js
"use strict";
(() => {
  const { make, icon: glyph, reduced, postJson } = window.agent;
  const SVG = "http://www.w3.org/2000/svg";
  const SHAPES = { supported: "full", met: "full", "single-source": "half", partial: "half", uncertain: "none", unknown: "dash", unmet: "cross" };
  const KINDS = { subject: "Subject", company: "Company", source: "Source", candidate: "Candidate", finding: "Finding", requirement: "Requirement" };
  const RINGS = { subject: 0, company: 200, source: 235, candidate: 235, requirement: 340, finding: 390 };
  const LENGTHS = { company: 200, source: 235, candidate: 235, finding: 170, requirement: 170 };
  const BOX = { min: 0.3, max: 2.4, fit: 1.1, margin: 36, zoomStep: 1.25, wheel: 0.0015 };
  const SIM = { cool: 0.986, floor: 0.02, kick: 0.85, damping: 0.8, charge: 6000, cap: 6, gapX: 20, gapY: 14, settle: 260, stretch: 2.2, squash: 0.72 };
  const GOLDEN = 2.399963;
  const NOTE_LIMIT = 150;
  const icons = new Map(
    [...document.querySelectorAll("template[data-icons]")].flatMap((box) => [...box.content.querySelectorAll("svg")]).map((item) => [item.dataset.task, item]),
  );
  let serial = 0;

  const svg = (tag, attributes = {}) => {
    const node = document.createElementNS(SVG, tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  const taskIcon = (task) => {
    const found = icons.get(task) || icons.get("search");
    return found ? found.cloneNode(true) : make("span");
  };
  const mark = (value) => {
    const node = make("i", `mark mark--${SHAPES[value] || "none"}`);
    node.setAttribute("aria-hidden", "true");
    return node;
  };
  const host = (url) => {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch {
      return url || "";
    }
  };
  const safe = (url) => (/^https?:\/\//i.test(url || "") ? url : "");
  const clip = (value, limit) => (value.length > limit ? `${value.slice(0, limit - 3)}...` : value);
  const words = (value) => String(value || "").replaceAll("_", " ").replaceAll("-", " ");
  const link = (url) => {
    const anchor = make("a", "", host(url));
    anchor.href = safe(url) || "#";
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer nofollow";
    anchor.referrerPolicy = "no-referrer";
    return anchor;
  };

  const createLog = (list) => {
    let current = null;
    const retire = () => {
      if (current) current.classList.remove("is-live");
      current = null;
    };
    const row = (className, lead, parts, meta) => {
      const item = make("li", `log__row ${className}`);
      const first = make("span", "log__mark");
      first.append(lead);
      const body = make("div");
      body.append(...parts);
      item.append(first, body, make("span", "log__meta", meta || ""));
      const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
      list.append(item);
      if (stick) list.scrollTop = list.scrollHeight;
      return item;
    };
    const line = (text, sub) => {
      const parts = [document.createTextNode(text)];
      if (sub) parts.push(make("span", "log__sub", sub));
      return parts;
    };
    const bars = (criteria) => {
      const box = make("div", "log__bars");
      criteria.forEach((item) => {
        const track = make("span", "meter");
        track.append(make("i", `p${Math.round((item.score / 5) * 20) * 5}`));
        const bar = make("div", "log__bar");
        bar.append(make("small", "", words(item.name)), track, make("b", "", String(item.score)));
        box.append(bar);
      });
      return box;
    };
    const add = (event) => {
      const data = event.data || {};
      const kind = event.kind;
      if (kind === "stage" || kind === "step") {
        retire();
        current = row(`log__row--${kind} is-live`, taskIcon(event.task), line(event.text));
      } else if (kind === "note") {
        row("log__row--note", make("span"), line(event.text));
      } else if (kind === "company") {
        row("", taskIcon("company"), line(event.text), words(data.fact_kind));
      } else if (kind === "claim") {
        row("", mark("uncertain"), line(event.text, words(data.category)), host(data.url));
      } else if (kind === "finding") {
        row("", mark(data.tag), line(event.text), words(data.tag));
      } else if (kind === "requirement") {
        row("", mark(data.status), line(event.text.split(": ")[0], data.detail), words(data.status));
      } else if (kind === "candidate" || kind === "source") {
        const score = data.score != null ? ` | match ${Number(data.score).toFixed(2)}` : "";
        row("", taskIcon(event.task), line(kind === "source" ? `Read ${event.text}` : event.text), `${host(data.url)}${score}`);
      } else if (kind === "rating") {
        const parts = line(event.text);
        if ((data.criteria || []).length) parts.push(bars(data.criteria));
        row("", taskIcon("rate"), parts);
      } else if (kind === "scepticism") {
        row("", taskIcon("scepticism"), line(event.text));
      } else {
        retire();
        row(`log__row--${kind}`, taskIcon(kind === "question" ? "wait" : "export"), line(event.text));
      }
    };
    return { add, retire };
  };

  const createGraph = (box, subjectLabel) => {
    const world = make("div", "graph__world");
    const lines = svg("svg", { class: "graph__edges", "aria-hidden": "true" });
    const edgeLayer = svg("g");
    lines.append(edgeLayer);
    world.append(lines);
    const empty = make("p", "graph__empty", "The graph fills in as the agent finds things.");
    const detail = make("aside", "graph__detail");
    detail.hidden = true;
    detail.setAttribute("aria-live", "polite");
    box.append(world, empty);

    const tools = make("div", "graph__tools");
    const button = (label, title, action) => {
      const item = make("button", "", label);
      item.type = "button";
      item.title = title;
      item.setAttribute("aria-label", title);
      item.addEventListener("click", action);
      return item;
    };
    const legend = make("div", "graph__legend");
    const key = (shape, label) => {
      const item = make("span");
      item.append(mark(shape), label);
      return item;
    };
    legend.append(key("full", "supported or met"), key("half", "one source or partial"), key("none", "uncertain"), key("dash", "no evidence"), key("cross", "unmet"));
    box.append(tools, detail);

    const nodes = new Map();
    const edges = [];
    const counters = {};
    const view = { x: 0, y: 0, k: 1 };
    const target = { x: 0, y: 0, k: 1 };
    const aspect = () => Math.max(1, Math.min(SIM.stretch, box.clientWidth / Math.max(box.clientHeight, 1)));
    const state = { auto: true, alpha: 0, frame: 0, selected: null, hovered: null, drag: null, settling: 0 };

    const applyView = () => {
      world.style.transform = `translate(${view.x.toFixed(1)}px, ${view.y.toFixed(1)}px) scale(${view.k.toFixed(3)})`;
    };
    const describeKind = (node) => {
      node.kindEl.replaceChildren();
      if (node.kind === "finding") node.kindEl.append(mark(node.tag));
      if (node.kind === "requirement") node.kindEl.append(mark(node.status));
      const extra = node.kind === "finding" ? words(node.tag) : node.kind === "requirement" ? words(node.status) : "";
      node.kindEl.append(document.createTextNode([KINDS[node.kind], extra].filter(Boolean).join(" | ")));
    };
    const measure = (node) => {
      node.w = node.el.offsetWidth;
      node.h = node.el.offsetHeight;
    };
    const refresh = (node) => {
      describeKind(node);
      node.textEl.textContent = clip(node.label, NOTE_LIMIT);
      measure(node);
    };
    const addNode = (key, kind, label, extra = {}) => {
      let node = nodes.get(key);
      const fresh = !node;
      if (fresh) {
        const el = make("div", `gnode gnode--${kind}`);
        const card = make("div", "gnode__card");
        const kindEl = make("span", "gnode__kind");
        const textEl = make("span", "gnode__text");
        card.append(kindEl, textEl);
        el.append(card);
        world.append(el);
        const count = (counters[kind] = (counters[kind] || 0) + 1);
        const angle = count * GOLDEN + (kind === "finding" ? 0.7 : kind === "requirement" ? 1.9 : 0);
        const radius = RINGS[kind] * (0.85 + 0.15 * Math.random());
        node = {
          id: key, kind, label, el, kindEl, textEl, links: new Set(), urls: [], facts: [],
          x: Math.cos(angle) * radius * aspect(), y: Math.sin(angle) * radius * SIM.squash, vx: 0, vy: 0, w: 0, h: 0,
          pinned: kind === "subject", tag: null, status: null, info: {},
        };
        el.dataset.node = key;
        nodes.set(key, node);
        empty.hidden = true;
      }
      Object.assign(node, extra);
      node.label = label || node.label;
      refresh(node);
      return node;
    };
    const connect = (a, b, dashed = false) => {
      if (!a || !b || a === b || a.links.has(b.id)) return;
      a.links.add(b.id);
      b.links.add(a.id);
      const line = svg("line", { class: `edge edge--${dashed ? "dashed" : "solid"}`, pathLength: "1" });
      edgeLayer.append(line);
      edges.push({ a, b, line, length: LENGTHS[a.kind === "subject" ? b.kind : a.kind] || 220 });
    };

    const force = () => {
      const list = [...nodes.values()];
      const scale = state.alpha;
      for (let i = 0; i < list.length; i += 1) {
        for (let j = i + 1; j < list.length; j += 1) {
          const a = list[i];
          const b = list[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          if (!dx && !dy) {
            dx = Math.random() - 0.5;
            dy = Math.random() - 0.5;
          }
          const d2 = dx * dx + dy * dy;
          const d = Math.sqrt(d2);
          const overlapX = (a.w + b.w) / 2 + SIM.gapX - Math.abs(dx);
          const overlapY = (a.h + b.h) / 2 + SIM.gapY - Math.abs(dy);
          const push = Math.min(SIM.charge / d2, SIM.cap);
          let fx = (dx / d) * push;
          let fy = (dy / d) * push;
          if (overlapX > 0 && overlapY > 0) {
            if (overlapX < overlapY) fx += Math.sign(dx) * overlapX * 0.6;
            else fy += Math.sign(dy) * overlapY * 0.6;
          }
          a.vx -= fx * scale;
          a.vy -= fy * scale;
          b.vx += fx * scale;
          b.vy += fy * scale;
        }
      }
      edges.forEach(({ a, b, length }) => {
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.hypot(dx, dy) || 1;
        const pull = (d - length) * 0.035 * scale;
        a.vx += (dx / d) * pull;
        a.vy += (dy / d) * pull;
        b.vx -= (dx / d) * pull;
        b.vy -= (dy / d) * pull;
      });
      list.forEach((node) => {
        if (node.pinned) {
          node.vx = 0;
          node.vy = 0;
          return;
        }
        const stretch = aspect();
        const nx = node.x / stretch;
        const ny = node.y / SIM.squash;
        const r = Math.hypot(nx, ny) || 1;
        const pull = (RINGS[node.kind] - r) * 0.014 * scale;
        node.vx += (nx / r) * pull * stretch;
        node.vy += (ny / r) * pull * SIM.squash;
        node.vx *= SIM.damping;
        node.vy *= SIM.damping;
        node.x += node.vx;
        node.y += node.vy;
      });
      state.alpha *= SIM.cool;
    };

    const fitView = () => {
      if (!nodes.size || !box.clientWidth) return;
      let [left, top, right, bottom] = [Infinity, Infinity, -Infinity, -Infinity];
      nodes.forEach((node) => {
        left = Math.min(left, node.x - node.w / 2);
        right = Math.max(right, node.x + node.w / 2);
        top = Math.min(top, node.y - node.h / 2);
        bottom = Math.max(bottom, node.y + node.h / 2);
      });
      const width = right - left + BOX.margin * 2;
      const height = bottom - top + BOX.margin * 2;
      target.k = Math.max(BOX.min, Math.min(BOX.fit, box.clientWidth / width, box.clientHeight / height));
      target.x = box.clientWidth / 2 - ((left + right) / 2) * target.k;
      target.y = box.clientHeight / 2 - ((top + bottom) / 2) * target.k;
    };

    const draw = () => {
      nodes.forEach((node) => {
        node.el.style.transform = `translate(${(node.x - node.w / 2).toFixed(1)}px, ${(node.y - node.h / 2).toFixed(1)}px)`;
      });
      edges.forEach(({ a, b, line }) => {
        line.setAttribute("x1", a.x.toFixed(1));
        line.setAttribute("y1", a.y.toFixed(1));
        line.setAttribute("x2", b.x.toFixed(1));
        line.setAttribute("y2", b.y.toFixed(1));
      });
    };
    const frame = () => {
      state.frame = 0;
      if (state.alpha > SIM.floor) force();
      draw();
      let moving = state.alpha > SIM.floor;
      if (state.auto) {
        fitView();
        const gap = Math.abs(target.x - view.x) + Math.abs(target.y - view.y) + Math.abs(target.k - view.k) * 200;
        if (gap > 0.4) {
          view.x += (target.x - view.x) * 0.14;
          view.y += (target.y - view.y) * 0.14;
          view.k += (target.k - view.k) * 0.14;
          moving = true;
        }
        applyView();
      }
      if (moving) state.frame = requestAnimationFrame(frame);
    };
    const kick = (strength = SIM.kick) => {
      state.alpha = Math.max(state.alpha, strength);
      if (reduced) {
        clearTimeout(state.settling);
        state.settling = setTimeout(() => {
          for (let step = 0; step < SIM.settle; step += 1) force();
          state.alpha = 0;
          draw();
          if (state.auto) {
            fitView();
            Object.assign(view, target);
            applyView();
          }
        }, 60);
        return;
      }
      if (!state.frame) state.frame = requestAnimationFrame(frame);
    };
    const refit = () => {
      state.auto = true;
      nodes.forEach(measure);
      if (reduced) {
        fitView();
        Object.assign(view, target);
        applyView();
      } else kick(0.5);
    };
    const zoomAt = (px, py, factor) => {
      const k = Math.max(BOX.min, Math.min(BOX.max, view.k * factor));
      view.x = px - (px - view.x) * (k / view.k);
      view.y = py - (py - view.y) * (k / view.k);
      view.k = k;
      applyView();
    };
    const center = (factor) => {
      state.auto = false;
      zoomAt(box.clientWidth / 2, box.clientHeight / 2, factor);
    };
    tools.append(
      button("Fit", "Fit everything in view", refit),
      button("+", "Zoom in", () => center(BOX.zoomStep)),
      button("-", "Zoom out", () => center(1 / BOX.zoomStep)),
    );

    const focus = () => {
      const node = state.selected || state.hovered;
      box.classList.toggle("has-focus", Boolean(node));
      nodes.forEach((other) => {
        other.el.classList.toggle("is-near", Boolean(node) && (other === node || node.links.has(other.id)));
        other.el.classList.toggle("is-selected", other === state.selected);
      });
      edges.forEach(({ a, b, line }) => line.classList.toggle("is-near", Boolean(node) && (a === node || b === node)));
    };
    const section = (label, value) => {
      if (!value) return null;
      const item = make("p");
      item.append(make("strong", "", `${label}: `), document.createTextNode(value));
      return item;
    };
    const select = (node) => {
      state.selected = node;
      focus();
      detail.hidden = !node;
      detail.replaceChildren();
      if (!node) return;
      const head = make("header");
      const kind = node.kindEl.cloneNode(true);
      const close = make("button", "ibtn");
      close.type = "button";
      close.setAttribute("aria-label", "Close the details");
      close.append(glyph("close"));
      close.addEventListener("click", () => select(null));
      head.append(kind, close);
      const links = [...new Set(node.urls.filter(safe))];
      const parts = [head, make("p", "", node.label)];
      const info = node.info;
      if (node.kind === "company") node.facts.forEach((fact) => parts.push(section(words(fact.kind), fact.text)));
      if (info.network) parts.push(section("Network", words(info.network)));
      if (info.score != null) parts.push(section("Match", Number(info.score).toFixed(2)));
      if (info.snippet) parts.push(section("Snippet", info.snippet));
      if (info.detail) parts.push(section("Why", info.detail));
      if (info.priority) parts.push(section("Priority", info.priority === "must" ? "must have" : "nice to have"));
      if (info.category) parts.push(section("Category", words(info.category)));
      if (info.rating) parts.push(section("Rating", info.rating));
      if (info.scepticism) parts.push(section("Scepticism", info.scepticism));
      if (links.length) {
        const row = make("p", "log__sub", "Sources: ");
        links.forEach((url, index) => {
          if (index) row.append(document.createTextNode(", "));
          row.append(link(url));
        });
        parts.push(row);
      }
      detail.append(...parts.filter(Boolean));
    };

    const nodeFor = (element) => {
      const holder = element && element.closest ? element.closest(".gnode") : null;
      return holder ? nodes.get(holder.dataset.node) : null;
    };
    box.addEventListener("pointerdown", (event) => {
      if (event.target.closest(".graph__tools, .graph__detail, .graph__legend") || event.button > 0) return;
      const node = nodeFor(event.target);
      state.drag = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, node, nx: node ? node.x : 0, ny: node ? node.y : 0, moved: false };
      box.setPointerCapture(event.pointerId);
    });
    box.addEventListener("pointermove", (event) => {
      const drag = state.drag;
      if (!drag) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      state.auto = false;
      box.classList.add("is-dragging");
      if (drag.node) {
        drag.node.pinned = true;
        drag.node.x = drag.nx + dx / view.k;
        drag.node.y = drag.ny + dy / view.k;
        kick(0.25);
        draw();
      } else {
        view.x = drag.vx + dx;
        view.y = drag.vy + dy;
        applyView();
      }
    });
    const release = (event) => {
      const drag = state.drag;
      if (!drag) return;
      state.drag = null;
      box.classList.remove("is-dragging");
      if (box.hasPointerCapture(event.pointerId)) box.releasePointerCapture(event.pointerId);
      if (!drag.moved) select(drag.node);
    };
    box.addEventListener("pointerup", release);
    box.addEventListener("pointercancel", release);
    box.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        state.auto = false;
        const rect = box.getBoundingClientRect();
        zoomAt(event.clientX - rect.left, event.clientY - rect.top, Math.exp(-event.deltaY * BOX.wheel));
      },
      { passive: false },
    );
    box.addEventListener("pointerover", (event) => {
      if (event.pointerType !== "mouse" || state.drag) return;
      const node = nodeFor(event.target);
      if (node === state.hovered) return;
      state.hovered = node;
      focus();
    });
    box.addEventListener("keydown", (event) => event.key === "Escape" && select(null));
    box.addEventListener("pointerleave", () => {
      if (!state.hovered) return;
      state.hovered = null;
      focus();
    });
    new ResizeObserver(() => {
      if (state.auto) refit();
    }).observe(box);

    const subject = addNode("subject", "subject", subjectLabel, { info: {} });
    const sourceNode = (url, label, extra = {}) => {
      const known = nodes.get(url);
      if (known) return known;
      return addNode(url, "source", label || host(url), { urls: [url], info: extra });
    };
    const add = (event) => {
      const data = event.data || {};
      const kind = event.kind;
      if (kind === "company") {
        const company = addNode("company", "company", data.name || "Company");
        company.facts.push({ kind: data.fact_kind, text: event.text });
        if (safe(data.url)) company.urls.push(data.url);
        connect(company, subject, true);
      } else if (kind === "candidate") {
        const key = data.url || event.text;
        const node = addNode(key, "candidate", event.text, { urls: [data.url], info: { network: data.network, score: data.score, snippet: data.snippet } });
        connect(node, subject);
      } else if (kind === "source") {
        const extra = { network: data.network, score: data.score };
        const known = nodes.get(data.url);
        const node = known || sourceNode(data.url, event.text, extra);
        if (known) {
          node.info = { ...node.info, ...extra };
          node.label = event.text;
          refresh(node);
        }
        connect(node, subject);
      } else if (kind === "finding") {
        const node = addNode(`finding-${data.id}`, "finding", event.text, { tag: data.tag, urls: data.urls || [], info: { category: data.category } });
        (data.urls || []).forEach((url) => connect(node, sourceNode(url, host(url))));
      } else if (kind === "requirement") {
        const node = addNode(`requirement-${data.id}`, "requirement", event.text.split(": ")[0], {
          status: data.status,
          urls: data.urls || [],
          info: { detail: data.detail, priority: data.priority },
        });
        (data.urls || []).forEach((url) => connect(node, nodes.get(url)));
        if (!(data.urls || []).length) connect(node, subject, true);
      } else if (kind === "rating" && !data.subject) {
        subject.info.rating = `${Number(data.overall).toFixed(1)} out of 5`;
        subject.label = `${subjectLabel} | ${Number(data.overall).toFixed(1)} / 5`;
        refresh(subject);
      } else if (kind === "scepticism") {
        subject.info.scepticism = event.text;
      } else return;
      kick();
    };
    kick(0.4);
    return { add, refit, legend };
  };

  const mount = (root) => {
    if (root.dataset.mounted) return;
    root.dataset.mounted = "1";
    const title = root.dataset.title || "Search";
    const persist = Boolean(root.dataset.running);
    let view = root.dataset.view === "graph" ? "graph" : "chat";
    root.classList.toggle("is-running", persist);

    const icon = make("span", "live__icon");
    icon.append(taskIcon(persist ? "wait" : "search"));
    const name = make("strong", "live__title", title);
    const stage = make("span", "live__stage", persist ? "Starting" : "Replaying what the agent did");
    const who = make("div", "live__who");
    who.append(name, stage);
    const group = `live-view-${(serial += 1)}`;
    const choose = make("div", "seg seg--small");
    choose.setAttribute("role", "radiogroup");
    choose.setAttribute("aria-label", "Live view");
    const radios = [["chat", "Log"], ["graph", "Graph"]].map(([value, label]) => {
      const item = make("label", "seg__item");
      const input = make("input");
      input.type = "radio";
      input.name = group;
      input.value = value;
      input.checked = value === view;
      item.append(input, make("span", "", label));
      choose.append(item);
      return input;
    });
    const expand = make("button", "ibtn");
    expand.type = "button";
    expand.setAttribute("aria-label", "Expand or shrink");
    expand.append(glyph("expand"));
    const tools = make("div", "live__tools");
    tools.append(choose, expand);
    const head = make("header", "live__head");
    head.append(icon, who, tools);
    const logBox = make("ol", "log");
    logBox.setAttribute("aria-live", "polite");
    const graphBox = make("div", "graph");
    graphBox.tabIndex = 0;
    graphBox.setAttribute("aria-label", "Graph of what the agent found. Drag to move, scroll to zoom.");
    const body = make("div", "live__body");
    body.append(logBox, graphBox);
    root.replaceChildren(head, body);
    const log = createLog(logBox);
    const graph = createGraph(graphBox, title);
    body.append(graph.legend);

    const show = (next) => {
      view = next;
      root.dataset.view = next;
      logBox.hidden = next !== "chat";
      graphBox.hidden = next !== "graph";
      graph.legend.hidden = next !== "graph";
      if (next === "graph") requestAnimationFrame(graph.refit);
    };
    radios.forEach((input) =>
      input.addEventListener("change", () => {
        show(input.value);
        if (persist) postJson("/live-view", { view: input.value }).catch(() => undefined);
      }),
    );
    expand.addEventListener("click", () => {
      root.classList.toggle("is-wide");
      requestAnimationFrame(graph.refit);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && root.classList.contains("is-wide")) {
        root.classList.remove("is-wide");
        requestAnimationFrame(graph.refit);
      }
    });

    const finish = () => {
      root.classList.remove("is-running");
      log.retire();
    };
    const handle = (event) => {
      if (persist) document.dispatchEvent(new CustomEvent("agent:event", { detail: event }));
      if (event.kind === "stage" || event.kind === "step") {
        stage.textContent = event.text;
        icon.replaceChildren(taskIcon(event.task));
      }
      log.add(event);
      graph.add(event);
      if (["done", "failed", "question"].includes(event.kind)) {
        finish();
        stage.textContent = event.kind === "done" ? "Done" : event.text;
        root.dispatchEvent(new CustomEvent("live:end", { bubbles: true, detail: event }));
        if (root.dataset.reload) setTimeout(() => location.reload(), reduced ? 100 : 1100);
      }
    };
    show(view);
    const after = Number(root.dataset.after || 0);
    const feed = new EventSource(`${root.dataset.live}?after=${after}`);
    feed.onmessage = (packet) => handle(JSON.parse(packet.data));
    feed.onerror = () => feed.readyState === EventSource.CLOSED && finish();
    feed.addEventListener("end", () => {
      feed.close();
      finish();
      root.dispatchEvent(new CustomEvent("live:closed", { bubbles: true }));
    });
  };

  const watcher = new IntersectionObserver((entries) =>
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      watcher.unobserve(entry.target);
      mount(entry.target);
    }),
  );
  const scan = (scope = document) => scope.querySelectorAll("[data-live]").forEach((root) => watcher.observe(root));
  window.agent.live = { mount, scan };
  scan();
})();
