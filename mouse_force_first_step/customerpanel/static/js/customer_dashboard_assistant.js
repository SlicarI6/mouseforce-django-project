/* Existing Message Us widget: explicit conversation state, never stored HTML. */
(() => {
  const prefix = 'mouseforce:customer-assistant:v2:';
  let sessionEnded = false;
  function storage(action, key, value) {
    try { return sessionStorage[action](key, value); } catch (_) { return null; }
  }
  function clearStored(except) {
    try {
      Object.keys(sessionStorage).filter(key => key.startsWith(prefix) && key !== except)
        .forEach(key => sessionStorage.removeItem(key));
      sessionStorage.removeItem('chat_content'); // Never restore old unscoped HTML.
    } catch (_) { /* Temporary chat also works without browser storage. */ }
  }
  window.addEventListener('customer:session-ended', () => { sessionEnded = true; clearStored(); });
  window.CustomerDashboardAssistant = function (scope) {
    const doc = scope.document, box = doc.getElementById('chat-box');
    if (!box) return;
    const body = doc.getElementById('chat-body'), input = doc.getElementById('chat-input');
    const send = doc.querySelector('#chat-footer button'), toggle = doc.getElementById('chat-toggle-btn');
    const minimize = doc.getElementById('chat-close-btn'), key = prefix + box.dataset.session;
    const personas = [
      {name: 'Leon S.', image: 'leon_support.png'},
      {name: 'Sofia W.', image: 'sofia_support.png'},
      {name: 'Charles M.', image: 'charles_support.png'},
    ];
    const fresh = () => ({privacy: null, phase: 'privacy', persona: 1, messages: [], history: []});
    let state = fresh(), opened = false, pending = false, generation = 0, controller = null;
    clearStored(key);
    try {
      const saved = JSON.parse(storage('getItem', key));
      if (saved?.privacy === 'remember' && ['privacy', 'support', 'active', 'ended'].includes(saved.phase)
          && Number.isInteger(saved.persona) && personas[saved.persona]
          && Array.isArray(saved.messages) && saved.messages.length <= 40
          && saved.messages.every(item => ['user', 'bot'].includes(item.role) && typeof item.text === 'string' && item.text.length <= 6000)
          && Array.isArray(saved.history) && saved.history.length <= 8
          && saved.history.every(item => ['user', 'assistant'].includes(item.role) && typeof item.content === 'string' && item.content.length <= 6000)) state = saved;
    } catch (_) { storage('removeItem', key); }
    function remember() {
      state.messages = state.messages.slice(-40);
      state.history = state.history.slice(-8);
      while (JSON.stringify(state.history).length > 11000) state.history.splice(0, 2);
      if (!sessionEnded && state.privacy === 'remember' && box.dataset.session) storage('setItem', key, JSON.stringify(state));
      else storage('removeItem', key);
    }
    function node(tag, text, className) {
      const element = doc.createElement(tag);
      if (text !== undefined) element.textContent = text;
      if (className) element.className = className;
      return element;
    }
    function link(url, title, external = false) {
      const anchor = node('a', title);
      try {
        const parsed = new URL(url, location.origin);
        if (external ? parsed.protocol !== 'https:' || parsed.username || parsed.password : parsed.origin !== location.origin) return node('span', title);
        anchor.href = parsed.href;
      } catch (_) { return node('span', title); }
      if (external) { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; }
      return anchor;
    }
    function bubble(item) {
      const row = node('div', undefined, 'chat-message ' + item.role + (item.role === 'bot' ? ' with-icon' : ''));
      if (item.role === 'user') row.textContent = item.text;
      else {
        const image = doc.createElement('img');
        image.src = box.dataset.staticImages + (item.persona ? personas[state.persona].image : 'chat_force_img.png');
        image.className = 'chat-icon'; image.alt = item.persona ? personas[state.persona].name + ' · AI Assistant' : 'MouseForce';
        const content = node('div', item.text, 'chat-bubble');
        if (item.contact) content.append(node('br'), link(box.dataset.contactUrl, 'Contact MouseForce'));
        if (Array.isArray(item.sources)) {
          const sources = node('div', undefined, 'chat-sources');
          item.sources.slice(0, 12).forEach(source => {
            if (source && typeof source.url === 'string' && typeof source.title === 'string')
              sources.append(link(source.url, source.title.slice(0, 200), true));
          });
          content.append(sources);
        }
        row.append(image, content);
      }
      return row;
    }
    function action(text, callback) {
      const button = node('button', text); button.type = 'button';
      button.addEventListener('click', callback); // Lifetime is this render's detached node.
      return button;
    }
    function render() {
      body.replaceChildren(...state.messages.map(bubble));
      const actions = node('div', undefined, 'yes-no-btns');
      if (state.phase === 'privacy') {
        const privacy = node('div', undefined, 'chat-bubble');
        privacy.append(link(box.dataset.privacyUrl, 'Privacy Policy'), node('br'),
          node('span', 'Your questions and recent conversation are sent to OpenAI to generate replies; it may retain data for safety. MouseForce does not save this AI conversation in its database. Remember this chat in this browser tab for your current login? Yes remembers it; No uses temporary chat, cleared when you close it or leave this page. Please avoid sensitive information.'));
        body.append(privacy);
        actions.append(action('Yes', () => choosePrivacy('remember')), action('No', () => choosePrivacy('temporary')));
      } else if (state.phase === 'support') {
        actions.append(action('Technical Support', technical), action('Product Advice', advice));
      } else {
        actions.className = 'chat-session-actions';
        if (state.phase === 'active') actions.append(action('End conversation', end));
        actions.append(action('Start new', restart));
      }
      body.append(actions);
      if (pending) body.append(node('div', personas[state.persona].name + ' is typing…', 'chat-status'));
      input.disabled = send.disabled = state.phase !== 'active' || pending;
      box.setAttribute('aria-busy', String(pending));
      body.scrollTop = body.scrollHeight;
    }
    function add(text, role = 'bot', extra = {}) { state.messages.push({text, role, ...extra}); remember(); }
    function cancel() { generation++; controller?.abort(); controller = null; pending = false; }
    function restart() {
      cancel(); storage('removeItem', key); state = fresh();
      const hour = new Date().getHours();
      add('Good ' + (hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening') + ' and warm welcome!');
      input.value = ''; render();
    }
    function choosePrivacy(choice) {
      if (state.phase !== 'privacy') return;
      state.privacy = choice; state.phase = 'support';
      add(choice === 'remember' ? 'Yes' : 'No', 'user');
      if (choice === 'temporary') add('No problem. This is a temporary chat; it will not be remembered when you close it or leave this page.');
      add('Do you need technical support or product advice?'); render();
    }
    function technical() {
      if (state.phase !== 'support') return;
      add('For help from the MouseForce team, please use Contact or the separate ChatMe messaging page. This assistant cannot confirm staff availability.', 'bot', {contact: true});
      end();
    }
    function advice() {
      if (state.phase !== 'support') return;
      state.phase = 'active'; state.persona = Math.floor(Math.random() * personas.length);
      add('Great! You can ask me anything about MouseForce, deals, places, travel, useful tools or everyday questions.');
      add("Hello! I'm " + personas[state.persona].name + ' · AI Assistant. How can I help?', 'bot', {persona: true});
      render(); input.focus();
    }
    function end() { cancel(); state.phase = 'ended'; add('Your conversation has ended\n' + new Date().toLocaleString()); render(); }
    function close() {
      cancel(); opened = false;
      if (state.privacy !== 'remember') { storage('removeItem', key); state = fresh(); body.replaceChildren(); input.value = ''; }
      else remember();
      box.classList.remove('visible'); box.style.display = 'none';
      minimize.style.display = 'none'; toggle.style.display = 'flex'; toggle.setAttribute('aria-expanded', 'false'); toggle.focus();
    }
    function open() {
      opened = true; box.style.display = 'flex'; box.classList.add('visible');
      toggle.style.display = 'none'; minimize.style.display = 'flex'; toggle.setAttribute('aria-expanded', 'true');
      if (!state.messages.length) restart(); else render();
    }
    async function sendChat() {
      const question = input.value.trim();
      if (!question || question.length > 2000 || pending || state.phase !== 'active') return;
      const history = state.history.slice(), requestGeneration = generation;
      const requestController = new AbortController(); controller = requestController;
      const requestId = crypto.randomUUID();
      add(question, 'user'); input.value = ''; pending = true; render();
      const timeout = setTimeout(() => requestController.abort(), 40000);
      try {
        // Intentionally not scope.track(): slow AI must not block navigation/music.
        const response = await fetch(box.dataset.aiUrl, {
          method: 'POST', credentials: 'same-origin', mode: 'same-origin', signal: requestController.signal,
          headers: {'Content-Type': 'application/json', 'X-CSRFToken': doc.querySelector('#chat-footer [name=csrfmiddlewaretoken]').value},
          body: JSON.stringify({question, history, request_id: requestId, privacy: state.privacy, persona: personas[state.persona].name}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || typeof data.response !== 'string') throw new Error(data.error || 'Unable to get a reply. Please try again or sign in again.');
        if (!scope.alive || generation !== requestGeneration || state.phase !== 'active') return;
        add(data.response, 'bot', {persona: true, sources: data.sources});
        state.history.push({role: 'user', content: question}, {role: 'assistant', content: data.response}); remember();
      } catch (error) {
        if (!scope.alive || generation !== requestGeneration) return;
        add(error.name === 'AbortError' ? 'The reply took too long. Please try again shortly.' : error.message);
      } finally {
        clearTimeout(timeout);
        if (scope.alive && generation === requestGeneration) { pending = false; controller = null; render(); }
      }
    }
    scope.on(toggle, 'click', open); scope.on(minimize, 'click', close);
    scope.on(doc.querySelector('[data-chat-minimize]'), 'click', close);
    scope.on(input, 'keydown', event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); sendChat(); } });
    scope.on(box, 'keydown', event => { if (event.key === 'Escape' && opened) close(); });
    scope.expose('sendChat', sendChat);
    scope.expose('clearChat', () => { restart(); close(); });
    scope.cleanup(() => { cancel(); if (!sessionEnded && state.privacy === 'remember') remember(); else storage('removeItem', key); });
    render();
  };
})();
