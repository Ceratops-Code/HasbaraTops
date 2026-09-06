import { captureSelection } from "./capture.js";

const MENU_ITEMS = Object.freeze([
  {
    id: "hasbaratops-use-post-context",
    title: "Start new discussion from here (new case)",
    role: "post_context",
  },
  {
    id: "hasbaratops-discuss-comment",
    title: "Respond to this comment",
    role: "target_comment",
  },
  {
    id: "hasbaratops-capture-published-reply",
    title: "This is my reply - save it",
    role: "published_reply",
  },
]);

const MENU_ROLES = new Map(MENU_ITEMS.map(({ id, role }) => [id, role]));
const PANEL_PORT_NAME = "hasbaratops-sidepanel";
const panelPorts = new Set();
let currentCaptureState = null;

async function configureAction() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

function registerContextMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const item of MENU_ITEMS) {
      chrome.contextMenus.create({
        id: item.id,
        title: item.title,
        contexts: ["selection"],
      });
    }
  });
}

function publishToPanels(message) {
  for (const port of panelPorts) {
    port.postMessage(message);
  }
}

function invalidateCapture(reason) {
  if (!currentCaptureState) {
    return;
  }

  currentCaptureState = null;
  publishToPanels({
    type: "capture-invalidated",
    reason,
  });
}

function compactCaptureError(error) {
  const message =
    error instanceof Error && error.message
      ? error.message
      : "Chrome could not capture the current selection.";

  return {
    code: "capture_unavailable",
    message,
  };
}

async function captureFromTab(tab, role, captureSource) {
  currentCaptureState = null;

  if (!Number.isInteger(tab?.id)) {
    publishToPanels({
      type: "capture-error",
      error: {
        code: "missing_active_tab",
        message: "No active tab is available for capture.",
      },
    });
    return;
  }

  publishToPanels({ type: "capture-pending" });

  try {
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "ISOLATED",
      func: captureSelection,
    });
    const result = injection?.result;

    if (!result?.ok) {
      publishToPanels({
        type: "capture-error",
        error:
          result?.error ?? {
            code: "capture_unavailable",
            message: "Chrome returned no selection capture.",
          },
      });
      return;
    }

    const capture = {
      schema_version: 1,
      capture_id: crypto.randomUUID(),
      captured_at: new Date().toISOString(),
      role,
      page_url: result.page_url,
      exact_text: result.exact_text,
      candidate_urls: result.candidate_urls,
      capture_source: captureSource,
      confidence: "needs_user_confirmation",
      warnings: result.warnings,
    };

    currentCaptureState = {
      capture,
      tabId: tab.id,
    };
    publishToPanels({
      type: "capture-ready",
      capture,
      source_tab_id: tab.id,
    });
  } catch (error) {
    publishToPanels({
      type: "capture-error",
      error: compactCaptureError(error),
    });
  }
}

async function captureFromActiveTab(role) {
  try {
    const [tab] = await chrome.tabs.query({
      active: true,
      lastFocusedWindow: true,
    });
    await captureFromTab(tab, role, "toolbar_selection");
  } catch (error) {
    currentCaptureState = null;
    publishToPanels({
      type: "capture-error",
      error: compactCaptureError(error),
    });
  }
}

chrome.runtime.onInstalled.addListener(() => {
  registerContextMenus();
  void configureAction();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const role = MENU_ROLES.get(info.menuItemId);
  if (!role || !Number.isInteger(tab?.windowId)) {
    return;
  }

  void (async () => {
    try {
      await chrome.sidePanel.open({ windowId: tab.windowId });
      await captureFromTab(tab, role, "context_menu_selection");
    } catch (error) {
      currentCaptureState = null;
      publishToPanels({
        type: "capture-error",
        error: compactCaptureError(error),
      });
    }
  })();
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PANEL_PORT_NAME) {
    return;
  }

  panelPorts.add(port);
  if (currentCaptureState) {
    port.postMessage({
      type: "capture-ready",
      capture: currentCaptureState.capture,
      source_tab_id: currentCaptureState.tabId,
    });
  }

  port.onMessage.addListener((message) => {
    if (
      message?.type === "capture-request" &&
      ["post_context", "target_comment", "published_reply"].includes(message.role)
    ) {
      void captureFromActiveTab(message.role);
    }
  });

  port.onDisconnect.addListener(() => {
    panelPorts.delete(port);
  });
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  if (currentCaptureState && currentCaptureState.tabId !== tabId) {
    invalidateCapture("The active tab changed. Capture the selection again.");
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (
    currentCaptureState?.tabId === tabId &&
    (changeInfo.url !== undefined || changeInfo.status === "loading")
  ) {
    invalidateCapture("The source page navigated or reloaded. Capture it again.");
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (currentCaptureState?.tabId === tabId) {
    invalidateCapture("The source tab was closed. Capture a new selection.");
  }
});

void configureAction();
