export function captureSelection() {
  const MAX_ANCESTOR_LEVELS = 4;
  const MAX_ELEMENTS_PER_LEVEL = 120;
  const MAX_CANDIDATE_URLS = 8;
  const pageUrl = globalThis.location.href;
  const selection = globalThis.getSelection();

  if (!selection || selection.rangeCount === 0) {
    return {
      ok: false,
      page_url: pageUrl,
      error: {
        code: "empty_selection",
        message: "Select visible text before capturing it.",
      },
    };
  }

  const exactText = selection.toString();
  if (exactText.trim().length === 0) {
    return {
      ok: false,
      page_url: pageUrl,
      error: {
        code: "empty_selection",
        message: "Select visible text before capturing it.",
      },
    };
  }

  const range = selection.getRangeAt(0);
  let ancestor =
    range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
      ? range.commonAncestorContainer
      : range.commonAncestorContainer.parentElement;

  if (!ancestor || ancestor === document.body || ancestor === document.documentElement) {
    return {
      ok: false,
      page_url: pageUrl,
      error: {
        code: "selection_too_broad",
        message: "Select text within one bounded item before capturing it.",
      },
    };
  }

  function isVisible(element) {
    if (element.hidden || element.getAttribute("aria-hidden") === "true") {
      return false;
    }

    const style = globalThis.getComputedStyle(element);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      element.getClientRects().length > 0
    );
  }

  function isPermalinkCandidate(anchor) {
    return (
      anchor.matches("[data-permalink], [data-hasbaratops-permalink], [rel~='bookmark']") ||
      anchor.querySelector("time") !== null
    );
  }

  function collectCandidateUrls(root) {
    const urls = [];
    const seen = new Set();
    const stack = [root];
    let visited = 0;

    while (
      stack.length > 0 &&
      visited < MAX_ELEMENTS_PER_LEVEL &&
      urls.length < MAX_CANDIDATE_URLS
    ) {
      const element = stack.pop();
      visited += 1;

      if (
        element instanceof HTMLAnchorElement &&
        isPermalinkCandidate(element) &&
        isVisible(element)
      ) {
        const url = element.href;
        if (
          (url.startsWith("https://") || url.startsWith("http://")) &&
          !seen.has(url)
        ) {
          seen.add(url);
          urls.push(url);
        }
      }

      const children = element.children;
      for (let index = children.length - 1; index >= 0; index -= 1) {
        stack.push(children[index]);
      }
    }

    return urls;
  }

  let candidateUrls = [];
  let level = 0;

  while (
    ancestor &&
    ancestor !== document.body &&
    ancestor !== document.documentElement &&
    level < MAX_ANCESTOR_LEVELS
  ) {
    candidateUrls = collectCandidateUrls(ancestor);
    if (candidateUrls.length > 0) {
      break;
    }

    ancestor = ancestor.parentElement;
    level += 1;
  }

  return {
    ok: true,
    page_url: pageUrl,
    exact_text: exactText,
    candidate_urls: candidateUrls,
    warnings:
      candidateUrls.length === 0 ? ["missing_permalink_candidate"] : [],
  };
}
