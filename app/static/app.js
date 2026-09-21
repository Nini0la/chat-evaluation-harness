const messages = document.querySelector('#messages');
const statusBox = document.querySelector('#status');
const composer = document.querySelector('#composer');
const input = document.querySelector('#message');
const send = document.querySelector('#send');
const newChatButton = document.querySelector('#new-chat');
const gate = document.querySelector('#access-gate');
const reportDialog = document.querySelector('#report-dialog');
const shadowSelects = [...document.querySelectorAll('[data-shadow-slot]')];
let reportTurn = null;
const sessionController = createSessionController({
  storage: localStorage,
  clearHistory: () => messages.replaceChildren(),
});
let testerId = localStorage.getItem('tester_id');
if (!testerId) {
  testerId = crypto.randomUUID();
  localStorage.setItem('tester_id', testerId);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
  });
  if (response.status === 401) {
    gate.classList.remove('hidden');
    throw new Error('Access required');
  }
  if (!response.ok) {
    let body = {};
    try { body = await response.json(); } catch {}
    throw new Error(body.detail || 'Request failed');
  }
  return response.status === 204 ? null : response.json();
}

function feedbackActions(turnId, source) {
  const actions = document.createElement('div');
  actions.className = 'feedback';
  if (source === 'primary') {
    [['Helpful', 1], ['Not helpful', -1]].forEach(([label, rating]) => {
      const button = document.createElement('button');
      button.textContent = label;
      button.onclick = () => feedback(turnId, {rating});
      actions.append(button);
    });
    const report = document.createElement('button');
    report.textContent = 'Report problem';
    report.onclick = () => {
      reportTurn = turnId;
      reportDialog.showModal();
    };
    actions.append(report);
  }
  const candidate = document.createElement('button');
  candidate.textContent = 'Mark eval candidate';
  candidate.onclick = async () => {
    try {
      await api(`/api/turns/${turnId}/eval-candidates`, {
        method: 'POST',
        body: JSON.stringify({source}),
      });
      candidate.disabled = true;
      candidate.textContent = 'Eval candidate marked';
    } catch (error) {
      statusBox.textContent = error.message;
    }
  };
  actions.append(candidate);
  return actions;
}

function bubble(role, text) {
  const box = document.createElement('div');
  box.className = `message ${role}`;
  box.textContent = text;
  messages.append(box);
  return box;
}

function responseCard(label, response, turnId) {
  const card = document.createElement('div');
  card.className = `response-card ${label}`;
  const heading = document.createElement('div');
  heading.className = 'response-label';
  const name = document.createElement('span');
  name.textContent = label;
  const meta = document.createElement('span');
  meta.className = 'response-meta';
  meta.textContent = response.total_latency_ms == null
    ? response.status
    : `${response.status} · ${Math.round(response.total_latency_ms)} ms`;
  heading.append(name, meta);
  const content = document.createElement('div');
  content.textContent = response.assistant_response || '';
  card.append(heading, content);
  if (response.status === 'failed') {
    const error = document.createElement('div');
    error.className = 'response-error';
    error.textContent = response.error_message || 'Model request failed';
    card.append(error);
  }
  if (response.assistant_response) card.append(feedbackActions(turnId, label));
  return card;
}

function renderTurn(turn) {
  bubble('user', turn.user_message);
  const grid = document.createElement('div');
  grid.className = 'response-grid';
  grid.append(responseCard('primary', turn, turn.id));
  turn.shadow_responses.forEach(response => {
    grid.append(responseCard(response.slot, response, turn.id));
  });
  messages.append(grid);
  messages.scrollTop = messages.scrollHeight;
}

async function feedback(turnId, payload) {
  try {
    await api(`/api/turns/${turnId}/feedback`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    statusBox.textContent = 'Feedback saved. Thank you.';
  } catch (error) {
    statusBox.textContent = error.message;
  }
}

function selectedShadows() {
  return shadowSelects
    .filter(select => select.value)
    .map(select => ({
      slot: select.dataset.shadowSlot,
      model_deployment_id: Number(select.value),
    }));
}

function showSessionShadows(session) {
  const assignments = new Map(session.shadows.map(item => [item.slot, item.model_deployment_id]));
  shadowSelects.forEach(select => {
    select.value = String(assignments.get(select.dataset.shadowSlot) || '');
  });
}

async function loadDeployments() {
  const deployments = await api('/api/deployments');
  shadowSelects.forEach(select => {
    const selected = select.value;
    select.replaceChildren(new Option('None', ''));
    deployments.filter(item => !item.active).forEach(item => {
      select.add(new Option(`${item.model_id} / ${item.model_version} (${item.provider})`, item.id));
    });
    select.value = selected;
  });
}

async function newSession() {
  return sessionController.replaceSession(() => api('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({
      anonymous_tester_id: testerId,
      client_metadata: {user_agent: navigator.userAgent, locale: navigator.language},
      shadows: selectedShadows(),
    }),
  }));
}

async function start() {
  try {
    await loadDeployments();
    if (sessionController.sessionId) {
      const [session, turns] = await Promise.all([
        api(`/api/sessions/${sessionController.sessionId}`),
        api(`/api/sessions/${sessionController.sessionId}/turns`),
      ]);
      showSessionShadows(session);
      turns.forEach(renderTurn);
    }
    gate.classList.add('hidden');
  } catch (error) {
    if (error.message !== 'Access required') {
      sessionController.clearSession();
      statusBox.textContent = error.message;
    }
  }
}

composer.addEventListener('submit', async event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  send.disabled = true;
  newChatButton.disabled = true;
  statusBox.className = 'status';
  statusBox.textContent = 'Models are responding...';
  try {
    if (!sessionController.sessionId) await newSession();
    const requestSessionId = sessionController.sessionId;
    const optimisticMessage = bubble('user', text);
    const turn = await api(`/api/sessions/${requestSessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({message: text}),
    });
    if (sessionController.sessionId !== requestSessionId) return;
    optimisticMessage.remove();
    renderTurn(turn);
    statusBox.textContent = 'Responses completed.';
  } catch (error) {
    statusBox.textContent = `Could not get a response: ${error.message}`;
    statusBox.className = 'status error';
  } finally {
    send.disabled = false;
    newChatButton.disabled = false;
    input.focus();
  }
});

newChatButton.onclick = async () => {
  try {
    await newSession();
    statusBox.textContent = 'Started a new chat.';
  } catch (error) {
    statusBox.textContent = error.message;
  }
};

document.querySelector('#access-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  const response = await fetch('/access', {method: 'POST', body: form});
  if (response.ok) {
    gate.classList.add('hidden');
    document.querySelector('#access-error').textContent = '';
    await start();
  } else {
    document.querySelector('#access-error').textContent = 'Invalid access code.';
  }
});

document.querySelector('#report-form').addEventListener('submit', async event => {
  if (event.submitter?.value === 'cancel') return;
  event.preventDefault();
  const data = new FormData(event.target);
  await feedback(reportTurn, {
    failure_category: data.get('failure_category') || null,
    comment: data.get('comment') || null,
  });
  reportDialog.close();
  event.target.reset();
});

start();
