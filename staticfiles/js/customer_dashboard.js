/* Dashboard-only state and effects. Reward calculations stay on the server. */
window.CustomerDashboard = function (scope, draft) {
  const document = scope.document;
  const get = id => document.getElementById(id);
  (function mountPoints() {

  const controls = document.getElementById('points-controls');
  if (!controls) return;

  const dailyButton = document.getElementById('points-daily');
  const bonusButton = document.getElementById('points-bonus');
  const message = document.getElementById('points-message');
  const csrfToken = controls.querySelector('[name="csrfmiddlewaretoken"]').value;
  let state = JSON.parse(document.getElementById('points-initial-state').textContent);
  let pending = false;

  function render() {
    if (!scope.alive) return;
    document.getElementById('points-total').textContent = `${state.total_points} Points`;
    document.getElementById('points-streak').textContent = state.streak_label;
    const dailyCompleted = !state.daily_claimable && state.daily_label === 'Claimed today';
    document.getElementById('points-daily-label').textContent = dailyCompleted
      ? 'Claimed today ✓' : (state.daily_claimable ? 'Ready to claim' : state.daily_label);
    dailyButton.dataset.state = dailyCompleted ? 'completed' : (state.daily_claimable ? 'available' : 'unavailable');

    // Presentation only: eligibility still comes from the unchanged server flags.
    const streak = state.displayed_streak;
    const bonusAvailable = state.day_7_bonus_claimable || state.day_14_bonus_claimable;
    const bonusCompleted = !state.streak_broken && (
      (streak === 7 && state.day_7_bonus_awarded) || (streak === 14 && state.day_14_bonus_awarded)
    );
    const goal = streak > 7 || (streak === 7 && state.day_7_bonus_awarded) ? 14 : 7;
    const goalLabel = `${goal} Day Streak → +${goal === 7 ? 35 : 50} Points`;
    document.getElementById('points-next-milestone').textContent = goalLabel;
    const progress = document.getElementById('points-progress');
    progress.max = goal;
    progress.value = streak;
    progress.setAttribute('aria-label', goalLabel);
    document.getElementById('points-progress-text').textContent = `${streak} / ${goal}`;
    const bonusLabel = document.getElementById('points-bonus-label');
    bonusLabel.hidden = !bonusAvailable && !bonusCompleted;
    bonusLabel.textContent = bonusAvailable ? state.bonus_label : (bonusCompleted ? 'Bonus claimed ✓' : '');
    bonusButton.dataset.state = bonusAvailable ? 'available' : (bonusCompleted ? 'completed' : 'status');
    document.getElementById('points-streak-status').textContent = state.streak_broken
      ? 'Streak broken. A fresh start is one daily claim away.' : '';
    dailyButton.disabled = pending || !state.daily_claimable;
    bonusButton.disabled = pending || !(state.day_7_bonus_claimable || state.day_14_bonus_claimable);
    controls.setAttribute('aria-busy', String(pending));
  }

  async function claim(url) {
    if (pending) return;
    pending = true;
    message.textContent = 'Claiming…';
    render();
    try {
      const response = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken },
      });
      const data = await response.json();
      if (!response.ok || !data.state) throw new Error('Claim not confirmed');
      state = data.state;
      message.textContent = '';
    } catch (error) {
      message.textContent = 'Unable to confirm the claim. Refresh the page to check your points before trying again.';
    } finally {
      pending = false;
      render();
    }
  }

  scope.on(dailyButton, 'click', () => {
    if (!dailyButton.disabled) scope.track(claim(controls.dataset.dailyUrl));
  });
  scope.on(bonusButton, 'click', () => {
    if (!bonusButton.disabled) scope.track(claim(controls.dataset.bonusUrl));
  });
  scope.on(window, 'customer:points-state', event => {
    if (!event.detail || typeof event.detail.total_points !== 'number') return;
    state = event.detail;
    render();
  });
  render();

  })();

  get('dateInput').value = new Date().toISOString().split('T')[0];
  const chatRoom = document.querySelector('.dashboard').dataset.chatRoom;
  const chatSocket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/chat/${encodeURIComponent(chatRoom)}/`);
  scope.expose('sendChatMessage', message => chatSocket.send(JSON.stringify({message})));
  scope.cleanup(() => chatSocket.close());

  const overflow = document.body.style.overflow;
  function expandFeedback() {
    get('expandedFeedback').style.display = 'flex';
    get('feedbackOverlay').style.display = 'block';
    document.querySelector('.feedback-bar').style.display = 'none';
    document.body.style.overflow = 'hidden';
  }
  function collapseFeedback() {
    get('expandedFeedback').style.display = 'none';
    get('feedbackOverlay').style.display = 'none';
    document.querySelector('.feedback-bar').style.display = 'flex';
    document.body.style.overflow = overflow;
  }
  scope.expose('expandFeedback', expandFeedback);
  scope.expose('collapseFeedback', collapseFeedback);
  scope.cleanup(() => { document.body.style.overflow = overflow; });

  const fields = ['quickMessage', 'feedbackInput', 'countryInput', 'ratingValue'];
  fields.forEach(id => { if (draft[id] !== undefined) get(id).value = draft[id]; });
  function setAudioFile(file) {
    const transfer = new DataTransfer();
    transfer.items.add(file);
    get('audioInput').files = transfer.files;
    if (audioURL) URL.revokeObjectURL(audioURL);
    audioURL = URL.createObjectURL(file);
    get('audioPlayer').src = audioURL;
    get('audioPlayer').style.display = 'block';
  }
  let recorder = null;
  let stream = null;
  let audioURL = null;
  let stopping = Promise.resolve();
  if (draft.audio) setAudioFile(draft.audio);
  scope.on(get('startRecord'), 'click', () => scope.track((async () => {
    if (stream) return;
    get('startRecord').disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (!scope.alive) { stream.getTracks().forEach(track => track.stop()); return; }
      recorder = new MediaRecorder(stream);
      const chunks = [];
      recorder.ondataavailable = event => chunks.push(event.data);
      stopping = new Promise(resolve => {
        recorder.onstop = () => {
          stream?.getTracks().forEach(track => track.stop());
          stream = null;
          if (scope.alive) {
            setAudioFile(new File(chunks, 'voice_feedback.webm', {type: 'audio/webm'}));
            get('recordingStatus').textContent = '✅ Înregistrare completă';
            get('startRecord').disabled = false;
          }
          resolve();
        };
      });
      recorder.start();
      get('recordingStatus').textContent = '🎙️ Înregistrare...';
    } catch (_) {
      stream?.getTracks().forEach(track => track.stop());
      stream = null;
      get('recordingStatus').textContent = 'Recording is unavailable. Please check microphone access.';
      get('startRecord').disabled = false;
    }
  })()));
  scope.on(get('stopRecord'), 'click', () => { if (recorder?.state === 'recording') recorder.stop(); });
  scope.beforeLeave = async () => {
    if (recorder?.state === 'recording') recorder.stop();
    await stopping;
    fields.forEach(id => { draft[id] = get(id).value; });
    draft.audio = get('audioInput').files[0];
  };
  scope.cleanup(() => {
    if (recorder?.state === 'recording') recorder.stop();
    stream?.getTracks().forEach(track => track.stop());
    get('audioPlayer').pause();
    if (audioURL) URL.revokeObjectURL(audioURL);
  });

  const feedbackBar = document.querySelector('.feedback-bar');
  const observer = new IntersectionObserver(entries => {
    const entry = entries[0];
    feedbackBar.style.position = entry.isIntersecting ? 'absolute' : 'fixed';
    feedbackBar.style.bottom = entry.isIntersecting ? `${entry.boundingClientRect.height}px` : '0';
  }, {root: null, threshold: 0.01});
  observer.observe(window.document.getElementById('footer'));
  scope.cleanup(() => observer.disconnect());

  const quick = get('quickMessage');
  const send = get('sendButton');
  function syncQuick() {
    const enabled = /[a-zăâîșț]/.test(quick.value.toLowerCase());
    send.classList.toggle('enabled', enabled);
    send.disabled = !enabled;
  }
  scope.on(quick, 'input', syncQuick);
  syncQuick();
  function csrf() {
    const cookie = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith('csrftoken='));
    return cookie ? decodeURIComponent(cookie.slice(10)) : '';
  }
  let feedbackPending = false;
  async function submitFeedback(quickMode) {
    if (feedbackPending) return;
    const input = quickMode ? quick : get('feedbackInput');
    const message = input.value.trim();
    if (!message) { alert('Scrie un mesaj înainte de a trimite.'); return; }
    feedbackPending = true;
    const headers = {'X-CSRFToken': csrf()};
    const body = quickMode ? new URLSearchParams({message}) : new FormData();
    if (quickMode) headers['Content-Type'] = 'application/x-www-form-urlencoded';
    else {
      body.append('message', message);
      if (get('ratingValue').value) body.append('rating', get('ratingValue').value);
      if (get('countryInput').value) body.append('country', get('countryInput').value);
      if (get('audioInput').files[0]) body.append('audio', get('audioInput').files[0]);
    }
    try {
      const response = await fetch('/customer/save-feedback/', {method: 'POST', headers, body});
      const data = await response.json();
      if (!scope.alive) return;
      if (data.status === 'ok') {
        alert('Feedback sent successfully!');
        input.value = '';
        syncQuick();
      } else if (quickMode) alert('You sent 2 attemps. Wait please');
      else if (data.status === 'limit') alert('You send 3 times wait 24 hours. Thanks');
      else alert('Error,please try again.');
    } catch (_) { alert('Unable to send feedback. Please try again.'); }
    finally { feedbackPending = false; }
  }
  scope.expose('submitQuickFeedback', () => scope.track(submitFeedback(true)));
  scope.expose('submitFeedback', () => scope.track(submitFeedback(false)));

  const stars = document.querySelectorAll('.star');
  let rating = Number(get('ratingValue').value) || 0;
  const highlight = value => stars.forEach((star, index) => star.querySelector('path').setAttribute('fill', index < value ? 'gold' : 'gray'));
  stars.forEach((star, index) => {
    scope.on(star, 'mouseover', () => highlight(index + 1));
    scope.on(star, 'click', () => { rating = index + 1; get('ratingValue').value = rating; highlight(rating); });
    scope.on(star, 'mouseleave', () => highlight(rating));
  });
  highlight(rating);

  const tabs = document.querySelectorAll('.tab-btn');
  const contents = document.querySelectorAll('.tab-content');
  tabs.forEach(tab => scope.on(tab, 'click', () => {
    tabs.forEach(item => item.classList.remove('active'));
    contents.forEach(item => item.classList.remove('active'));
    tab.classList.add('active');
    get(tab.dataset.tab).classList.add('active');
  }));
  if (window.VANTA?.CLOUDS && get('vanta-background')) {
    try {
      const effect = window.VANTA.CLOUDS({el: get('vanta-background'), mouseControls: true,
        touchControls: true, gyroControls: false, minHeight: 200, minWidth: 200, skyColor: 0xa2deff});
      scope.cleanup(() => effect.destroy());
    } catch (_) { /* Keep the static background when WebGL is unavailable. */ }
  }
  window.CustomerDashboardAssistant(scope);
};
