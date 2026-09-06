const ROLE_LABELS = Object.freeze({
  post_context: "Post context",
  published_reply: "Published reply",
  target_comment: "Target comment",
});

const port = chrome.runtime.connect({ name: "hasbaratops-sidepanel" });
const statusHeading = document.querySelector("#status-heading");
const statusMessage = document.querySelector("#status-message");
const captureForm = document.querySelector("#capture-form");
const previewRole = document.querySelector("#preview-role");
const exactText = document.querySelector("#exact-text");
const candidateSelect = document.querySelector("#candidate-select");
const selectedUrl = document.querySelector("#selected-url");
const pageUrl = document.querySelector("#page-url");
const confirmedCapture = document.querySelector("#confirmed-capture");
const confirmedRole = document.querySelector("#confirmed-role");
const confirmedUrl = document.querySelector("#confirmed-url");
const confirmedText = document.querySelector("#confirmed-text");
let currentCapture = null;
let sourceTabId = null;

function setStatus(heading, message, state = "neutral") {
  statusHeading.textContent = heading;
  statusMessage.textContent = message;
  statusHeading.closest(".status").dataset.state = state;
}

function resetPreview() {
  currentCapture = null;
  sourceTabId = null;
  captureForm.hidden = true;
  confirmedCapture.hidden = true;
  exactText.value = "";
  selectedUrl.value = "";
  pageUrl.value = "";
  candidateSelect.replaceChildren(new Option("No candidate selected", ""));
}

function showCapture(capture, capturedTabId) {
  currentCapture = capture;
  sourceTabId = capturedTabId;
  confirmedCapture.hidden = true;
  previewRole.textContent = ROLE_LABELS[capture.role];
  exactText.value = capture.exact_text;
  selectedUrl.value = capture.candidate_urls[0] ?? "";
  pageUrl.value = capture.page_url;

  const options = [new Option("Paste or select a permalink", "")];
  for (const candidateUrl of capture.candidate_urls) {
    options.push(new Option(candidateUrl, candidateUrl));
  }
  candidateSelect.replaceChildren(...options);
  candidateSelect.value = selectedUrl.value;

  captureForm.hidden = false;
  const linkMessage =
    capture.candidate_urls.length === 0
      ? "No nearby permalink was found. Paste the exact URL before confirming."
      : capture.candidate_urls.length === 1
        ? "Review the exact text and nearby permalink before confirming."
        : "Multiple nearby permalinks remain. Choose the correct one before confirming.";
  setStatus("Selection captured", linkMessage, "ready");
}

for (const button of document.querySelectorAll("[data-role]")) {
  button.addEventListener("click", () => {
    resetPreview();
    setStatus("Capturing selection", "Reading only the current user selection.", "pending");
    port.postMessage({
      type: "capture-request",
      role: button.dataset.role,
    });
  });
}

candidateSelect.addEventListener("change", () => {
  if (candidateSelect.value) {
    selectedUrl.value = candidateSelect.value;
  }
});

captureForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!currentCapture) {
    return;
  }

  const correctedText = exactText.value;
  const correctedUrl = selectedUrl.value;

  if (correctedText.trim().length === 0) {
    setStatus("Text required", "The exact-text field cannot be empty.", "error");
    exactText.focus();
    return;
  }

  let parsedUrl;
  try {
    parsedUrl = new URL(correctedUrl);
  } catch {
    setStatus("Valid URL required", "Paste or select an exact HTTP or HTTPS URL.", "error");
    selectedUrl.focus();
    return;
  }

  if (!["http:", "https:"].includes(parsedUrl.protocol)) {
    setStatus("Valid URL required", "Only HTTP or HTTPS URLs can be confirmed.", "error");
    selectedUrl.focus();
    return;
  }

  confirmedRole.textContent = ROLE_LABELS[currentCapture.role];
  confirmedUrl.textContent = correctedUrl;
  confirmedText.textContent = correctedText;
  confirmedCapture.hidden = false;
  captureForm.hidden = true;
  setStatus(
    "Capture confirmed locally",
    "The confirmed context remains only in this extension panel.",
    "confirmed",
  );
});

port.onMessage.addListener((message) => {
  if (message?.type === "capture-pending") {
    resetPreview();
    setStatus("Capturing selection", "Reading only the current user selection.", "pending");
    return;
  }

  if (message?.type === "capture-ready") {
    showCapture(message.capture, message.source_tab_id);
    return;
  }

  if (message?.type === "capture-error") {
    resetPreview();
    setStatus("Capture blocked", message.error.message, "error");
    return;
  }

  if (message?.type === "capture-invalidated") {
    resetPreview();
    setStatus("Capture invalidated", message.reason, "error");
  }
});

function invalidateForSourceChange(message) {
  if (sourceTabId === null) {
    return;
  }

  resetPreview();
  setStatus("Capture invalidated", message, "error");
}

chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (sourceTabId !== null && sourceTabId !== tabId) {
    invalidateForSourceChange(
      "The active tab changed. Capture the selection again.",
    );
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (
    sourceTabId === tabId &&
    (changeInfo.url !== undefined || changeInfo.status === "loading")
  ) {
    invalidateForSourceChange(
      "The source page navigated or reloaded. Capture it again.",
    );
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (sourceTabId === tabId) {
    invalidateForSourceChange(
      "The source tab was closed. Capture a new selection.",
    );
  }
});
