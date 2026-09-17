import json
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_new_chat_controller_replaces_stored_session_and_clears_history():
    controller_path = REPOSITORY_ROOT / "app/static/session-controller.js"
    script = """
const createSessionController = require(process.argv[1]);
const values = new Map([['chat_session_id', 'old-session']]);
const storage = {
  getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
  removeItem: key => values.delete(key),
};
const history = {
  entries: ['old user turn', 'old assistant turn'],
  replaceChildren() { this.entries = []; },
};
(async () => {
  const controller = createSessionController({
    storage,
    clearHistory: () => history.replaceChildren(),
  });
  await controller.replaceSession(async () => ({id: 'new-session'}));
  console.log(JSON.stringify({
    current: controller.sessionId,
    stored: storage.getItem('chat_session_id'),
    history: history.entries,
  }));
})().catch(error => { console.error(error); process.exit(1); });
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "current": "new-session",
        "stored": "new-session",
        "history": [],
    }


def test_frontend_loads_session_controller_before_application_script(client):
    html = client.get("/").text

    assert html.index("/static/session-controller.js") < html.index("/static/app.js")
