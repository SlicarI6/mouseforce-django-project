/* Dashboard assistant lifecycle. ChatMe remains a separate full-page destination. */
window.CustomerDashboardAssistant = function (scope) {
  const document = scope.document;
  const images = document.getElementById('chat-box').dataset.staticImages;

  function clearChat() {
  sessionStorage.removeItem("chat_content"); // șterge salvarea
  chatBody.innerHTML = "";                  // curăță vizual
  toggleChatClose();                        // închide vizual
}



let activeConsultantName = "";
let activeConsultantImage = "";

const chatBox = document.getElementById("chat-box");
const chatToggleBtn = document.getElementById("chat-toggle-btn");
const chatCloseBtn = document.getElementById("chat-close-btn");
const chatBody = document.getElementById("chat-body");

function toggleChatOpen() {
  const loader = document.getElementById("chat-loader-bar");
  loader.style.width = "100%";
  chatBox.style.display = "flex";

  scope.timeout(() => { loader.style.width = "0"; }, 1000);

  scope.timeout(() => {
    chatBox.classList.add("visible");
    const saved = sessionStorage.getItem("chat_content");
    if (saved) {
      chatBody.innerHTML = saved;
    } else {
      startIntroConversation();
    }
  }, 10);

  chatToggleBtn.style.display = "none";
  chatCloseBtn.style.display = "flex";
}

function toggleChatClose() {
  chatBox.classList.remove("visible");
  chatCloseBtn.style.display = "none";
  chatToggleBtn.style.display = "flex";
  scope.timeout(() => { chatBox.style.display = "none"; }, 300);
}

function sendChat() {
  const input = document.getElementById("chat-input");
  const msg = input.value.trim();
  if (!msg) return;

  const userMsg = document.createElement("div");
  userMsg.className = "chat-message user";
  userMsg.textContent = msg;
  chatBody.appendChild(userMsg);

  chatBody.scrollTop = chatBody.scrollHeight;
  input.value = "";

  if (aiChatActive) {
    scope.fetch("/customer/ask-openai/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify({ question: msg }),
    })
      .then((res) => res.json())
      .then((data) => {
        const delay = Math.floor(Math.random() * (60000 - 40000 + 1)) + 40000;

        scope.timeout(() => {
          const botBubble = document.createElement("div");
          botBubble.className = "chat-message bot with-icon";
          botBubble.innerHTML = `
            <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
            <div class="chat-bubble">${data.response}</div>
          `;
          chatBody.appendChild(botBubble);
          chatBody.scrollTop = chatBody.scrollHeight;
        }, delay);
      }).catch(() => {});
  }
}


function getGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning and warm welcome!";
  if (hour < 18) return "Good afternoon and warm welcome!";
  return "Good evening and warm welcome!";
}

function startIntroConversation() {
  chatBody.innerHTML = `<div style="color:#666;margin-bottom:12px">This is the beginning of your conversation with us.</div>`;

  scope.timeout(() => {
    const welcome = document.createElement("div");
    welcome.innerHTML = `
      <div class="chat-message bot no-icon">
        <div class="chat-bubble">${getGreeting()}</div>
      </div>`;
    chatBody.appendChild(welcome);
    chatBody.scrollTop = chatBody.scrollHeight;
  }, 1000);

  scope.timeout(() => {
    const privacyWrapper = document.createElement("div");
    privacyWrapper.innerHTML = `
      <div class="chat-message bot with-icon">
        <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
        <div>
          <div class="chat-bubble">
            For <a href="#">Privacy Policy</a>. We would store the history for 90 days. Do you agree to this?
          </div>
          <div style="font-size: 12px; color: #888; margin-top: 4px;">MF · ${getCurrentTime()}</div>
        </div>
      </div>
      <div class="yes-no-btns">
        <button onclick="handleYes()">Yes</button>
        <button onclick="handleNo()">No</button>
      </div>
    `;
    chatBody.appendChild(privacyWrapper);
    chatBody.scrollTop = chatBody.scrollHeight;
  }, 2500);
}

function handleYes() {
  const userYes = document.createElement("div");
  userYes.className = "chat-message user";
  userYes.textContent = "Yes";
  chatBody.appendChild(userYes);

  handleSupportPrompt();
}

function handleNo() {
  const userNo = document.createElement("div");
  userNo.className = "chat-message user";
  userNo.textContent = "No";
  chatBody.appendChild(userNo);

  handleSupportPrompt();
}

function handleSupportPrompt() {
  const yesNoBtns = document.querySelector(".yes-no-btns");
  if (yesNoBtns) yesNoBtns.remove();

  const botMsg = document.createElement("div");
  botMsg.className = "chat-message bot with-icon";
  botMsg.innerHTML = `
    <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
    <div class="chat-bubble">
      Do you need technical support or product advice?
    </div>
  `;
  chatBody.appendChild(botMsg);

  const optionsBtns = document.createElement("div");
  optionsBtns.className = "yes-no-btns";
  optionsBtns.innerHTML = `
    <button onclick="handleTechnicalSupport()">Technical Support</button>
    <button onclick="startOpenAIChat()">Product Advice</button>

  `;
  chatBody.appendChild(optionsBtns);

  chatBody.scrollTop = chatBody.scrollHeight;
}

function handleTechnicalSupport() {
  const msg1 = document.createElement("div");
  msg1.className = "chat-message bot with-icon";
  msg1.innerHTML = `
    <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
    <div class="chat-bubble">
      Our technical experts are currently assisting other customers.<br>
      However, we are available 24/7 to support you by phone:<br><br>
      <span style="font-size: 18px;">📞 +44 7786xxxxxx</span>
    </div>
  `;
  chatBody.appendChild(msg1);

  const msg2 = document.createElement("div");
  msg2.className = "chat-message bot with-icon";
  msg2.innerHTML = `
    <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
    <div class="chat-bubble">
      Alternatively, you can find more contact options here:<br><br>
      🌐 <a href="https://mouseforce.onrender.com/contact/" target="_blank">https://mouseforce.contact</a><br><br>
      Thank you for your understanding!
    </div>
  `;
  chatBody.appendChild(msg2);

  const now = new Date();
  const time = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const date = now.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });

  const endMsg = document.createElement("div");
  endMsg.style.textAlign = "center";
  endMsg.style.fontSize = "13px";
  endMsg.style.color = "#666";
  endMsg.style.marginTop = "20px";
  endMsg.innerHTML = `
    <div>Your conversation has ended</div>
    <div>${date} ${time}</div>
  `;
  chatBody.appendChild(endMsg);

  const restartBtn = document.createElement("div");
  restartBtn.style.textAlign = "center";
  restartBtn.innerHTML = `
    <button onclick="restartConversation()" style="margin-top: 15px; padding: 8px 16px; font-size: 15px; border-radius: 6px; background-color: #0a2b5c; color: white; border: none; cursor: pointer;">
      Start new
    </button>
  `;
  chatBody.appendChild(restartBtn);

  chatBody.scrollTop = chatBody.scrollHeight;
}

function restartConversation() {
  sessionStorage.removeItem("chat_content");
  chatBody.innerHTML = "";
  startIntroConversation();
}


function getCurrentTime() {
  const now = new Date();
  return now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}


let aiChatActive = true;











function sendQuestion() {
  const question = document.getElementById("user-question").value.trim();
  if (!question) return;

  const userBubble = document.createElement("div");
  userBubble.className = "chat-message user";
  userBubble.textContent = question;
  chatBody.appendChild(userBubble);

  document.getElementById("user-question").value = "";

  scope.fetch("/ask-openai/", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": getCookie("csrftoken"),
    },
    body: JSON.stringify({ question }),
  })
    .then((res) => res.json())
    .then((data) => {
      const delay = Math.floor(Math.random() * (60000 - 40000 + 1)) + 40000;

      scope.timeout(() => {
        const botBubble = document.createElement("div");
        botBubble.className = "chat-message bot with-icon";
        botBubble.innerHTML = `
          <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
          <div class="chat-bubble">${data.response}</div>
        `;
        chatBody.appendChild(botBubble);
        chatBody.scrollTop = chatBody.scrollHeight;
      }, delay);
    }).catch(() => {});
}

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}


scope.on(chatToggleBtn, "click", toggleChatOpen);
scope.on(chatCloseBtn, "click", () => {
  sessionStorage.setItem("chat_content", chatBody.innerHTML);
  toggleChatClose();
});



function startOpenAIChat() {
  const yesNoBtns = document.querySelector(".yes-no-btns");
  if (yesNoBtns) yesNoBtns.remove();

  // 🟢 Alege aleatoriu un consultant
  const consultants = [
    { name: "Leon S.", image: "leon_support.png", initial: "L" },
    { name: "Sofia W.", image: "sofia_support.png", initial: "S" },
    { name: "Charles M.", image: "charles_support.png", initial: "C" },
  ];
  const randomConsultant = consultants[Math.floor(Math.random() * consultants.length)];
  activeConsultantName = randomConsultant.name;
  activeConsultantImage = randomConsultant.image;

  // 🕒 Ora curentă
  const now = new Date();
  const timeString = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  // 🟢 Mesajul principal
  const aiMsg = document.createElement("div");
  aiMsg.className = "chat-message bot with-icon";
  aiMsg.innerHTML = `
    <img src="${images}chat_force_img.png" class="chat-icon" alt="Bot">
    <div class="chat-bubble">
      <strong>Great! You can ask me anything about our IT services below.</strong><br>
      <br>Next available consultant is <strong>${randomConsultant.name}</strong>.
    </div>
  `;
  chatBody.appendChild(aiMsg);

  // 🟢 Mesajul cu consultantul
  const consultantMsg = document.createElement("div");
  consultantMsg.className = "chat-message bot with-icon";
 consultantMsg.innerHTML = `
  <img src="${images}${randomConsultant.image}" class="chat-icon" alt="${randomConsultant.initial}">
  <div class="consultant-container">
    <div class="chat-bubble">
      Hello! I'm ${randomConsultant.name}. What service do you need?
    </div>
    <div class="chat-meta">${randomConsultant.name} · ${timeString}</div>
  </div>
`;
  chatBody.appendChild(consultantMsg);

  chatBody.scrollTop = chatBody.scrollHeight;
}

  scope.expose('clearChat', clearChat);
  scope.expose('sendChat', sendChat);
  scope.expose('sendQuestion', sendQuestion);
  scope.expose('handleTechnicalSupport', handleTechnicalSupport);
  scope.expose('startOpenAIChat', startOpenAIChat);
  scope.expose('restartConversation', restartConversation);
  scope.expose('handleYes', handleYes);
  scope.expose('handleNo', handleNo);
};
