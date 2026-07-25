"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

test("拡張はManifest V3でClassroom以外へcontent scriptを注入しない", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.content_scripts.flatMap((entry) => entry.matches), [
    "https://classroom.google.com/*",
    "https://classroom-grader-1.tail80e540.ts.net/ui/*",
  ]);
  assert.equal(manifest.version, "0.5.4");
  assert.deepEqual(manifest.host_permissions, [
    "https://classroom-grader-1.tail80e540.ts.net/*",
  ]);
});

test("公開サーバーを既定値にし保存済みURLを上書きしない", () => {
  const expected = "https://classroom-grader-1.tail80e540.ts.net";
  const background = fs.readFileSync(path.join(root, "background.js"), "utf8");
  const options = fs.readFileSync(path.join(root, "options.js"), "utf8");
  assert.match(background, new RegExp(expected.replaceAll(".", "\\.")));
  assert.match(options, new RegExp(expected.replaceAll(".", "\\.")));
  assert.match(options, /storage\.sync\.get\(\{ serverUrl: DEFAULT_SERVER_URL \}/);
  assert.doesNotMatch(options, /storage\.sync\.set\(\{ serverUrl: DEFAULT_SERVER_URL \}/);
});

test("WebUI bridgeとbackgroundがoriginを二重確認しqueueをsyncへ保存しない", () => {
  const background = fs.readFileSync(path.join(root, "background.js"), "utf8");
  const bridge = fs.readFileSync(path.join(root, "bridge.js"), "utf8");
  assert.match(bridge, /event\.source === window/);
  assert.match(bridge, /location\.origin !== EXPECTED_ORIGIN/);
  assert.match(bridge, /BRIDGE_VERSION = chrome\.runtime\.getManifest\(\)\.version/);
  assert.match(bridge, /CGA_EXTENSION_BRIDGE_PROBE_V1/);
  assert.match(bridge, /CGA_EXTENSION_BRIDGE_READY_V1/);
  assert.match(bridge, /local_queue_v1/);
  assert.match(background, /senderUrl\.origin !== WEB_UI_ORIGIN/);
  assert.match(background, /!senderUrl\.pathname\.startsWith\("\/ui\/"\)/);
  assert.match(background, /chrome\.storage\.session \|\| chrome\.storage\.local/);
  assert.doesNotMatch(background, /storage\.sync[^\n]*localDraftQueuesV1/);
});

test("拡張に返却・提出取消・Classroom API書込み経路を持たせない", () => {
  const source = ["background.js", "content.js", "selectors.js"]
    .map((name) => fs.readFileSync(path.join(root, name), "utf8"))
    .join("\n");
  for (const forbidden of ["/return", "/reclaim", "turnIn", "studentSubmissions", "checkbox"]) {
    assert.equal(source.includes(forbidden), false, `${forbidden} must not be present`);
  }
  assert.match(source, /pairings\/claim/);
  assert.match(source, /device-batches\/claim/);
  assert.match(source, /chrome\.storage\.local/);
  assert.doesNotMatch(source, /storage\.sync\.(?:set|get)\([^)]*deviceToken/);
  assert.match(source, /batches\/\$\{id\}\/consume/);
  assert.match(source, /chrome\.storage\.session/);
  assert.match(source, /summary\.filled > 0/);
  assert.match(source, /draft-input-jobs\/pending/);
  assert.match(source, /draft-input-jobs\/\$\{jobId\}\/progress/);
  assert.match(source, /parseClassroomContext\(sender\.url/);
  assert.doesNotMatch(source, /console\.(?:log|info|debug)/);
  assert.match(source, /activeEditInput\(document, window\)/);
  assert.doesNotMatch(source, /document\.querySelector\(CgaSelectors\.SELECTORS\.editInput\)/);
  assert.match(source, /applyLocalGradeOutcome/);
  assert.match(source, /gradeButtonForStudent\(studentId, studentName, document\)/);
  assert.match(source, /pendingStudentForGradeButton/);
});

test("下書き削除は同一画面で入力成功した記録と現在値の一致を必須にする", () => {
  const content = fs.readFileSync(path.join(root, "content.js"), "utf8");
  assert.match(content, /const undoRecords = new Map\(\)/);
  assert.match(content, /if \(outcome === "filled"\) recordFilled/);
  assert.match(content, /Number\(initial\) !== Number\(expectedScore\)/);
  assert.match(content, /if \(!confirm\(/);
  assert.match(content, /clearButton\.disabled = undoRecords\.size === 0/);
  assert.doesNotMatch(content, /chrome\.storage/);
});
