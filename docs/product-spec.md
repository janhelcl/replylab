# ReplyLab product specification

## Purpose

ReplyLab lets an experimenter intervene in a chat and run it forward again. For example, run two AIs for six turns, cut an early answer after its first sentence, and see how the other seat responds to the shorter version. Keep both conversations so the intervention and its effects are visible.

The CLI and browser operate on the same sessions. To an AI seat, the conversation has the shape of an ordinary multi-turn chat.

## MVP scope

- Create a local text chat with two seats, A and B. Choose independently whether each seat is human or AI. Support human–human, human–AI in either order, and AI–AI.
- Configure the starting seat and an optional opening prompt. Require an opening prompt when the starting seat is an AI. Each AI seat has its own model, instructions, and generation settings.
- Enter human turns, stream AI turns, advance one turn at a time, run bounded autoplay, and stop an active run.
- Inspect any saved branch. Fork from the start, a turn boundary, or inside a message. Edit either seat's messages on a new branch.
- Inspect the input and output of each AI attempt. Run the MVP with one real model API adapter and a deterministic fake for development and tests.

The MVP assumes one trusted local operator. Two people can use the two seats through local clients. Seat configuration is fixed for a session so its branches can isolate transcript changes. Hosted collaboration, authentication between seats, tools, attachments, automatic context compression, and batch experiment orchestration are outside the MVP.

## Seats and opening prompt

The opening prompt is session setup addressed to the starting seat. Show it separately from the shared transcript. The opening seat sees it as user input and the other seat first sees the opening seat's reply. If the opener is human, show it above their composer as a prompt to answer.

| Shared transcript turn | Model A sees | Model B sees |
|---|---|---|
| A says “What would you try first?” | `assistant` | `user` |
| B says “I’d check the input data.” | `user` | `assistant` |

Messages preserve the authoring seat through edits and replays. A human may edit a message from either seat. The revised text occupies the same role in each model's view.

## Editing and replay

Use an immutable source branch and create a new branch for each intervention. The new branch inherits the prefix up to the selected point. Later turns are available on the source branch. A fork starts idle so the operator can inspect it before pressing **Next** or **Play**.

| Action | New branch | Next seat |
|---|---|---|
| **Replay from start** | Empty shared transcript with the original setup | Starting seat |
| **Replay before a turn** | All turns before the selected turn | Seat that authored the selected turn |
| **Replay after a turn** | All turns through the selected turn | Other seat |
| **Reply from here** inside a turn | Earlier turns plus a new committed turn containing the exact selected prefix | Other seat |
| **Edit and replay** | Earlier turns plus the edited message as one committed turn by its original seat | Other seat |
| **Continue from here** inside a turn, follow-up release | Earlier turns plus a draft containing the exact selected prefix | Same seat finishes the turn, then the other seat replies |

For example, A writes “I agree with the proposal, but the timing is risky.” Select the cursor after “I agree with the proposal”. **Reply from here** commits that prefix as A's full message and lets B answer it. **Continue from here** lets A complete the sentence differently.

A cursor at the beginning of a message means replay before it. A cursor at the end of a committed message means replay after it. A nonempty prefix from a saved stopped, failed, or interrupted generation can also seed a reply fork. Its full captured text can be selected. Whitespace within a selected prefix is preserved. An entirely whitespace-only prefix or edit is rejected.

Here, replay means running forward from the chosen transcript with fresh generations where an AI is speaking. A saved branch is the record of what happened in its original run.

## Playback and client parity

- **Next** on an AI seat starts exactly one attempt. On a human seat it focuses or opens the composer. The human commits the turn with **Send**.
- **Play** starts on an AI seat and commits at most the requested number of AI turns. It ends at a human turn, on stop, on error, or at the turn limit. The default is ten turns. Play at a human seat waits for input by opening the composer and requires another explicit Play to start autoplay later.
- **Stop** ends the run. If an attempt is active, preserve the received text as an uncommitted partial. The operator can retry the same turn or fork using the partial. The other seat only receives partial text if the operator commits it through a reply fork.
- The browser offers a transcript, current speaker and run status, composer, playback controls, branch navigation, and a cursor in raw message text for replay. The CLI exposes the same actions, with message number and character offset for precise cursor placement. Both expose stored attempts and resolved model inputs.
- A running branch is visibly marked. Switching the displayed branch leaves its run active, and its stop control remains available in both clients. New branches display their source and intervention point.

## MVP acceptance

1. An operator can create and complete at least two turns in each seat pairing. In the AI pair, both request logs show the correct role projection and only the opener receives the opening prompt.
2. A browser can start autoplay, a CLI can watch it and stop it, and both show the same committed transcript. The system does not commit the stopped attempt or advance its speaker.
3. A fork from the start, a boundary, a cursor inside a message, and an edit from each seat all produce the expected history and next speaker. The source branch still displays its later turns.
4. A reply fork from a stopped partial includes only the selected prefix and changes context only on the new branch. A retry on the source starts with the same committed history.
5. After a service restart, both clients can reopen the session and inspect branch history and saved AI attempts.

## Same-speaker continuation

Implement **Continue from here** after the MVP. For a human, prefill the composer. For an AI, offer it when the adapter supports supplying a native assistant prefix as part of the current turn. Display unsupported continuation explicitly. The completed turn stores the prefix and the generated suffix as one message. Keep the model's actual request inspectable in either case.
