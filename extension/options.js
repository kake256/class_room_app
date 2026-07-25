"use strict";
const DEFAULT_SERVER_URL = "https://classroom-grader-1.tail80e540.ts.net";
const server = document.getElementById("server");
const status = document.getElementById("status");

function showStatus(kind, message) {
  status.className = kind;
  status.textContent = message;
}

function verifiedDeviceStatus() {
  return new Promise((resolve, reject) => chrome.runtime.sendMessage(
    { type: "verifiedDeviceStatus" },
    (reply) => {
      if (chrome.runtime.lastError) reject(new Error("拡張機能の状態を確認できませんでした。"));
      else resolve(reply || {});
    }
  ));
}

async function checkConnection() {
  showStatus("warning", "サーバーと端末の接続状態を確認しています…");
  try {
    const serverUrl = CgaCore.validateServerUrl(server.value);
    const origin = CgaCore.hostPermissionPattern(serverUrl);
    const allowed = await chrome.permissions.contains({ origins: [origin] });
    if (!allowed) throw new Error("サーバーへの接続許可がありません。「保存して接続を許可」を押してください。");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    let response;
    try {
      response = await fetch(`${serverUrl}/health`, {
        cache: "no-store", credentials: "omit", signal: controller.signal
      });
    } finally {
      clearTimeout(timeout);
    }
    if (!response.ok) throw new Error(`サーバーが応答しません（HTTP ${response.status}）。`);
    const device = await verifiedDeviceStatus();
    if (device.valid) {
      showStatus("success", "接続済みです。Classroom採点画面では「最新の下書きを取得して入力」を押すだけで利用できます。");
    } else if (device.localToken) {
      showStatus("warning", "サーバー接続は成功しましたが、この端末の接続は失効済みまたは期限切れです。「この端末を再接続」を押し、新しい初回接続コードを入力してください。");
    } else {
      showStatus("warning", "サーバー接続は成功しました。未接続の端末です。Web UIの初回接続コードをClassroom採点画面へ入力してください。");
    }
  } catch (error) {
    const message = error.name === "AbortError" ? "サーバー接続がタイムアウトしました。" : error.message;
    showStatus("error", `接続確認に失敗しました: ${message}`);
  }
}

chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL }, (data) => {
  server.value = data.serverUrl;
  checkConnection();
});

document.getElementById("save").addEventListener("click", async () => {
  try {
    const serverUrl = CgaCore.validateServerUrl(server.value);
    const granted = await chrome.permissions.request({
      origins: [CgaCore.hostPermissionPattern(serverUrl)]
    });
    if (!granted) throw new Error("接続許可がありません。");
    await chrome.storage.sync.set({ serverUrl });
    server.value = serverUrl;
    await checkConnection();
  } catch (error) {
    showStatus("error", `保存できませんでした: ${error.message}`);
  }
});

document.getElementById("check").addEventListener("click", checkConnection);
document.getElementById("unpair").addEventListener("click", () => {
  if (!confirm("このブラウザの端末tokenを削除し、初回接続コードで再接続しますか？")) return;
  chrome.runtime.sendMessage({ type: "unpairDevice" }, (reply) => {
    if (reply?.ok) {
      showStatus("warning", "この端末の接続を解除しました。Web UIで新しい初回接続コードを発行し、Classroom採点画面へ入力してください。");
    } else {
      showStatus("error", "この端末の接続を解除できませんでした。");
    }
  });
});
