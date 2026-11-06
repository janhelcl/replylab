# ReplyLab

[![CI](https://github.com/janhelcl/replylab/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/janhelcl/replylab/actions/workflows/ci.yml)

ReplyLab is a local test harness for branching text conversations. Each of the two seats can be a person or a model. Run a conversation, change an earlier message, and see how the other seat responds. Every fork preserves the original conversation. The browser and CLI work with the same sessions.

## Quick start

You need Python 3.11 or newer. The demo runs without installing packages or configuring an API key.

```sh
git clone https://github.com/janhelcl/replylab.git
cd replylab
python -m replylab serve
```

1. Open the browser UI and select **New session**. Choose **Demo model** for both seats and enter an opening prompt such as “Name one use for a cardboard box.” [Open ReplyLab](http://127.0.0.1:8765)
2. Set **Turns** to 4 and click **Play**. Use **Next** for a single turn or **Stop** to interrupt playback. Autoplay also ends when a human seat is next.
3. Pick an earlier message and click **Replay**. Place the cursor inside its text and click **Reply from here**. The other seat gets only the prefix before your cursor on the new branch. Use **Edit and replay** to replace a message from either seat.
4. Click **Next** on the fork to get a fresh reply. Switch back to **Original** to compare. On a model turn, **Inspect** shows the recorded request, response, and any provider metadata.

A stopped generation keeps any text it received under **Uncommitted attempts**. Open the attempt and choose **Replay from this partial** to use a prefix as a committed message on a new branch.

## Run the same experiment from the CLI

Leave the service running and open a second terminal in the checkout. Create a demo session.

```sh
python -m replylab create --a fake --a-model Alpha --b fake --b-model Beta \
  --prompt "Name one use for a cardboard box."
```

Replace `SESSION` and `BRANCH` with the returned `session_id` and `branch_id`. Start playback, wait for it to finish, then read the transcript. You can follow the run in the browser or use `watch SESSION` in a separate terminal.

```sh
python -m replylab play SESSION BRANCH --limit 4
python -m replylab show SESSION BRANCH
```

The first demo response ends with “What follows from that?” Fork at offset 53 of message 1 to drop that line. The fork command returns the new ID under `branch.id`. Use it as `FORK` to continue the conversation.

```sh
python -m replylab fork SESSION BRANCH --at 1:53
python -m replylab next SESSION FORK
```

| Command | What it does |
| --- | --- |
| `sessions` / `branches SESSION` | List sessions or find a session’s branches. |
| `send SESSION BRANCH "Message"` | Submit a turn for the human seat that is next. |
| `stop SESSION` | Stop the session’s active run. |
| `fork SESSION BRANCH --before 2` | Replay from just before message 2. Use `--after 2` or `--start` for other boundaries. |
| `fork SESSION BRANCH --edit 1 --text "Replacement"` | Replace message 1 on a new branch and let the other seat answer. |
| `fork SESSION BRANCH --partial ATTEMPT:OFFSET` | Replay from a prefix of a saved partial response. |
| `attempt SESSION ATTEMPT_ID` | Inspect a model attempt. Find its ID in `show` or the event stream. |

Message numbers start at 1. An offset counts the Unicode code points to keep. `show --json` returns the branch as structured data. Run `python -m replylab --help` for the command list, or install the shorter `replylab` entry point with `python -m pip install .`.

## Model seats

| Seat | Browser | CLI |
| --- | --- | --- |
| Human | **Human** | `--a human` |
| Deterministic demo | **Demo model** | `--a fake --a-model Alpha` |
| OpenAI | **OpenAI** | `--a openai --a-model MODEL_ID` |

Use `--b` and the matching `--b-*` flags to configure the other seat. An AI opener requires an opening prompt. Set `--starter B` to start with seat B.

For OpenAI, set `OPENAI_API_KEY` in the service’s environment before starting it. Use a Chat Completions model ID your account can access. Configure each seat’s instructions and generation options in the browser or with flags such as `--a-instructions "Keep replies brief."` and `--a-options '{"max_completion_tokens": 512}'`. OpenAI requests may incur charges.

Each model sees its own previous turns as assistant messages and the other seat’s turns as user messages. Forks inherit the session’s seat configuration. Replay runs forward with fresh turns.

## Local data and configuration

The service binds to `127.0.0.1:8765` by default. It stores sessions, branches, transcripts, and model attempts in SQLite at `.replylab/replylab.sqlite3`, relative to the service’s working directory. OpenAI seats send their model requests to OpenAI.

- Choose a database with `serve --db PATH` or `REPLYLAB_DB`.
- Change the port with `serve --port PORT`. Point CLI commands to the matching address with `REPLYLAB_URL` or pass `--url URL` before the command.

## Development

Run the unit and CLI integration tests from the checkout.

```sh
python -m unittest discover -s tests -v
```

GitHub Actions also builds the package, installs the wheel, and tests browser and CLI interoperability with Chromium. To reproduce that check in a fresh virtual environment on Linux:

```sh
python -m pip install build==1.6.1 playwright==1.64.1
python -m build
python -m pip install dist/*.whl
python -m playwright install --with-deps --only-shell chromium
python tests/browser_smoke.py
```

[Product specification](docs/product-spec.md) · [Technical specification](docs/technical-spec.md) · [MIT license](LICENSE)
