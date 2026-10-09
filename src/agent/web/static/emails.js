// src/agent/web/static/emails.js
"use strict";
(() => {
  const form = document.querySelector("[data-email-form]");
  if (!form) return;
  const out = document.querySelector("[data-drafts]");
  const failure = document.querySelector("[data-preview-error]");
  const failureText = document.querySelector("[data-preview-message]");
  const body = form.elements.body;
  const sets = form.querySelectorAll("[data-set]");
  const DELAY = 350;
  let timer = 0;
  let current = [];
  let sequence = 0;

  const tell = (text) => document.dispatchEvent(new CustomEvent("toast", { detail: text }));
  const copy = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      tell("Copied to the clipboard.");
    } catch {
      tell("Copy is not available here.");
    }
  };
  const button = (label, primary, action) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `btn btn--small${primary ? " btn--primary" : ""}`;
    item.textContent = label;
    item.addEventListener("click", action);
    return item;
  };
  const card = (draft) => {
    const article = document.createElement("article");
    article.className = "draft";
    const head = document.createElement("header");
    const name = document.createElement("strong");
    name.textContent = draft.recipient || "Draft";
    const actions = document.createElement("div");
    actions.className = "draft__actions";
    actions.append(
      button("Copy subject", false, () => copy(draft.subject)),
      button("Copy text", false, () => copy(draft.body)),
      button("Copy all", true, () => copy(`Subject: ${draft.subject}\n\n${draft.body}`)),
    );
    head.append(name, actions);
    const subject = document.createElement("p");
    subject.className = "draft__subject";
    subject.textContent = draft.subject;
    const text = document.createElement("pre");
    text.className = "draft__body";
    text.textContent = draft.body;
    article.append(head, subject, text);
    return article;
  };
  const payload = () => ({
    category: form.elements.category.value,
    purpose: form.elements.purpose.value,
    subject: form.elements.subject.value,
    body: body.value,
    job: form.elements.job.value,
    names: form.elements.names.value.split("\n").map((item) => item.trim()).filter(Boolean),
  });
  const run = async () => {
    const mine = ++sequence;
    if (!form.elements.subject.value.trim() && !body.value.trim()) {
      failure.hidden = true;
      return;
    }
    try {
      const response = await fetch(form.dataset.preview, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.body.dataset.csrf },
        body: JSON.stringify(payload()),
      });
      const data = await response.json();
      if (mine !== sequence) return;
      failure.hidden = response.ok;
      if (!response.ok) {
        failureText.textContent = data.error || "The drafts could not be created.";
        return;
      }
      current = data.drafts;
      out.replaceChildren(...current.map(card));
    } catch {
      failure.hidden = false;
      failureText.textContent = "The drafts could not be created.";
    }
  };
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(run, DELAY);
  };
  const showFields = () => {
    const chosen = form.elements.category.value;
    sets.forEach((set) => (set.hidden = set.dataset.set !== chosen));
  };

  form.querySelectorAll("[data-trigger]").forEach((input) => input.addEventListener("input", refresh));
  form.querySelectorAll('input[type="hidden"]').forEach((input) => input.addEventListener("change", refresh));
  form.querySelectorAll('input[name="category"]').forEach((input) =>
    input.addEventListener("change", () => {
      showFields();
      refresh();
    }),
  );
  form.querySelectorAll("[data-insert]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const token = `{{ ${chip.dataset.insert} }}`;
      body.setRangeText(token, body.selectionStart, body.selectionEnd, "end");
      body.focus();
      refresh();
    }),
  );
  document.querySelector("[data-copy-all]").addEventListener("click", () => {
    if (!current.length) return tell("There is nothing to copy yet.");
    copy(current.map((draft) => `To: ${draft.recipient}\nSubject: ${draft.subject}\n\n${draft.body}`).join("\n\n----------\n\n"));
  });
  showFields();
  run();
})();
