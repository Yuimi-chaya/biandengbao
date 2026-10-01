"""Loopback-only synthetic UI demo. Never reads real Codex configuration."""
import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]
from test_bridge import DesktopFixture, THREAD
from bridge.httpd import GatewayServer
from bridge.service import Bridge

MODEL = "demo-codex"
PROJECT = "C:/Demo/biandengbao" if os.name == "nt" else "/demo/biandengbao"
ANSWER = """## 移动端布局已整理

这轮把工作区和线程阅读做了统一，手机上也能接着工作。

- **项目分组**：展开和收起都保留，无归属线程放最后。
- **阅读位置**：历史加载不会把正在看的内容推走。
- **输入区**：保留附件、模型、Skill 和独立的上下文压缩入口。

| 检查项 | 合成场景结果 |
| --- | --- |
| 手机排版 | 没有横向溢出 |
| 线程目录 | 可以按轮次跳转 |
| 推理强度 | 与选择的 high 一致 |

```css
.composer {
  flex-shrink: 0;
  min-height: 0;
}
```

接下来可以在自己的手机上确认键盘弹起时的手感。
"""


def user(item_id, text):
    return {"id": item_id, "type": "userMessage",
            "content": [{"type": "text", "text": text}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--control", type=Path, default=REPO / ".tmp/demo-control.json")
    parser.add_argument("--stop-file", type=Path, default=REPO / ".tmp/demo.stop")
    parser.add_argument("--duration", type=int, default=1800)
    args = parser.parse_args()
    (REPO / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(REPO / ".tmp"), prefix="demo-") as temp:
        root = Path(temp)
        fixture = DesktopFixture(root)
        started = 1790863200000
        turns = [
            {"turnId": "layout", "status": "completed", "turnStartedAtMs": started,
             "durationMs": 65000, "items": [
                 user("layout-input", "整理一下手机工作区：按项目分组，同时保留最后一条输入预览。"),
                 {"id": "layout-reason", "type": "reasoning",
                  "summary": ["**检查项目与线程布局**\n\n先对齐分组和预览，再检查小屏幕宽度。"],
                  "content": ["这是合成思考文本，不是模型的隐藏思维链。"]},
                 {"id": "layout-tool", "type": "commandExecution",
                  "command": "python -m unittest discover -s tests",
                  "aggregatedOutput": "Synthetic result: 88 tests passed.",
                  "status": "completed", "exitCode": 0},
                 {"id": "layout-answer", "type": "agentMessage",
                  "phase": "final_answer", "text": "工作区已整理：项目可以展开或收起，线程旁边保留最后输入预览，无归属线程放在最后。"}]},
            {"turnId": "mobile", "status": "completed", "turnStartedAtMs": started + 180000,
             "durationMs": 92000, "items": [
                 user("mobile-input", "继续优化手机输入框和阅读体验，别让历史加载把视线推走。"),
                 {"id": "mobile-reason", "type": "reasoning",
                  "summary": ["**确认输入区与阅读位置**\n\n保留当前可见内容，单独处理键盘视窗变化。"],
                  "content": []},
                 {"id": "mobile-tool", "type": "commandExecution",
                  "command": "node tests/thread-ui.test.cjs",
                  "aggregatedOutput": "Synthetic result: 25 assertions passed.",
                  "status": "completed", "exitCode": 0},
                 {"id": "mobile-answer", "type": "agentMessage",
                  "phase": "final_answer", "text": ANSWER}]}]
        fixture.state.update(
            cwd=PROJECT, title="手机工作区与阅读体验",
            latestModel=MODEL, latestReasoningEffort="high",
            latestThreadSettings={"model": MODEL, "effort": "high"},
            latestTokenUsageInfo={"last": {"totalTokens": 64000},
                                 "total": {"totalTokens": 960000},
                                 "modelContextWindow": 200000},
            threadRuntimeStatus={"type": "idle"}, turns=copy.deepcopy(turns))
        database = sqlite3.connect(root / "state_5.sqlite")
        database.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, updated_at INT, archived INT, originator TEXT, source TEXT, rollout_path TEXT)")
        rows = [
            (THREAD, "手机工作区与阅读体验", PROJECT, "继续优化手机输入框和阅读体验，别让历史加载把视线推走。"),
            (str(uuid.uuid4()), "上下文占用与手动压缩", PROJECT, "显示当前窗口占用，不要把累计 token 当作上下文。"),
            (str(uuid.uuid4()), "新建线程的模型选择", PROJECT, "新建时让我明确选项目、模型 ID 和推理强度。"),
            (str(uuid.uuid4()), "商品详情页排版", PROJECT + "-shop", "把规格和配送信息放得紧凑一点，手机上也要清楚。"),
            (str(uuid.uuid4()), "购物车状态检查", PROJECT + "-shop", "检查删除商品和修改数量后，总价有没有正确更新。"),
            (str(uuid.uuid4()), "周末路线整理", PROJECT + "-notes", "把两天的行程整理成一份容易查阅的清单。"),
            (str(uuid.uuid4()), "随手问一句", "/demo/unassigned", "帮我把这段说明改得简洁一些，别绕弯子。")]
        for index, (thread_id, title, cwd, prompt) in enumerate(rows):
            rollout = root / "sessions" / (thread_id + ".jsonl")
            rollout.parent.mkdir(exist_ok=True)
            rollout.write_text(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": prompt}}, ensure_ascii=False), encoding="utf-8")
            database.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)",
                             (thread_id, title, cwd, 1790864000 - index * 180,
                              0, "Codex Desktop", "vscode", str(rollout)))
        database.commit()
        database.close()
        bridge = Bridge(root, root / "data")
        bridge.ipc.path = fixture.path
        projects = [
            {"id": "bridge", "name": "便蹬宝", "rootPaths": [PROJECT]},
            {"id": "shop", "name": "小店计划", "rootPaths": [PROJECT + "-shop"]},
            {"id": "notes", "name": "生活笔记", "rootPaths": [PROJECT + "-notes"]}]
        bridge.hosts.state = lambda: {"local-projects": {p["id"]: p for p in projects}}
        bridge.catalog_reader.get = lambda *a, **k: {"models": [
            {"id": MODEL, "name": "Demo Codex", "efforts": ["low", "medium", "high", "xhigh"],
             "defaultEffort": "medium"},
            {"id": "demo-fast", "name": "Demo Fast", "efforts": ["low", "medium", "high"],
             "defaultEffort": "low"}], "skills": []}

        def publish():
            fixture.revision += 1
            if fixture.client:
                fixture.snapshot()

        class Tools:
            caller = THREAD

            def tools(self):
                return {"create_thread": {}}

            def call(self, name, arguments):
                if name == "list_projects":
                    return {"projects": [{"projectId": p["id"], "label": p["name"],
                                          "projectKind": "local", "hostId": "local",
                                          "hostDisplayName": "此电脑"} for p in projects]}
                # Reuse only the synthetic thread; no desktop or model call occurs.
                fixture.state.update(title=arguments.get("title") or "新建演示线程",
                                     latestModel=arguments["model"],
                                     latestReasoningEffort=arguments["thinking"],
                                     latestThreadSettings={"model": arguments["model"],
                                                           "effort": arguments["thinking"]})
                fixture.state["turns"].append({
                    "turnId": "created-" + uuid.uuid4().hex, "status": "completed",
                    "turnStartedAtMs": started + 360000, "durationMs": 1200,
                    "items": [user("created-input", arguments["prompt"]),
                              {"id": "created-answer", "type": "agentMessage",
                               "phase": "final_answer",
                               "text": "已收到这条演示需求。此处是合成执行结果，没有调用真实模型。"}]})
                publish()
                return {"threadId": THREAD, "hostId": "local"}

        bridge.desktop_tools = Tools()
        native_call = bridge._call

        def call(session, method, params, timeout=30):
            result = native_call(session, method, params, timeout)
            if method == "thread-follower-compact-thread":
                fixture.state["latestTokenUsageInfo"]["last"]["totalTokens"] = 16000
                fixture.state["turns"].append({
                    "turnId": "compact", "status": "completed",
                    "turnStartedAtMs": started + 480000, "durationMs": 1600,
                    "items": [{"id": "compact", "type": "contextCompaction",
                               "status": "completed"}]})
                publish()
            if method == "thread-follower-interrupt-turn":
                fixture.state["threadRuntimeStatus"] = {"type": "idle"}
                fixture.state["turns"][-1]["status"] = "interrupted"
                publish()
            return result

        bridge._call = call
        origin = "http://127.0.0.1:" + str(args.port)
        server = GatewayServer(("127.0.0.1", args.port), bridge,
                               {"auth": {"mode": "none"}, "origins": [origin]}, REPO / "web")
        server.timeout = .08
        print("Synthetic demo only: " + origin, flush=True)
        print("Thread: " + THREAD, flush=True)
        mode, last_control, streaming_at = "idle", None, None
        try:
            deadline = time.monotonic() + args.duration
            while not args.stop_file.exists() and time.monotonic() < deadline:
                server.handle_request()
                control = args.control.read_text(encoding="utf-8") if args.control.exists() else ""
                if control != last_control:
                    last_control = control
                    mode = json.loads(control).get("mode", "idle") if control else "idle"
                    fixture.state["title"] = "手机工作区与阅读体验"
                    fixture.state["turns"] = copy.deepcopy(turns)
                    fixture.state["latestTokenUsageInfo"]["last"]["totalTokens"] = 64000
                    if mode == "stream":
                        fixture.state["threadRuntimeStatus"] = {"type": "active"}
                        fixture.state["turns"][-1].update(status="inProgress")
                        fixture.state["turns"][-1]["items"][-1]["text"] = ""
                        streaming_at = time.monotonic()
                    else:
                        fixture.state["threadRuntimeStatus"] = {"type": "idle"}
                        streaming_at = None
                    publish()
                if streaming_at is not None:
                    elapsed = time.monotonic() - streaming_at
                    text = ANSWER[:int(elapsed * 55)]
                    item = fixture.state["turns"][-1]["items"][-1]
                    if text != item["text"]:
                        item["text"] = text
                        publish()
                    if len(text) == len(ANSWER):
                        fixture.state["threadRuntimeStatus"] = {"type": "idle"}
                        fixture.state["turns"][-1]["status"] = "completed"
                        streaming_at = None
                        publish()
        finally:
            server.server_close()
            bridge.close()
            fixture.close()


if __name__ == "__main__":
    main()
