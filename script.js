
"use strict";

// ================================================
// NOVAAI - UPDATED FRONTEND JAVASCRIPT
// ================================================

const CONFIG = {
    API_ENDPOINT: "/api/chat",
    STATUS_ENDPOINT: "/api/status",
    MAX_IMAGE_SIZE: 10 * 1024 * 1024,
    STATUS_REFRESH_INTERVAL: 60000
};

// ================================================
// DOM ELEMENTS
// ================================================

const elements = {
    chatArea: document.getElementById("chatArea"),
    chatContent: document.getElementById("chatContent"),
    welcome: document.getElementById("welcome"),

    userInput: document.getElementById("userInput"),
    sendBtn: document.getElementById("sendBtn"),

    provider: document.getElementById("provider"),

    imageInput: document.getElementById("imageInput"),
    preview: document.getElementById("preview"),
    previewImage: document.getElementById("previewImage"),

    history: document.getElementById("history"),
    sidebar: document.getElementById("sidebar")
};

// ================================================
// APPLICATION STATE
// ================================================

let chatHistory = [];
let selectedImage = null;
let imagePreviewURL = null;
let isSending = false;

let providerStatus = [];
let lastUsedProvider = null;

// ================================================
// PROVIDER INFORMATION
// ================================================

const PROVIDER_NAMES = {
    gemini: "Google Gemini",
    groq: "Groq",
    mistral: "Mistral AI",
    openrouter: "OpenRouter",
    sambanova: "SambaNova",
    cohere: "Cohere",
    cloudflare: "Cloudflare AI",
    nvidia: "NVIDIA NIM"
};

// ================================================
// INITIALIZATION
// ================================================

document.addEventListener("DOMContentLoaded", initialize);

function initialize() {

    // Send button
    if (elements.sendBtn) {
        elements.sendBtn.addEventListener("click", sendMessage);
    }

    // Enter to send
    if (elements.userInput) {
        elements.userInput.addEventListener("keydown", event => {

            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        });

        elements.userInput.addEventListener("input", autoResize);
    }

    // Image upload
    if (elements.imageInput) {
        elements.imageInput.addEventListener("change", handleImageUpload);
    }

    // Suggestions
    document.querySelectorAll(".suggestion").forEach(button => {
        button.addEventListener("click", () => {
            const prompt = button.dataset.prompt;

            if (prompt && elements.userInput) {
                elements.userInput.value = prompt;
                sendMessage();
            }
        });
    });

    // Mobile menu
    document.querySelectorAll(".mobile-menu").forEach(button => {
        button.addEventListener("click", toggleSidebar);
    });

    // Do not attach New Chat listeners here:
    // Existing HTML already uses onclick="newChat()".

    createProviderMonitor();

    refreshProviderStatus();

    setInterval(
        refreshProviderStatus,
        CONFIG.STATUS_REFRESH_INTERVAL
    );

    console.log("NovaAI initialized successfully.");
}

// ================================================
// PROVIDER MONITOR
// ================================================

function createProviderMonitor() {

    if (document.getElementById("providerMonitor")) {
        return;
    }

    const monitor = document.createElement("section");

    monitor.id = "providerMonitor";
    monitor.className = "provider-monitor";

    const heading = document.createElement("h3");
    heading.textContent = "AI Provider Status";

    const list = document.createElement("div");
    list.id = "providerStatusList";

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.textContent = "Refresh Status";
    refresh.className = "refresh-providers";

    refresh.addEventListener("click", refreshProviderStatus);

    monitor.append(heading, list, refresh);

    if (elements.sidebar) {
        elements.sidebar.appendChild(monitor);
    } else if (elements.provider?.parentElement) {
        elements.provider.parentElement.after(monitor);
    }
}

// ================================================
// REFRESH PROVIDER STATUS
// ================================================

async function refreshProviderStatus() {

    const list = document.getElementById("providerStatusList");

    if (list && !providerStatus.length) {
        list.textContent = "Checking AI providers...";
    }

    try {

        const response = await fetch(CONFIG.STATUS_ENDPOINT, {
            method: "GET",
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error("Could not retrieve provider status.");
        }

        const data = await response.json();

        providerStatus = data.providers || [];

        renderProviderStatus();

    } catch (error) {

        console.error("Provider status error:", error);

        if (list) {
            list.textContent = "Provider status unavailable.";
        }
    }
}

// ================================================
// RENDER PROVIDER STATUS
// ================================================

function renderProviderStatus() {

    const list = document.getElementById("providerStatusList");

    if (!list) return;

    list.replaceChildren();

    if (!providerStatus.length) {
        list.textContent = "No providers found.";
        return;
    }

    providerStatus.forEach(provider => {

        const card = document.createElement("div");
        card.className = "provider-card";

        const name = document.createElement("strong");
        name.textContent = provider.name;

        const status = document.createElement("span");
        status.className = "provider-state";

        const quota = provider.quota || {};

        let stateText = "Not configured";

        if (provider.configured) {

            if (quota.cooldown_seconds > 0) {
                stateText =
                    "Cooling down: " +
                    formatDuration(quota.cooldown_seconds);

            } else if (!quota.allowed) {
                stateText = "Local quota reached";

            } else {
                stateText = "Available";
            }
        }

        status.textContent = stateText;

        const details = document.createElement("small");

        const dailyText = quota.daily_limit
            ? `Daily: ${quota.daily_used}/${quota.daily_limit}`
            : "Daily: No local cap";

        const monthlyText = quota.monthly_limit
            ? `Monthly: ${quota.monthly_used}/${quota.monthly_limit}`
            : "Monthly: No local cap";

        details.textContent = dailyText + " | " + monthlyText;

        const model = document.createElement("small");
        model.textContent = provider.model
            ? "Model: " + provider.model
            : "Model not selected";

        card.append(name, status, details, model);

        list.appendChild(card);
    });
}

// ================================================
// FORMAT COOLDOWN
// ================================================

function formatDuration(seconds) {

    seconds = Math.max(0, Number(seconds) || 0);

    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;

    if (minutes >= 60) {
        const hours = Math.floor(minutes / 60);
        const remainingMinutes = minutes % 60;

        return `${hours}h ${remainingMinutes}m`;
    }

    return `${minutes}m ${remainingSeconds}s`;
}

// ================================================
// IMAGE UPLOAD
// ================================================

function handleImageUpload(event) {

    const file = event.target.files?.[0];

    if (!file) return;

    const allowedTypes = [
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif"
    ];

    if (!allowedTypes.includes(file.type)) {
        alert("Please select a JPG, PNG, WEBP or GIF image.");
        event.target.value = "";
        return;
    }

    if (file.size > CONFIG.MAX_IMAGE_SIZE) {
        alert("Image must be 10 MB or smaller.");
        event.target.value = "";
        return;
    }

    selectedImage = file;

    if (imagePreviewURL) {
        URL.revokeObjectURL(imagePreviewURL);
    }

    imagePreviewURL = URL.createObjectURL(file);

    if (elements.previewImage) {
        elements.previewImage.src = imagePreviewURL;
    }

    if (elements.preview) {
        elements.preview.style.display = "block";
    }
}

// ================================================
// REMOVE IMAGE
// ================================================

function removeImage() {

    selectedImage = null;

    if (imagePreviewURL) {
        URL.revokeObjectURL(imagePreviewURL);
        imagePreviewURL = null;
    }

    if (elements.previewImage) {
        elements.previewImage.removeAttribute("src");
    }

    if (elements.preview) {
        elements.preview.style.display = "none";
    }

    if (elements.imageInput) {
        elements.imageInput.value = "";
    }
}

// ================================================
// MESSAGE CREATION
// ================================================

function addMessage(role, content, imageURL = null, provider = null) {

    if (!elements.chatContent) return null;

    const message = document.createElement("div");

    message.className = `message ${role}-message`;

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";

    avatar.textContent = role === "user" ? "You" : "N";

    const body = document.createElement("div");
    body.className = "message-body";

    const text = document.createElement("div");
    text.className = "message-text";

    // textContent prevents HTML/script injection.
    text.textContent = content;

    body.appendChild(text);

    if (imageURL) {

        const image = document.createElement("img");

        image.src = imageURL;
        image.alt = "Uploaded image";
        image.className = "chat-image";

        body.appendChild(image);
    }

    if (provider) {

        const label = document.createElement("small");

        label.className = "message-provider";
        label.textContent =
            "Answered by " +
            (PROVIDER_NAMES[provider] || provider);

        body.appendChild(label);
    }

    message.append(avatar, body);

    elements.chatContent.appendChild(message);

    scrollToBottom();

    return message;
}

// ================================================
// LOADING INDICATOR
// ================================================

function showLoading() {

    const loading = document.createElement("div");

    loading.id = "novaLoading";
    loading.className = "message assistant-message";

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "N";

    const text = document.createElement("div");
    text.className = "message-text";
    text.textContent = "NovaAI is thinking...";

    loading.append(avatar, text);

    elements.chatContent.appendChild(loading);

    scrollToBottom();
}

function hideLoading() {

    document.getElementById("novaLoading")?.remove();
}

// ================================================
// SEND MESSAGE
// ================================================

async function sendMessage() {

    if (isSending) return;

    const message = elements.userInput?.value.trim() || "";

    if (!message && !selectedImage) return;

    const imageToSend = selectedImage;

    const imageURL = imagePreviewURL;

    const provider = elements.provider?.value || "auto";

    const formData = new FormData();

    formData.append("message", message);
    formData.append("provider", provider);

    // Previous messages only.
    formData.append("history", JSON.stringify(chatHistory));

    if (imageToSend) {
        formData.append("image", imageToSend);
    }

    isSending = true;

    if (elements.sendBtn) {
        elements.sendBtn.disabled = true;
        elements.sendBtn.textContent = "Sending...";
    }

    if (elements.welcome) {
        elements.welcome.style.display = "none";
    }

    if (elements.userInput) {
        elements.userInput.value = "";
    }

    autoResize();

    addMessage("user", message || "Please analyse this image.", imageURL);

    // Release the preview URL only after the image is loaded.
    if (imageURL) {
        const displayedImages = elements.chatContent?.querySelectorAll(
            ".chat-image"
        );

        const displayedImage = displayedImages?.[displayedImages.length - 1];

        if (displayedImage) {
            displayedImage.addEventListener(
                "load",
                () => URL.revokeObjectURL(imageURL),
                { once: true }
            );

            displayedImage.addEventListener(
                "error",
                () => URL.revokeObjectURL(imageURL),
                { once: true }
            );
        }
    }

    showLoading();

    // Clear selected image without revoking the URL being displayed.
    selectedImage = null;
    imagePreviewURL = null;

    if (elements.previewImage) {
        elements.previewImage.removeAttribute("src");
    }

    if (elements.preview) {
        elements.preview.style.display = "none";
    }

    if (elements.imageInput) {
        elements.imageInput.value = "";
    }

    try {

        const response = await fetch(CONFIG.API_ENDPOINT, {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        hideLoading();

        if (!response.ok) {
            throw new Error(data.error || "AI request failed.");
        }

        if (!data.reply) {
            throw new Error("AI returned an empty response.");
        }

        const reply = data.reply;

        lastUsedProvider = data.provider;

        chatHistory.push({
            role: "user",
            content: message || "Please analyse this image."
        });

        chatHistory.push({
            role: "assistant",
            content: reply
        });

        chatHistory = chatHistory.slice(-30);

        addMessage(
            "assistant",
            reply,
            null,
            data.provider
        );

        updateHistory(message || "Image analysis");

        // Refresh quota information after a successful response.
        refreshProviderStatus();

    } catch (error) {

        hideLoading();

        addMessage(
            "assistant",
            "Sorry! " + error.message
        );

        console.error("NovaAI error:", error);

        // Refresh status even after an unsuccessful request.
        refreshProviderStatus();

    } finally {

        isSending = false;

        if (elements.sendBtn) {
            elements.sendBtn.disabled = false;
            elements.sendBtn.textContent = "Send";
        }

        elements.userInput?.focus();
    }
}

// ================================================
// CHAT HISTORY
// ================================================

function updateHistory(title) {

    if (!elements.history) return;

    const item = document.createElement("div");

    item.className = "history-item";

    const text = document.createElement("span");

    text.textContent = title.slice(0, 35);

    item.appendChild(text);

    item.addEventListener("click", () => {
        alert(
            "This chat is stored only in the current session. " +
            "Persistent chat restoration will need a database feature."
        );
    });

    elements.history.prepend(item);
}

// ================================================
// NEW CHAT
// ================================================

function newChat() {

    if (isSending) return;

    chatHistory = [];

    lastUsedProvider = null;

    if (elements.chatContent) {
        elements.chatContent.replaceChildren();
    }

    if (elements.welcome) {
        elements.welcome.style.display = "flex";
    }

    if (elements.userInput) {
        elements.userInput.value = "";
    }

    removeImage();

    autoResize();
}

// ================================================
// SIDEBAR
// ================================================

function toggleSidebar() {

    if (!elements.sidebar) return;

    elements.sidebar.classList.toggle("active");
}

// ================================================
// INPUT AUTO RESIZE
// ================================================

function autoResize() {

    if (!elements.userInput) return;

    elements.userInput.style.height = "auto";

    elements.userInput.style.height =
        Math.min(elements.userInput.scrollHeight, 180) + "px";
}

// ================================================
// SCROLL
// ================================================

function scrollToBottom() {

    if (!elements.chatArea) return;

    elements.chatArea.scrollTo({
        top: elements.chatArea.scrollHeight,
        behavior: "smooth"
    });
}

// ================================================
// EXPORT CHAT
// ================================================

function exportChat() {

    if (!chatHistory.length) {
        alert("There is no conversation to export.");
        return;
    }

    let output = "NOVAAI CHAT EXPORT\n";
    output += "Date: " + new Date().toLocaleString() + "\n";
    output += "================================\n\n";

    chatHistory.forEach(item => {

        const role = item.role === "user" ? "YOU" : "NOVAAI";

        output += role + ":\n";
        output += item.content + "\n\n";
        output += "--------------------------------\n\n";
    });

    const blob = new Blob(
        [output],
        { type: "text/plain;charset=utf-8" }
    );

    const url = URL.createObjectURL(blob);

    const link = document.createElement("a");

    link.href = url;
    link.download = "NovaAI-Chat.txt";

    document.body.appendChild(link);
    link.click();
    link.remove();

    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ================================================
// GLOBAL FUNCTIONS FOR HTML onclick
// ================================================

window.sendMessage = sendMessage;
window.newChat = newChat;
window.toggleSidebar = toggleSidebar;
window.removeImage = removeImage;
window.exportChat = exportChat;
window.refreshProviderStatus = refreshProviderStatus;
