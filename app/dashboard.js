const state = {
  user: null,
  guilds: [],
  guildId: null,
  forms: [],
  form: null,
  options: { channels: [], roles: [] },
  demo: false,
  dirty: false,
  toastTimer: null,
};

const byId = (id) => document.getElementById(id);
const api = async (url, options = {}) => {
  if (state.demo) return demoApi(url, options);
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === 'string' ? payload : payload.error || payload.message;
    throw new Error(message || `Error HTTP ${response.status}`);
  }
  return payload;
};

function demoApi(url, options = {}) {
  const path = new URL(url, location.origin).pathname;
  const match = path.match(/^\/api\/guilds\/(\d+)(?:\/forms(?:\/(\d+))?(?:\/(submissions|attachments|publish))?)?(?:\/options)?$/);
  if (!match) return Promise.reject(new Error('Esta acción no está disponible en la vista de demostración.'));
  const guildId = match[1];
  const formId = match[2] ? Number(match[2]) : null;
  const suffix = match[3] || '';
  if (path.endsWith('/options')) return Promise.resolve(state.options);
  if (path.endsWith('/forms') && (!options.method || options.method === 'GET')) {
    return Promise.resolve({ forms: state.forms, limits: { forms: 10, questions: 50 }, premium: true });
  }
  if (path.endsWith('/forms') && options.method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    const form = { id: Date.now(), name: body.name, log_channel_id: '', message_channel_id: '', published_message_id: '', questions: [], accept_role_ids: [], reject_role_ids: [], message: { content: '', embeds: [], attachments: [], components: [] } };
    state.forms.push(form);
    return Promise.resolve({ id: form.id, name: form.name });
  }
  const form = state.forms.find((item) => item.id === formId);
  if (!form && formId !== null) return Promise.reject(new Error('Formulario demo no encontrado.'));
  if (suffix === 'submissions') return Promise.resolve({ submissions: [{ id: 1, user_id: '701245889034112100', username: 'Ejemplo · Applicant', status: 'pending', review_reason: '', created_at: '2026-10-01 12:30:00' }] });
  if (suffix === 'attachments' && options.method === 'POST') {
    const body = options.body;
    const files = [...body.values()].filter((value) => value instanceof File);
    return Promise.resolve({ attachments: files.map((file) => ({ id: crypto.randomUUID().replaceAll('-', ''), name: file.name, size: file.size })) });
  }
  if (suffix === 'publish') return Promise.resolve({ ok: true, message_id: 'preview-only', jump_url: '#' });
  if (options.method === 'DELETE') {
    state.forms = state.forms.filter((item) => item.id !== formId);
    return Promise.resolve({ ok: true });
  }
  if (options.method === 'PUT') {
    const body = JSON.parse(options.body || '{}');
    Object.assign(form, body);
    return Promise.resolve({ ok: true });
  }
  return Promise.resolve(structuredClone(form));
}

function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function toLocalDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function safeMarkdown(value = '') {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/gs, '<strong>$1</strong>')
    .replace(/__(.+?)__/gs, '<u>$1</u>')
    .replace(/~~(.+?)~~/gs, '<s>$1</s>')
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
}

function showToast(message, error = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.toggle('error', error);
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { toast.hidden = true; }, 3600);
}

function setDirty(dirty = true) {
  state.dirty = dirty;
  byId('save-form').disabled = !state.form || !dirty;
  byId('save-status').textContent = dirty ? 'Cambios sin guardar' : 'Guardado';
  if (state.form) renderFormList();
}

function ensureMessage() {
  state.form.message ||= { content: '', embeds: [], attachments: [], components: [] };
  state.form.message.content ||= '';
  state.form.message.embeds ||= [];
  state.form.message.attachments ||= [];
  state.form.message.components ||= [];
}

async function start() {
  if (new URLSearchParams(location.search).get('demo') === '1') {
    startDemo();
    return;
  }
  try {
    const session = await api('/api/session');
    if (!session.authenticated) {
      byId('login-screen').hidden = false;
      return;
    }
    state.user = session.user;
    byId('dashboard').hidden = false;
    renderUser();
    await loadGuilds();
  } catch (error) {
    byId('login-screen').hidden = false;
    byId('login-error').textContent = error.message;
    byId('login-error').hidden = false;
  }
}

function startDemo() {
  state.demo = true;
  state.user = { id: '0', username: 'Vista de demostración', avatar: null };
  state.guilds = [{ id: '120000000000000001', name: 'Andorra Roleplay' }, { id: '120000000000000002', name: 'Sentra Training' }];
  state.guildId = state.guilds[0].id;
  state.options = {
    channels: [
      { id: '120000000000000101', name: 'anuncios', category: 'COMUNIDAD' },
      { id: '120000000000000102', name: 'solicitudes-staff', category: 'SEGURIDAD' },
      { id: '120000000000000103', name: 'staff-privado', category: 'SEGURIDAD' },
    ],
    roles: [
      { id: '120000000000000201', name: 'Moderador', color: 0x286d5c },
      { id: '120000000000000202', name: 'Seguridad', color: 0xc8313c },
      { id: '120000000000000203', name: 'Supervisor', color: 0x9a6813 },
    ],
  };
  state.forms = [
    {
      id: 1,
      name: 'Ingreso a seguridad',
      log_channel_id: '120000000000000102',
      message_channel_id: '120000000000000101',
      published_message_id: '',
      questions: [
        { position: 1, prompt: '¿Por qué quieres formar parte del equipo de seguridad?' },
        { position: 2, prompt: '¿Qué experiencia tienes moderando comunidades?' },
        { position: 3, prompt: '¿Cómo actuarías ante un conflicto entre dos miembros?' },
      ],
      accept_role_ids: ['120000000000000203'],
      reject_role_ids: ['120000000000000202'],
      message: {
        content: '## Convocatoria abierta\nBuscamos personas responsables para reforzar el equipo. Completa la solicitud con sinceridad.',
        embeds: [{ author: { name: 'Sentra Security', url: '', icon_url: '' }, title: 'Solicitud de ingreso', description: 'Cuéntanos sobre tu experiencia y disponibilidad. El equipo revisará tu solicitud y te responderá por mensaje directo.', url: '', color: '#C8313C', image: '', thumbnail: '', footer: { text: 'Andorra Roleplay · Equipo de Seguridad', icon_url: '' }, timestamp: '', fields: [{ name: 'Requisitos', value: 'Respeto · Actividad · Criterio', inline: true }, { name: 'Revisión', value: 'Respuesta privada por Discord', inline: true }] }],
        attachments: [],
        components: [{ type: 'button', action: 'apply', label: 'Iniciar solicitud', style: 'success' }],
      },
    },
    {
      id: 2,
      name: 'Reporte de incidente',
      log_channel_id: '120000000000000102',
      message_channel_id: '120000000000000101',
      published_message_id: '',
      questions: [{ position: 1, prompt: 'Describe brevemente qué ocurrió.' }, { position: 2, prompt: '¿Cuándo sucedió y quiénes participaron?' }],
      accept_role_ids: ['120000000000000202'],
      reject_role_ids: [],
      message: { content: 'Utiliza este formulario para reportar un incidente. Tus respuestas serán visibles solo para el staff autorizado.', embeds: [], attachments: [], components: [{ type: 'button', action: 'apply', label: 'Crear reporte', style: 'primary' }] },
    },
  ];
  byId('dashboard').hidden = false;
  byId('login-screen').hidden = true;
  byId('demo-badge').hidden = false;
  renderUser();
  byId('guild-select').innerHTML = state.guilds.map((guild) => `<option value="${escapeHtml(guild.id)}">${escapeHtml(guild.name)}</option>`).join('');
  renderFormList();
  selectForm(state.forms[0].id).catch((error) => showToast(error.message, true));
}

function renderUser() {
  const summary = byId('user-summary');
  const avatar = state.user.avatar
    ? `<img class="user-avatar" src="https://cdn.discordapp.com/avatars/${encodeURIComponent(state.user.id)}/${encodeURIComponent(state.user.avatar)}.png?size=64" alt="">`
    : `<span class="user-avatar user-avatar-fallback">${escapeHtml((state.user.username || '?').slice(0, 1).toUpperCase())}</span>`;
  summary.innerHTML = `${avatar}<span>${escapeHtml(state.user.username)}</span>`;
}

async function loadGuilds() {
  state.guilds = await api('/api/guilds');
  const select = byId('guild-select');
  select.innerHTML = state.guilds.map((guild) => `<option value="${escapeHtml(guild.id)}">${escapeHtml(guild.name)}</option>`).join('');
  if (!state.guilds.length) {
    byId('empty-state').innerHTML = '<p class="eyebrow">SIN SERVIDORES DISPONIBLES</p><h1>El bot aún no está conectado</h1><p class="muted">Añade Sentra Security a un servidor en el que seas owner o administrador.</p>';
    byId('empty-state').hidden = false;
    return;
  }
  state.guildId = state.guilds[0].id;
  await loadGuild();
}

async function loadGuild() {
  state.form = null;
  state.options = await api(`/api/guilds/${encodeURIComponent(state.guildId)}/options`);
  const result = await api(`/api/guilds/${encodeURIComponent(state.guildId)}/forms`);
  state.forms = result.forms;
  byId('plan-badge').textContent = result.premium ? 'PREMIUM' : 'GRATUITO';
  byId('plan-badge').classList.toggle('premium', Boolean(result.premium));
  byId('plan-badge').title = `${result.limits.forms} formularios · ${result.limits.questions} preguntas por formulario`;
  renderFormList();
  if (state.forms.length) await selectForm(state.forms[0].id);
  else showEmptyEditor();
}

function renderFormList() {
  const list = byId('form-list');
  list.innerHTML = state.forms.map((form) => `
    <button class="form-item ${state.form?.id === form.id ? 'is-active' : ''}" data-form-id="${form.id}" type="button">
      <span class="form-item-name">${escapeHtml(form.name)}</span>
      <span class="form-item-count">${form.questions?.length ?? ''}</span>
    </button>`).join('');
  list.querySelectorAll('[data-form-id]').forEach((button) => button.addEventListener('click', async () => {
    if (state.dirty && !confirm('Hay cambios sin guardar. ¿Descartarlos?')) return;
    await selectForm(Number(button.dataset.formId));
  }));
}

function showEmptyEditor() {
  state.form = null;
  byId('editor').hidden = true;
  byId('empty-state').hidden = false;
  byId('breadcrumb-form').textContent = 'Sin formularios';
  byId('save-form').disabled = true;
  byId('publish-form').disabled = true;
}

async function selectForm(formId) {
  const form = await api(`/api/guilds/${encodeURIComponent(state.guildId)}/forms/${formId}`);
  state.form = form;
  ensureMessage();
  state.dirty = false;
  byId('empty-state').hidden = true;
  byId('editor').hidden = false;
  byId('save-form').disabled = true;
  byId('publish-form').disabled = false;
  byId('breadcrumb-form').textContent = form.name;
  byId('editor-title').textContent = form.name;
  fillSelect(byId('log-channel'), state.options.channels, form.log_channel_id, 'Sin canal de logs');
  fillSelect(byId('message-channel'), state.options.channels, form.message_channel_id, 'Selecciona canal para publicar');
  fillRoles(byId('accept-roles'), form.accept_role_ids);
  fillRoles(byId('reject-roles'), form.reject_role_ids);
  byId('form-name').value = form.name;
  byId('form-name').oninput = () => { form.name = byId('form-name').value; byId('editor-title').textContent = form.name || 'Formulario'; byId('breadcrumb-form').textContent = form.name || 'Formulario'; setDirty(); };
  byId('log-channel').onchange = () => { form.log_channel_id = byId('log-channel').value; setDirty(); };
  byId('message-channel').onchange = () => { form.message_channel_id = byId('message-channel').value; setDirty(); };
  byId('accept-roles').onchange = () => { form.accept_role_ids = selectedValues(byId('accept-roles')); setDirty(); };
  byId('reject-roles').onchange = () => { form.reject_role_ids = selectedValues(byId('reject-roles')); setDirty(); };
  renderQuestions();
  renderMessage();
  renderSubmissions();
  renderFormList();
}

function fillSelect(element, options, selected, emptyLabel) {
  element.innerHTML = `<option value="">${escapeHtml(emptyLabel)}</option>` + options.map((option) =>
    `<option value="${escapeHtml(option.id)}" ${String(option.id) === String(selected || '') ? 'selected' : ''}>${escapeHtml(option.category ? `${option.category} / ${option.name}` : option.name)}</option>`
  ).join('');
}

function fillRoles(element, selected) {
  const selectedSet = new Set((selected || []).map(String));
  element.innerHTML = state.options.roles.map((role) =>
    `<option value="${escapeHtml(role.id)}" ${selectedSet.has(String(role.id)) ? 'selected' : ''}>${escapeHtml(role.name)}</option>`
  ).join('');
}

function selectedValues(element) {
  return [...element.selectedOptions].map((option) => option.value);
}

function renderQuestions() {
  const container = byId('question-list');
  const limits = byId('plan-badge').classList.contains('premium') ? 50 : 20;
  byId('question-limit').textContent = `${state.form.questions.length} / ${limits} preguntas`;
  byId('add-question').disabled = state.form.questions.length >= limits;
  container.replaceChildren();
  if (!state.form.questions.length) {
    container.innerHTML = '<div class="question-empty">Todavía no hay preguntas. Añade la primera para habilitar solicitudes.</div>';
    return;
  }
  state.form.questions.forEach((question, index) => {
    const row = document.createElement('div');
    row.className = 'question-row';
    const number = document.createElement('span');
    number.className = 'question-position';
    number.textContent = String(index + 1).padStart(2, '0');
    const input = document.createElement('input');
    input.className = 'question-input';
    input.maxLength = 1000;
    input.value = question.prompt;
    input.setAttribute('aria-label', `Pregunta ${index + 1}`);
    input.addEventListener('input', () => { question.prompt = input.value; setDirty(); });
    const controls = document.createElement('div');
    controls.className = 'question-controls';
    controls.append(
      makeSmallButton('↑', 'Subir pregunta', () => moveQuestion(index, -1), index === 0),
      makeSmallButton('↓', 'Bajar pregunta', () => moveQuestion(index, 1), index === state.form.questions.length - 1),
      makeSmallButton('×', 'Borrar pregunta', () => removeQuestion(index)),
    );
    row.append(number, input, controls);
    container.append(row);
  });
}

function makeSmallButton(label, title, callback, disabled = false) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'small-button'; button.textContent = label; button.title = title; button.disabled = disabled;
  button.addEventListener('click', callback);
  return button;
}

function moveQuestion(index, offset) {
  const next = index + offset;
  if (next < 0 || next >= state.form.questions.length) return;
  [state.form.questions[index], state.form.questions[next]] = [state.form.questions[next], state.form.questions[index]];
  renderQuestions(); setDirty();
}

function removeQuestion(index) {
  state.form.questions.splice(index, 1);
  renderQuestions(); setDirty();
}

function renderMessage() {
  ensureMessage();
  byId('message-content').value = state.form.message.content;
  byId('message-content').oninput = (event) => {
    state.form.message.content = event.target.value;
    byId('content-count').textContent = String(event.target.value.length);
    setDirty(); renderPreview();
  };
  byId('content-count').textContent = String(state.form.message.content.length);
  byId('preview-channel').textContent = selectedChannelName();
  renderAttachments(); renderEmbeds(); renderComponents(); renderPreview();
}

function selectedChannelName() {
  const channel = state.options.channels.find((item) => String(item.id) === String(state.form?.message_channel_id));
  return channel ? `# ${channel.name}` : 'sin canal';
}

function renderAttachments() {
  const attachments = state.form.message.attachments;
  byId('attachment-count').textContent = String(attachments.length);
  byId('attachment-size').textContent = `${(attachments.reduce((sum, item) => sum + item.size, 0) / 1_000_000).toFixed(1)} MB`;
  const list = byId('attachment-list'); list.replaceChildren();
  attachments.forEach((item, index) => {
    const row = document.createElement('div'); row.className = 'attachment-item';
    const name = document.createElement('span'); name.textContent = `${item.name} · ${(item.size / 1_000_000).toFixed(2)} MB`;
    const remove = makeSmallButton('×', 'Quitar adjunto', () => { attachments.splice(index, 1); renderAttachments(); setDirty(); renderPreview(); });
    row.append(name, remove); list.append(row);
  });
  byId('clear-attachments').disabled = attachments.length === 0;
  byId('preview-attachments').innerHTML = attachments.map((item) => `<div class="preview-attachment">${escapeHtml(item.name)}</div>`).join('');
}

async function uploadFiles(files) {
  const current = state.form.message.attachments;
  const pendingSize = current.reduce((sum, item) => sum + item.size, 0) + [...files].reduce((sum, file) => sum + file.size, 0);
  if (current.length + files.length > 10 || pendingSize > 24_500_000) {
    showToast('El límite es 10 adjuntos y 24.5 MB en total.', true); return;
  }
  const body = new FormData();
  [...files].forEach((file) => body.append('files', file));
  try {
    const result = await api(`/api/guilds/${state.guildId}/forms/${state.form.id}/attachments`, { method: 'POST', body });
    current.push(...result.attachments); renderAttachments(); setDirty(); renderPreview();
  } catch (error) { showToast(error.message, true); }
  byId('attachment-input').value = '';
}

function blankEmbed() {
  return { author: { name: '', url: '', icon_url: '' }, title: '', description: '', url: '', color: '#E5323B', image: '', thumbnail: '', footer: { text: '', icon_url: '' }, timestamp: '', fields: [] };
}

function renderEmbeds() {
  const host = byId('embed-editors'); host.replaceChildren();
  const embeds = state.form.message.embeds;
  byId('embed-count').textContent = String(embeds.length);
  embeds.forEach((embed, index) => host.append(buildEmbedEditor(embed, index)));
  byId('add-embed').disabled = embeds.length >= 10;
  byId('clear-embeds').disabled = embeds.length === 0;
  updateEmbedTextCount(); renderPreview();
}

function buildEmbedEditor(embed, index) {
  embed.author ||= { name: '', url: '', icon_url: '' }; embed.footer ||= { text: '', icon_url: '' }; embed.fields ||= [];
  const wrapper = document.createElement('article'); wrapper.className = 'embed-editor';
  wrapper.innerHTML = `<div class="embed-editor-head"><strong>Embed ${index + 1}</strong><button class="small-button remove-embed" type="button" title="Eliminar embed">×</button></div>
    <div class="form-grid">
      <label class="field-label">Author<input class="control" data-path="author.name" maxlength="256" placeholder="Author" value="${escapeHtml(embed.author.name)}"></label>
      <label class="field-label">Author URL<input class="control" data-path="author.url" placeholder="https://" value="${escapeHtml(embed.author.url)}"></label>
      <label class="field-label">Author Icon URL<input class="control span-2" data-path="author.icon_url" placeholder="https://" value="${escapeHtml(embed.author.icon_url)}"></label>
      <label class="field-label span-2">Title<input class="control" data-path="title" maxlength="256" value="${escapeHtml(embed.title)}"><small class="counter" data-counter="title">${(embed.title || '').length} / 256</small></label>
      <label class="field-label span-2">Description<div class="format-toolbar" role="toolbar" aria-label="Formato de descripción"><button type="button" data-embed-wrap="**" title="Negrita"><strong>B</strong></button><button type="button" data-embed-wrap="*" title="Cursiva"><em>I</em></button><button type="button" data-embed-wrap="__" title="Subrayado"><u>U</u></button><button type="button" data-embed-wrap="~~" title="Tachado"><s>S</s></button></div><textarea class="control" data-path="description" maxlength="4096">${escapeHtml(embed.description)}</textarea><small class="counter" data-counter="description">${(embed.description || '').length} / 4096</small></label>
      <label class="field-label">URL<input class="control" data-path="url" placeholder="https://" value="${escapeHtml(embed.url)}"></label>
      <label class="field-label">Color<input class="control" data-path="color" type="color" value="${/^#[0-9a-f]{6}$/i.test(embed.color || '') ? embed.color : '#e5323b'}"></label>
      <label class="field-label">Image URL<input class="control" data-path="image" placeholder="https:// o attachment://..." value="${escapeHtml(embed.image)}"></label>
      <label class="field-label">Thumbnail URL<input class="control" data-path="thumbnail" placeholder="https://" value="${escapeHtml(embed.thumbnail)}"></label>
      <label class="field-label">Footer<input class="control" data-path="footer.text" maxlength="2048" placeholder="Footer" value="${escapeHtml(embed.footer.text)}"><small class="counter" data-counter="footer.text">${(embed.footer.text || '').length} / 2048</small></label>
      <label class="field-label">Footer Icon URL<input class="control" data-path="footer.icon_url" placeholder="https://" value="${escapeHtml(embed.footer.icon_url)}"></label>
      <label class="field-label span-2">Timestamp<input class="control" data-path="timestamp" type="datetime-local" value="${escapeHtml(toLocalDateTime(embed.timestamp))}"></label>
    </div>
    <div class="embed-fields"><div class="section-heading"><div><h3>Fields <small class="counter" data-counter="fields">${embed.fields.length} / 25</small></h3></div><button class="button button-quiet add-field" type="button">Add Field</button></div><div class="field-list"></div><button class="button button-quiet clear-fields" type="button">Clear Fields</button></div>
    <p class="field-hint embed-requirement"></p>`;
  wrapper.querySelector('.remove-embed').addEventListener('click', () => { state.form.message.embeds.splice(index, 1); renderEmbeds(); setDirty(); });
  wrapper.querySelectorAll('[data-path]').forEach((input) => input.addEventListener('input', () => {
    const value = input.dataset.path === 'timestamp' && input.value
      ? new Date(input.value).toISOString()
      : input.value;
    setNested(embed, input.dataset.path, value);
    const counter = wrapper.querySelector(`[data-counter="${input.dataset.path}"]`);
    if (counter) counter.textContent = `${input.value.length} / ${input.maxLength}`;
    updateEmbedTextCount(); setDirty(); renderPreview();
  }));
  wrapper.querySelectorAll('[data-embed-wrap]').forEach((button) => button.addEventListener('click', () => {
    const textarea = wrapper.querySelector('[data-path="description"]');
    const marker = button.dataset.embedWrap; const start = textarea.selectionStart; const end = textarea.selectionEnd;
    textarea.setRangeText(`${marker}${textarea.value.slice(start, end)}${marker}`, start, end, 'select');
    textarea.dispatchEvent(new Event('input', { bubbles: true })); textarea.focus();
  }));
  const fieldsHost = wrapper.querySelector('.field-list');
  wrapper.querySelector('.embed-requirement').textContent = 'Description is required when no other fields are set.';
  const drawFields = () => {
    fieldsHost.replaceChildren();
    embed.fields.forEach((field, fieldIndex) => {
      const row = document.createElement('div'); row.className = 'field-editor';
      row.innerHTML = `<input class="control" maxlength="256" aria-label="Field name" placeholder="Name" value="${escapeHtml(field.name)}"><input class="control" maxlength="1024" aria-label="Field value" placeholder="Value" value="${escapeHtml(field.value)}"><label><input type="checkbox" ${field.inline ? 'checked' : ''}> Inline</label><button class="small-button" type="button" title="Eliminar campo">×</button>`;
      const inputs = row.querySelectorAll('input');
      inputs[0].addEventListener('input', () => { field.name = inputs[0].value; updateEmbedTextCount(); setDirty(); renderPreview(); });
      inputs[1].addEventListener('input', () => { field.value = inputs[1].value; updateEmbedTextCount(); setDirty(); renderPreview(); });
      inputs[2].addEventListener('change', () => { field.inline = inputs[2].checked; setDirty(); renderPreview(); });
      row.querySelector('button').addEventListener('click', () => { embed.fields.splice(fieldIndex, 1); drawFields(); updateEmbedTextCount(); setDirty(); renderPreview(); });
      fieldsHost.append(row);
    });
    wrapper.querySelector('[data-counter="fields"]').textContent = `${embed.fields.length} / 25`;
    wrapper.querySelector('.add-field').disabled = embed.fields.length >= 25;
    wrapper.querySelector('.clear-fields').disabled = embed.fields.length === 0;
  };
  wrapper.querySelector('.add-field').addEventListener('click', () => { if (embed.fields.length < 25) { embed.fields.push({ name: '', value: '', inline: false }); drawFields(); setDirty(); } });
  wrapper.querySelector('.clear-fields').addEventListener('click', () => { embed.fields = []; drawFields(); updateEmbedTextCount(); setDirty(); renderPreview(); });
  drawFields();
  return wrapper;
}

function setNested(target, path, value) {
  const keys = path.split('.'); let object = target;
  while (keys.length > 1) object = object[keys.shift()];
  object[keys[0]] = value;
}

function updateEmbedTextCount() {
  const total = state.form.message.embeds.reduce((sum, embed) => sum + (embed.title || '').length + (embed.description || '').length + (embed.author?.name || '').length + (embed.footer?.text || '').length + (embed.fields || []).reduce((fieldSum, field) => fieldSum + (field.name || '').length + (field.value || '').length, 0), 0);
  byId('embed-text-count').textContent = String(total);
  byId('embed-text-count').classList.toggle('over-limit', total > 6000);
}

function renderComponents() {
  const host = byId('component-editors'); host.replaceChildren();
  const components = state.form.message.components;
  byId('component-count').textContent = String(components.length);
  byId('add-component').disabled = components.length >= 5;
  components.forEach((component, index) => {
    const row = document.createElement('div'); row.className = 'component-editor';
    row.innerHTML = `<select class="control component-action"><option value="link" ${component.action === 'link' ? 'selected' : ''}>Link button</option><option value="apply" ${component.action === 'apply' ? 'selected' : ''}>Apply button</option></select><input class="control component-label" maxlength="80" placeholder="Button label" value="${escapeHtml(component.label)}"><input class="control component-url" placeholder="https://" value="${escapeHtml(component.url || '')}" ${component.action === 'apply' ? 'disabled' : ''}><button type="button" class="small-button" title="Remove component">×</button>`;
    row.querySelector('.component-action').addEventListener('change', (event) => { component.action = event.target.value; if (component.action === 'apply') component.url = ''; renderComponents(); setDirty(); renderPreview(); });
    row.querySelector('.component-label').addEventListener('input', (event) => { component.label = event.target.value; setDirty(); renderPreview(); });
    row.querySelector('.component-url').addEventListener('input', (event) => { component.url = event.target.value; setDirty(); });
    row.querySelector('button').addEventListener('click', () => { components.splice(index, 1); renderComponents(); setDirty(); renderPreview(); });
    host.append(row);
  });
  renderPreview();
}

function renderPreview() {
  if (!state.form) return;
  const message = state.form.message;
  byId('preview-content').innerHTML = safeMarkdown(message.content || '');
  byId('preview-components').innerHTML = message.components.map((component) => `<button class="preview-button" type="button">${escapeHtml(component.label || 'Button')}</button>`).join('');
  const embedsHost = byId('preview-embeds'); embedsHost.replaceChildren();
  message.embeds.forEach((embed) => {
    const element = document.createElement('div'); element.className = 'preview-embed';
    element.style.borderLeftColor = embed.color || '#e5323b';
    const author = embed.author?.name ? `<div class="preview-embed-author">${embed.author.icon_url ? `<img src="${escapeHtml(embed.author.icon_url)}" alt="">` : ''}${escapeHtml(embed.author.name)}</div>` : '';
    const title = embed.title ? `<div class="preview-embed-title">${escapeHtml(embed.title)}</div>` : '';
    const description = embed.description ? `<div class="preview-embed-desc">${safeMarkdown(embed.description)}</div>` : '';
    const fields = embed.fields?.length ? `<div class="preview-fields">${embed.fields.map((field) => `<div><strong>${escapeHtml(field.name)}</strong>${escapeHtml(field.value)}</div>`).join('')}</div>` : '';
    const image = embed.image && !embed.image.startsWith('attachment://') ? `<img class="preview-embed-image" src="${escapeHtml(embed.image)}" alt="">` : '';
    const footer = embed.footer?.text ? `<div class="preview-footer">${embed.footer.icon_url ? `<img src="${escapeHtml(embed.footer.icon_url)}" alt="">` : ''}${escapeHtml(embed.footer.text)}</div>` : '';
    element.innerHTML = `${author}${title}${description}${fields}${image}${footer}`;
    embedsHost.append(element);
  });
  byId('preview-channel').textContent = selectedChannelName();
}

async function saveForm() {
  if (!state.form) return;
  const payload = {
    ...state.form,
    name: byId('form-name').value,
    log_channel_id: byId('log-channel').value,
    message_channel_id: byId('message-channel').value,
    accept_role_ids: selectedValues(byId('accept-roles')),
    reject_role_ids: selectedValues(byId('reject-roles')),
    questions: state.form.questions.map((question) => ({ prompt: question.prompt })),
    message: state.form.message,
  };
  try {
    await api(`/api/guilds/${state.guildId}/forms/${state.form.id}`, { method: 'PUT', body: JSON.stringify(payload) });
    state.form.name = payload.name;
    setDirty(false); showToast('Formulario guardado.');
    return true;
  } catch (error) { showToast(error.message, true); return false; }
}

async function publishForm() {
  if (!state.form) return;
  if (state.dirty && !await saveForm()) return;
  try {
    const result = await api(`/api/guilds/${state.guildId}/forms/${state.form.id}/publish`, { method: 'POST', body: '{}' });
    state.form.published_message_id = result.message_id;
    showToast('Mensaje publicado en Discord.');
    window.open(result.jump_url, '_blank', 'noopener');
  } catch (error) { showToast(error.message, true); }
}

async function createForm() {
  const name = prompt('Nombre del formulario:');
  if (!name?.trim()) return;
  try {
    const created = await api(`/api/guilds/${state.guildId}/forms`, { method: 'POST', body: JSON.stringify({ name }) });
    await loadGuild(); await selectForm(created.id); showToast('Formulario creado.');
  } catch (error) { showToast(error.message, true); }
}

async function deleteForm() {
  if (!state.form || !confirm(`¿Eliminar el formulario "${state.form.name}" y sus solicitudes? Esta acción no se puede deshacer.`)) return;
  try {
    await api(`/api/guilds/${state.guildId}/forms/${state.form.id}`, { method: 'DELETE' });
    await loadGuild(); showToast('Formulario eliminado.');
  } catch (error) { showToast(error.message, true); }
}

async function downloadBackup() {
  if (state.demo) {
    const snapshot = { forms: state.forms, exported_at: new Date().toISOString(), note: 'Demo local: no contiene datos del bot.' };
    const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = 'sentra-demo.json'; link.click();
    URL.revokeObjectURL(url); showToast('Descargada una copia de demostración local.');
    return;
  }
  try {
    const response = await fetch(`/api/guilds/${state.guildId}/backup`, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `sentra-backup-${state.guildId}.zip`;
    link.click();
    URL.revokeObjectURL(url);
    showToast('Copia descargada. Guárdala en un lugar privado.');
  } catch (error) { showToast(error.message, true); }
}

async function restoreBackup(file) {
  if (!file) return;
  if (state.demo) {
    showToast('La restauración real requiere el backend del bot.');
    byId('restore-backup-input').value = '';
    return;
  }
  if (!confirm('Restaurar reemplazará formularios y solicitudes de este servidor. Las solicitudes pendientes se archivarán. ¿Continuar?')) {
    byId('restore-backup-input').value = '';
    return;
  }
  try {
    const response = await fetch(`/api/guilds/${state.guildId}/backup`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/zip' },
      body: file,
    });
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    state.dirty = false;
    await loadGuild();
    showToast(`Respaldo restaurado: ${result.forms} formularios y ${result.submissions} solicitudes.`);
  } catch (error) { showToast(error.message, true); }
  byId('restore-backup-input').value = '';
}

async function renderSubmissions() {
  if (!state.form) return;
  const host = byId('submission-list');
  try {
    const result = await api(`/api/guilds/${state.guildId}/forms/${state.form.id}/submissions`);
    if (!result.submissions.length) { host.innerHTML = '<div class="question-empty">No hay solicitudes todavía.</div>'; return; }
    host.innerHTML = result.submissions.map((item) => `<article class="submission-row"><strong>#${item.id}</strong><span><b>${escapeHtml(item.username || item.user_id)}</b><br><span class="muted">${escapeHtml(item.review_reason || item.created_at || '')}</span></span><span class="status-badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></article>`).join('');
  } catch (error) { host.innerHTML = `<div class="question-empty">${escapeHtml(error.message)}</div>`; }
}

function addEmbed() { if (state.form.message.embeds.length >= 10) return; state.form.message.embeds.push(blankEmbed()); renderEmbeds(); setDirty(); }
function addComponent() { if (state.form.message.components.length >= 5) return; state.form.message.components.push({ type: 'button', action: 'link', label: '', url: '', style: 'primary' }); renderComponents(); setDirty(); }

byId('guild-select').addEventListener('change', async (event) => {
  if (state.dirty && !confirm('Descartar los cambios sin guardar y cambiar de servidor?')) { event.target.value = state.guildId; return; }
  state.guildId = event.target.value;
  try { await loadGuild(); } catch (error) { showToast(error.message, true); }
});
byId('new-form').addEventListener('click', createForm);
byId('save-form').addEventListener('click', saveForm);
byId('publish-form').addEventListener('click', publishForm);
byId('delete-form').addEventListener('click', deleteForm);
byId('download-backup').addEventListener('click', downloadBackup);
byId('restore-backup-input').addEventListener('change', (event) => restoreBackup(event.target.files[0]));
byId('add-question').addEventListener('click', () => { state.form.questions.push({ prompt: '' }); renderQuestions(); setDirty(); document.querySelector('.question-input:last-of-type')?.focus(); });
byId('attachment-input').addEventListener('change', (event) => uploadFiles(event.target.files));
byId('clear-attachments').addEventListener('click', () => { state.form.message.attachments = []; renderAttachments(); setDirty(); renderPreview(); });
byId('add-embed').addEventListener('click', addEmbed);
byId('clear-embeds').addEventListener('click', () => { state.form.message.embeds = []; renderEmbeds(); setDirty(); });
byId('add-component').addEventListener('click', addComponent);
byId('clear-components').addEventListener('click', () => { state.form.message.components = []; renderComponents(); setDirty(); });
byId('preview-refresh').addEventListener('click', renderPreview);
byId('refresh-submissions').addEventListener('click', renderSubmissions);
byId('logout').addEventListener('click', async () => { await api('/auth/logout', { method: 'POST', body: '{}' }); location.reload(); });
document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('is-active', item === tab));
  document.querySelectorAll('.tab-panel').forEach((panel) => { panel.hidden = panel.id !== `tab-${tab.dataset.tab}`; panel.classList.toggle('is-active', !panel.hidden); });
  if (tab.dataset.tab === 'submissions') renderSubmissions();
}));
document.querySelectorAll('[data-wrap]').forEach((button) => button.addEventListener('click', () => {
  const textarea = byId('message-content'); const marker = button.dataset.wrap; const start = textarea.selectionStart; const end = textarea.selectionEnd;
  const value = textarea.value; textarea.setRangeText(`${marker}${value.slice(start, end)}${marker}`, start, end, 'select');
  textarea.dispatchEvent(new Event('input', { bubbles: true })); textarea.focus();
}));
byId('message-content').addEventListener('input', () => { state.form.message.content = byId('message-content').value; renderPreview(); });
start();
