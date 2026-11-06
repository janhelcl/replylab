# ReplyLab technical specification

This document implements the behavior in the [product specification](product-spec.md). The language, web framework, and first provider can be chosen during implementation.

## Architecture

Run one local service with SQLite. Serve the browser UI from the service and have the CLI call the same HTTP commands and subscribe to the same server-sent events (SSE). Put turn rules, branching, request projection, and run coordination in the service. Neither client talks directly to providers.

Use a single service process per database in the MVP. Serialize state-changing operations within a session and make durable changes transactional. Release database transactions before provider I/O. Separate the service into transcript and command logic, a run coordinator, persistence, and model adapters.

Bind to loopback by default and serve the browser from the same origin. Validate the local Host and Origin where present, use JSON for mutating requests, and keep cross-origin access disabled. Configure provider secrets in the service environment. Exclude credentials and authorization headers from stored attempts and error responses. The experiment database holds chat content and model inputs and should stay local by default.

## Persistent records

| Record | Minimum fields and invariants |
|---|---|
| Session | ID, created time, starting seat, optional opening prompt, immutable A and B configuration. AI config contains adapter, model identifier, system instructions, and generation options. |
| Branch | ID, session ID, head turn ID or null, source branch ID or null, intervention kind and source anchor, revision. Derive the next seat from its head and session setup. |
| Turn | ID, session ID, parent turn ID or null, author seat, exact text, created time, origin, optional producing attempt ID. Turns are immutable. |
| Run | ID, session and branch IDs, mode, turn limit, committed turn count, status, stop reason. A session can have at most one active run. |
| Attempt | ID, run and branch IDs, intended seat and starting head, model request record, text received, status, finish reason, optional provider usage and identifiers, timestamps. At most one in-flight attempt per run. |

A branch head points to a parent-linked chain of turns in the same session. Branches share unchanged ancestors. A plain replay creates a branch pointing to the appropriate existing turn, or null for the start. A truncation or edit creates a replacement turn with the selected turn's original parent and seat. The source branch continues to point to its existing head. A saved partial carries its starting head and intended seat so a fork can construct the same kind of replacement turn.

Create the replacement turn and the fork in one transaction. Confirm that a committed source turn belongs to the selected branch's history or that an attempt anchor belongs to that branch and ended without committing a turn. Resolve history in oldest-first order for display and projection. The first turn is by the configured starting seat. Each subsequent turn is by the opposite seat from its parent.

## Cursor and text contract

The shared cursor is a zero-based Unicode code-point offset into the stored message text. Offset `n` keeps the first `n` code points. Preserve source text without Unicode normalization. Reject invalid offsets and malformed Unicode. The browser converts native selection offsets from the raw text editor to code points. The CLI resolves its displayed one-based message number to an immutable turn ID before sending a command. Both use the same offset semantics.

For a committed turn, offset zero selects its parent, and the full text length selects the turn itself. An interior cursor creates a replacement prefix turn. For a saved partial from an uncommitted attempt, offset zero selects the attempt's starting head, and any valid nonempty prefix creates a new turn by the attempted speaker. An edit targets a committed turn ID and takes a complete replacement string. Preserve its seat and preceding history. Reject empty or whitespace-only replacements and committed prefixes.

## Commands and events

Use explicit session, branch, and run identifiers. A client can inspect any branch independently of the currently running branch.

| Command | Inputs and effect |
|---|---|
| Create session | Seat configs, starting seat, optional opening prompt. Create an empty root branch. |
| Read session or branch | Return branch lineage, ordered turns, next seat, revision, relevant attempt summaries, and active run state. |
| Send | Branch ID, expected revision, human seat, nonblank text. Append one turn when that seat is up and the session has no active run. |
| Next | Branch ID and expected revision. Start a one-attempt AI run or return a human-input state. |
| Play | Branch ID, expected revision, optional positive integer turn limit. Start bounded autoplay or return a human-input state. |
| Stop | Session and run ID. End that run. Repeating a stop for a terminal run returns its terminal state without affecting any later run. |
| Fork | Source branch and start, turn, or saved partial anchor. Supply a cursor, boundary, or replacement text as appropriate. Return the new branch and its revision. |
| Read attempt | Return recorded request, output or partial, terminal state, and available metadata. |

Branch writes that advance play or send a turn check the expected revision and the session run lock. Advance the revision when accepting a run start or appending a turn, so repeating a stale start or send cannot duplicate work. Return a conflict with current state for a stale revision or a busy session. A fork can run during generation when its anchor is already committed or terminal. It does not start generation.

Expose one session event stream with run, attempt, text delta, turn commit, and branch creation events. Include the relevant IDs and a monotonic event sequence. On subscribe or reconnect, send a snapshot with a sequence watermark, including current run state and buffered draft text. Then deliver events strictly after that watermark. Establish the snapshot and subscription without a gap. Clients replace local state on a fresh snapshot and ignore earlier events.

## Run and attempt lifecycle

1. Start a run and reserve the session in one transaction. Before each model call, record the expected branch head and the resolved request as an attempt. Stream visible text as a draft, with periodic persistence and a final checkpoint.
2. On a successful adapter result with nonblank text, commit one turn only if the run and attempt are still active and the branch head matches. In the same transaction, finalize the attempt, update the branch head and revision, and increment the run's committed count. Continue only if autoplay is active, its budget remains, and the next seat is an AI.
3. On stop, serialize against completion, end the run, and finalize any active attempt as stopped with its latest received text. Cancel upstream generation where supported. Drop events for attempts that have already been finalized. If completion wins the race, keep that committed turn and prevent further turns once the stop is accepted.
4. On provider error, timeout, blank output, or an output the adapter classifies as incomplete, finish the attempt with its partial and end the run. Keep the branch head unchanged for that attempt. A subsequent Next retries the same seat.
5. After an unclean service restart, close previously active runs and attempts as interrupted and recover their last persisted checkpoint. Resume with an explicit Next or Play.

If the upstream call continues briefly after a local stop, the run and attempt IDs prevent any late data from being committed. A client disconnect leaves the service-owned run active.

## Model adapter contract

Construct each request from the model seat's immutable configuration and the branch's committed history.

1. Apply only the active seat's system instructions through the provider's normal instruction mechanism.
2. If the active seat is the starting seat and an opening prompt exists, prepend it as a user message. Include it on later turns for that same seat.
3. Map turns authored by the active seat to assistant and the other seat's turns to user. Preserve message boundaries and exact content. Pass the result through the provider's ordinary text chat interface.

Keep branch metadata, draft output, the other seat's private settings, and playback controls out of the model input. Keep system instructions separate from chat text in the canonical representation. Require the first adapter to handle the ordinary alternating role sequence. Surface a context-limit error when the full branch exceeds supported input. Store the resolved model ID, generation options, and provider request body excluding secrets, along with completion metadata available from the adapter. Repeated generations may differ even for identical recorded inputs.

The adapter provides streaming visible text, cancellation where supported, and a terminal result with status, finish reason, and optional usage. Map provider outcomes to complete, incomplete, or failed explicitly. For the follow-up continuation feature, expose assistant-prefix capability separately. An adapter with that capability takes the prefix as part of the current assistant turn and yields the suffix. Use that suffix to commit one turn containing prefix plus suffix. A provider-specific continuation prompt would be a distinct mode because it changes the input.

## Implementation and verification

1. Build the transcript store and fork rules. Test both seat projections, opener visibility, empty history, branch ancestry, Unicode cursor boundaries, and immutability.
2. Implement the coordinator with a deterministic streaming fake. Test bounded autoplay, pauses at human seats, stale commands, concurrent starts, stop versus completion, late deltas, provider failure, reconnect, and crash recovery.
3. Add the shared service interface and both clients. Run the product acceptance scenarios through browser and CLI, including watching one client control the other's run.
4. Add one real text chat adapter. Smoke-test an AI pair and inspect both actual request bodies. Keep the fake available for repeatable checks.
5. Add same-speaker continuation as a separate increment. Test the prefilled human composer and use adapter tests for native AI prefix support and unsupported models.
