"use strict";
(() => {
  const EXPECTED_ORIGIN = "https://classroom-grader-1.tail80e540.ts.net";
  const BRIDGE_VERSION = chrome.runtime.getManifest().version;
  const CAPABILITIES = ["local_queue_v1", "automatic_job_v1"];
  if (location.origin !== EXPECTED_ORIGIN || !location.pathname.startsWith("/ui/")) return;
  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== EXPECTED_ORIGIN) return;
    if (event.data?.type === "CGA_EXTENSION_BRIDGE_PROBE_V1"
        && /^[A-Za-z0-9_-]{16,80}$/.test(String(event.data.requestId || ""))) {
      window.postMessage({
        type: "CGA_EXTENSION_BRIDGE_READY_V1", requestId: event.data.requestId,
        version: BRIDGE_VERSION, capabilities: CAPABILITIES
      }, EXPECTED_ORIGIN);
      return;
    }
    let envelope;
    try {
      envelope = CgaCore.validateTransferEnvelope({
        sourceIsWindow: event.source === window, origin: event.origin, data: event.data
      }, EXPECTED_ORIGIN);
    } catch (_error) { return; }
    chrome.runtime.sendMessage({ type: "storeLocalQueue", payload: envelope.payload }, (reply) => {
      const ok = !chrome.runtime.lastError && reply?.ok === true;
      window.postMessage({
        type: "CGA_EXTENSION_TRANSFER_RESULT_V1", requestId: envelope.requestId,
        version: BRIDGE_VERSION, capabilities: CAPABILITIES,
        ok, count: ok ? Number(reply.count || 0) : 0,
        error: ok ? "" : (reply?.error || "拡張機能へ転送できませんでした。")
      }, EXPECTED_ORIGIN);
    });
  });
})();
