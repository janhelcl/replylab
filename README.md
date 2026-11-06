# ReplyLab

ReplyLab is a local harness for two-seat chat. Each seat can be a human or an AI. Run a conversation one turn at a time or on autoplay, then edit a message or branch from a point inside it. The original conversation stays available for comparison.

## Run locally

From a checkout with Python 3.11 or newer:

```sh
python -m replylab serve
```

Open [the browser UI](http://127.0.0.1:8765) and select **New session**. Choose **Demo model** for an AI seat that runs locally without credentials. If an AI opens, provide an opening prompt.

The service stores sessions in `.replylab/replylab.sqlite3`. Use `serve --db PATH --port PORT` to override the defaults. The service listens on loopback, and the CLI uses the same address as the browser. Set `REPLYLAB_URL` or pass `--url` before the CLI command when using a different port.

You can also install the `replylab` command with `python -m pip install .`.

## Use the CLI

In a second terminal, create a demo session. Copy the session and branch IDs into the next commands.

```sh
python -m replylab create --a fake --a-model Alpha --b fake --b-model Beta --prompt "Propose a short experiment."
python -m replylab play SESSION BRANCH --limit 4
python -m replylab show SESSION BRANCH
python -m replylab fork SESSION BRANCH --at 1:12
```

`--at 1:12` keeps the first 12 Unicode code points of message 1 and lets the other seat reply. `--before`, `--after`, and `--start` replay from turn boundaries. Use `fork SESSION BRANCH --edit 1 --text "Replacement"` to replace a past message on a new branch. Use `send SESSION BRANCH "Message"` for a human turn and `next SESSION BRANCH` for a single step. Run `watch SESSION` to stream events and `stop SESSION` to stop autoplay. Use `attempt SESSION ATTEMPT_ID` to inspect the resolved model request, output, and available provider metadata.

## Use an OpenAI seat

Set `OPENAI_API_KEY` in the environment where you start the service. In the browser choose **OpenAI** and enter a Chat Completions model ID your account can access. The CLI equivalent is `--a openai --a-model MODEL_ID` or `--b openai --b-model MODEL_ID`. Optional generation settings can be passed as a JSON object with `--a-options` or `--b-options`. API calls may incur charges.

## Design and tests

- [Product specification](docs/product-spec.md)
- [Technical specification](docs/technical-spec.md)

```sh
python -m unittest discover -s tests -v
```

[GitHub Actions CI](https://github.com/janhelcl/replylab/actions/workflows/ci.yml) runs lint, the Python test matrix, and a Chromium smoke test on pull requests and pushes to `main`. The browser test installs the built wheel and exercises playback, stop, and replay across the browser and the installed CLI using the demo model.

To run the browser check locally in a virtual environment:

```sh
python -m pip install build==1.6.1 playwright==1.64.1
python -m build
python -m pip install dist/*.whl
python -m playwright install chromium
python tests/browser_smoke.py
```
