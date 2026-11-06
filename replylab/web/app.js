const $ = (selector) => document.querySelector(selector);
const api = "/api/sessions";
const state = {
  sessionId: "", branchId: "", session: null, view: null, run: null, draft: null,
  stream: null, sequence: -1, selection: 0, refreshPending: false,
};
let forkTarget;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(label, action, className) {
  const node = element("button", label, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
  return payload;
}

function showError(error, target = $("#error")) {
  target.textContent = error ? error.message : "";
  target.hidden = !error;
}

function sessionPath() {
  return api + "/" + state.sessionId;
}

function branchPath() {
  return sessionPath() + "/branches/" + state.branchId;
}

function short(id) {
  return id.slice(0, 8);
}

function branchName(branch) {
  return branch.kind === "root" ? "Original" : "Fork " + short(branch.id);
}

function seatLabel(seat) {
  return seat.kind === "human" ? "Human" : seat.model;
}

async function listSessions() {
  const sessions = await request(api);
  const options = [new Option("Choose a session", "")];
  for (const session of sessions) {
    const label = seatLabel(session.seats.A) + " / " + seatLabel(session.seats.B);
    options.push(new Option(label + " · " + short(session.id), session.id));
  }
  $("#sessions").replaceChildren(...options);
  $("#sessions").value = state.sessionId;
}

async function selectSession(id, branchId = "") {
  state.stream?.close();
  const selection = ++state.selection;
  state.sessionId = id;
  state.branchId = branchId;
  state.session = state.view = state.run = state.draft = null;
  state.sequence = -1;
  state.refreshPending = false;
  showError(null);
  $("#message").value = "";
  $("#connection").textContent = "Local workspace";
  render();
  if (!id) {
    location.hash = "";
    return;
  }
  const data = await request(sessionPath());
  if (selection !== state.selection) return;
  if (!data.branches.some((branch) => branch.id === state.branchId)) state.branchId = data.branches[0].id;
  state.session = data;
  state.run = data.run;
  state.draft = data.draft;
  setLocation();
  connect();
  await refresh();
}

function setLocation() {
  history.replaceState(null, "", "#" + state.sessionId + "/" + state.branchId);
}

async function selectBranch(id) {
  if (id === state.branchId) return;
  state.branchId = id;
  state.view = null;
  $("#message").value = "";
  setLocation();
  render();
  await refresh();
}

async function refresh() {
  if (!state.sessionId) return;
  if (state.refreshPending) {
    state.refreshPending.again = true;
    return state.refreshPending.promise;
  }
  const job = {again: false};
  state.refreshPending = job;
  const selection = state.selection;
  job.promise = (async () => {
    do {
      job.again = false;
      const branchId = state.branchId;
      try {
        const [session, view] = await Promise.all([request(sessionPath()), request(branchPath())]);
        if (selection !== state.selection) return;
        if (branchId !== state.branchId) {
          job.again = true;
          continue;
        }
        state.session = session;
        state.view = view;
        render();
      } catch (error) {
        if (selection === state.selection) showError(error);
      }
    } while (job.again);
  })().finally(() => {
    if (state.refreshPending === job) state.refreshPending = false;
  });
  return job.promise;
}

function connect() {
  const stream = new EventSource(sessionPath() + "/events");
  state.stream = stream;
  stream.onerror = () => {
    if (state.stream === stream) $("#connection").textContent = "Reconnecting…";
  };
  stream.onmessage = ({data}) => {
    if (state.stream !== stream) return;
    const event = JSON.parse(data);
    if (event.type === "snapshot") {
      state.sequence = event.sequence;
      state.session = event;
      state.run = event.run;
      state.draft = event.draft;
      $("#connection").textContent = "Live";
      render();
      void refresh();
      return;
    }
    if (event.sequence <= state.sequence) return;
    state.sequence = event.sequence;
    switch (event.type) {
      case "run_started":
        state.run = {id: event.run_id, branch_id: event.branch_id};
        renderControls();
        break;
      case "attempt_started":
        state.draft = {id: event.attempt_id, branch_id: event.branch_id, seat: event.seat, text: ""};
        renderDraft();
        break;
      case "text_delta":
        if (state.draft?.id === event.attempt_id) {
          state.draft.text += event.text;
          renderDraft();
        }
        return;
      case "attempt_ended":
        if (state.draft?.id === event.attempt_id) {
          state.draft = null;
          renderDraft();
        }
        break;
      case "run_ended":
        if (state.run?.id === event.run_id) state.run = null;
        renderControls();
        break;
    }
    void refresh();
  };
}

function render() {
  $("#empty").hidden = Boolean(state.sessionId);
  $("#chat").hidden = !state.view;
  $("#fork-start").hidden = !state.view;
  if (!state.session || !state.view) {
    if (!state.session) $("#branches").replaceChildren();
    return;
  }
  const {branch, turns, next_seat: nextSeat, attempts} = state.view;
  const setup = state.session.session;
  $("#branches").replaceChildren(...state.session.branches.map((item) => {
    const tab = button(branchName(item), () => selectBranch(item.id), "branch");
    tab.setAttribute("aria-current", String(item.id === branch.id));
    if (item.id === state.run?.branch_id) tab.append(element("small", "Running"));
    return tab;
  }));
  $("#branch-title").textContent = branchName(branch);
  let lineage = "";
  if (branch.source_id) {
    const source = state.session.branches.find((item) => item.id === branch.source_id);
    lineage = "From " + (source ? branchName(source) : short(branch.source_id));
    if (branch.kind === "start") lineage += " · Start of chat";
    else if (branch.kind === "edit") lineage += " · Edited turn " + short(branch.anchor.turn_id);
    else if (branch.anchor.attempt_id) lineage += " · Partial " + short(branch.anchor.attempt_id) + ", offset " + branch.anchor.offset;
    else lineage += " · Turn " + short(branch.anchor.turn_id) + ", offset " + branch.anchor.offset;
  }
  $("#branch-source").textContent = lineage;
  $("#seats").replaceChildren(...["A", "B"].map((seat) =>
    element("span", seat + " · " + seatLabel(setup.seats[seat]), seat === nextSeat ? "seat current" : "seat")));
  $("#setup").hidden = setup.opening_prompt === null;
  if (setup.opening_prompt !== null) {
    $("#setup").replaceChildren(element("p", "Opening prompt for " + setup.starter, "section-label"),
      element("p", setup.opening_prompt, "message-text"));
  }
  $("#turns").replaceChildren(...turns.map(renderTurn));
  if (!turns.length) $("#turns").append(element("p", "No turns yet.", "muted"));
  const uncommitted = attempts.filter((attempt) => !["complete", "active"].includes(attempt.status));
  $("#attempts").hidden = !uncommitted.length;
  $("#attempt-list").replaceChildren(...uncommitted.map((attempt) =>
    button(attempt.seat + " · " + attempt.status + " · " + reason(attempt.reason), () => inspect(attempt.id))));
  renderDraft();
  renderControls();
}

function renderTurn(turn, index) {
  const node = element("article", "", "turn");
  node.dataset.seat = turn.seat;
  const head = element("div", "", "turn-head");
  head.append(element("strong", (index + 1) + " · Seat " + turn.seat));
  head.append(button("Replay", () => openFork(turn, "cursor")));
  head.append(button("Edit and replay", () => openFork(turn, "edit")));
  if (turn.attempt_id) head.append(button("Inspect", () => inspect(turn.attempt_id)));
  node.append(head, element("p", turn.text, "message-text"));
  return node;
}

function renderDraft() {
  const draft = state.draft;
  const container = $("#draft");
  container.hidden = !draft || draft.branch_id !== state.branchId;
  if (container.hidden) {
    container.replaceChildren();
    return;
  }
  if (container.dataset.id !== draft.id || !container.firstChild) {
    container.dataset.id = draft.id;
    container.replaceChildren(element("p", "Seat " + draft.seat + " · Streaming", "section-label"),
      element("p", "", "message-text"),
      button("Inspect request", () => inspect(draft.id), "quiet"));
  }
  container.querySelector(".message-text").textContent = draft.text;
}

function reason(code) {
  const labels = {
    missing_openai_api_key: "Set OPENAI_API_KEY on the service to run this seat",
    context_limit: "The transcript exceeds the model context limit",
  };
  return labels[code] || (code || "").replaceAll("_", " ");
}

function renderControls() {
  const run = state.run;
  $("#stop").disabled = !run;
  $("#next").disabled = $("#play").disabled = Boolean(run);
  if (!state.view) return;
  const seat = state.view.next_seat;
  const human = state.session.session.seats[seat].kind === "human";
  const last = state.view.last_run;
  const status = run
    ? "Running on " + (run.branch_id === state.branchId ? "this branch" : "branch " + short(run.branch_id))
    : "Seat " + seat + " is next" + (last?.reason ? " · Last run: " + reason(last.reason) : "");
  $("#status").textContent = status;
  $("#composer").hidden = !human || Boolean(run);
  $("#compose-label").textContent = "Message from seat " + seat;
}

async function action(name, body = {}) {
  showError(null);
  try {
    const result = await request(branchPath() + "/" + name, {revision: state.view.branch.revision, ...body});
    if (result.status === "human_input") $("#message").focus();
    await refresh();
    return result;
  } catch (error) {
    showError(error);
    await refresh();
    return null;
  }
}

async function inspect(id) {
  const selection = state.selection;
  try {
    const attempt = await request(sessionPath() + "/attempts/" + id);
    if (selection !== state.selection) return;
    $("#attempt-status").textContent = "Seat " + attempt.seat + " · " + attempt.status +
      (attempt.reason ? " · " + reason(attempt.reason) : "");
    $("#attempt-output").textContent = attempt.text || "No text received.";
    $("#attempt-request").textContent = JSON.stringify(attempt.request, null, 2);
    $("#metadata-heading").hidden = $("#attempt-metadata").hidden = !attempt.metadata;
    $("#attempt-metadata").textContent = JSON.stringify(attempt.metadata, null, 2);
    const partial = $("#fork-partial");
    partial.hidden = ["complete", "active"].includes(attempt.status) || !attempt.text || attempt.branch_id !== state.branchId;
    partial.onclick = () => {
      $("#attempt-dialog").close();
      openFork(attempt, "cursor", true);
    };
    $("#attempt-dialog").showModal();
  } catch (error) {
    showError(error);
  }
}

function codePointOffset(raw, normalizedOffset) {
  // Textareas collapse CRLF to one UTF-16 code unit.
  let units = 0;
  let points = 0;
  for (let i = 0; i < raw.length && units < normalizedOffset; points++) {
    const length = raw.codePointAt(i) > 0xffff ? 2 : 1;
    if (units + length > normalizedOffset) break;
    units += length;
    if (raw[i] === "\r" && raw[i + 1] === "\n") {
      i += 2;
      points += 1;
    } else {
      i += length;
    }
  }
  return points;
}

function openFork(source, kind, partial = false) {
  forkTarget = {source, kind, partial, path: branchPath()};
  const field = $("#fork-text");
  field.value = source.text;
  field.readOnly = kind === "cursor";
  $("#fork-title").textContent = kind === "edit" ? "Edit and replay" : "Replay from a cursor";
  $("#fork-help").textContent = kind === "edit"
    ? "The edited message stays with seat " + source.seat + ". The other seat speaks next on the new branch."
    : "Place the cursor where the other seat should receive the end of this message.";
  $("#fork-confirm").textContent = kind === "edit" ? "Create edited branch" : "Reply from here";
  $("#replay-before").hidden = kind === "edit";
  $("#replay-after").hidden = kind === "edit" || partial;
  $("#cursor").hidden = kind === "edit";
  showError(null, $("#fork-form .form-error"));
  $("#fork-dialog").showModal();
  field.setSelectionRange(kind === "edit" ? 0 : field.value.length, field.value.length);
  updateCursor();
}

function updateCursor() {
  if (!forkTarget || forkTarget.kind !== "cursor") return;
  const offset = codePointOffset(forkTarget.source.text, $("#fork-text").selectionStart);
  $("#cursor").textContent = "Character offset " + offset;
}

async function submitFork(offset) {
  const target = forkTarget;
  const anchor = target.partial ? {attempt_id: target.source.id} : {turn_id: target.source.id};
  const body = target.kind === "edit"
    ? {kind: "edit", ...anchor, text: $("#fork-text").value}
    : {kind: "cursor", ...anchor, offset};
  const submit = $("#fork-confirm");
  submit.disabled = $("#replay-before").disabled = $("#replay-after").disabled = true;
  try {
    const result = await request(target.path + "/fork", body);
    $("#fork-dialog").close();
    await selectBranch(result.branch.id);
  } catch (error) {
    showError(error, $("#fork-form .form-error"));
  } finally {
    submit.disabled = $("#replay-before").disabled = $("#replay-after").disabled = false;
  }
}

function createDialog() {
  showError(null, $("#create-form .form-error"));
  $("#create-dialog").showModal();
}

for (const seat of ["A", "B"]) {
  const fields = $("#seat-template").content.cloneNode(true).firstElementChild;
  fields.dataset.seat = seat;
  fields.querySelector("legend").textContent = "Seat " + seat;
  const adapter = fields.querySelector("[name=adapter]");
  adapter.addEventListener("change", () => {
    fields.querySelector(".model-fields").hidden = adapter.value === "human";
    fields.querySelector("[name=model]").placeholder = adapter.value === "fake" ? "echo" : "Model ID";
  });
  $("#seat-fields").append(fields);
}

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const seats = {};
    for (const fields of $("#seat-fields").children) {
      const get = (name) => fields.querySelector("[name=" + name + "]").value;
      const adapter = get("adapter");
      seats[fields.dataset.seat] = adapter === "human" ? {kind: "human"} : {
        kind: "ai", adapter, model: get("model") || (adapter === "fake" ? "echo" : ""),
        instructions: get("instructions"), options: JSON.parse(get("options") || "{}"),
      };
    }
    const form = new FormData(event.currentTarget);
    const result = await request(api, {seats, starter: form.get("starter"), opening_prompt: form.get("prompt") || null});
    $("#create-dialog").close();
    await selectSession(result.session_id, result.branch_id);
    await listSessions();
  } catch (error) {
    showError(error, $("#create-form .form-error"));
  } finally {
    submit.disabled = false;
  }
});

$("#new-session").onclick = $("#empty-create").onclick = createDialog;
$("#sessions").onfocus = () => listSessions().catch(showError);
$("#sessions").onchange = () => selectSession($("#sessions").value).catch(showError);
$("#next").onclick = () => action("next");
$("#play").onclick = () => {
  if ($("#limit").reportValidity()) void action("play", {limit: Number($("#limit").value)});
};
$("#stop").onclick = async () => {
  if (!state.run) return;
  showError(null);
  try {
    await request(sessionPath() + "/runs/" + state.run.id + "/stop", {});
    await refresh();
  } catch (error) {
    showError(error);
  }
};
$("#composer").onsubmit = async (event) => {
  event.preventDefault();
  const message = $("#message");
  const submitted = message.value;
  if (await action("send", {seat: state.view.next_seat, text: submitted}) && message.value === submitted) {
    message.value = "";
  }
};
$("#fork-start").onclick = async () => {
  const result = await action("fork", {kind: "start"});
  if (result) await selectBranch(result.branch.id);
};
$("#fork-text").addEventListener("selectionchange", updateCursor);
$("#fork-text").addEventListener("keyup", updateCursor);
$("#fork-text").addEventListener("click", updateCursor);
$("#replay-before").onclick = () => submitFork(0);
$("#replay-after").onclick = () => submitFork([...forkTarget.source.text].length);
$("#fork-form").onsubmit = (event) => {
  event.preventDefault();
  void submitFork(codePointOffset(forkTarget.source.text, $("#fork-text").selectionStart));
};
for (const close of document.querySelectorAll("[data-close]")) {
  close.onclick = () => close.closest("dialog").close();
}

try {
  const [id, branch] = location.hash.slice(1).split("/");
  if (/^[a-f0-9]{32}$/.test(id)) await selectSession(id, branch);
  await listSessions();
} catch (error) {
  showError(error);
}
