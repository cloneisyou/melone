# Plan: Implement commit units 09 through 12 from docs/dev/implement-plan.md sequentiall

Branch: codex/implement-commit-units-09-through-12-from-docs-dev-implement-plan.md-sequentiall

## Intent

Implement commit units 09 through 12 from docs/dev/implement-plan.md sequentially. Units 01 through 08 are already implemented on main. Keep the code clean, simple, readable, and avoid overengineering.

## Working Notes

- Keep each commit focused on the matching source unit from `docs/dev/implement-plan.md`.
- Build on the existing Python service package under `apps/service`.
- Reuse the current `NormalizedEvent`, `normalize_event`, `EventRepository`, `Collector` protocol, permission helpers, and service loop patterns.
- Do not add Electron, HTTP APIs, WebSocket, OCR, VLM, vector search, audio, or tray UI work.
- Do not store raw keyboard text. Store aggregate metadata only.
- Prefer small, injectable helpers so macOS-specific APIs can be unit tested without requiring live permissions.
- Run tests from `apps/service`; use the repo virtualenv if present, otherwise create one as needed.

## Commit Units

### Commit 1: Unit 09 - Add Browser URL Collector

Implement `docs/dev/implement-plan.md` Commit 09.

Scope:

- Add `melone_service/collectors/browser_url.py`.
- Detect whether the frontmost app is a supported browser and query its active tab URL.
- Support Chrome, Brave, Arc, Edge, Safari, and Orion mappings.
- Treat Firefox-family browsers as unsupported for URL lookup in this MVP.
- Add a small AppleScript execution helper around `osascript`, with dependency injection for tests.
- Implement Chromium-style and Safari-style URL lookup scripts.
- Keep the active app collector running even when browser URL lookup fails.
- Add a 1 second TTL cache so repeated polling does not hammer AppleEvents.
- Emit `browser_url_changed` only when the browser URL changes.
- Use the existing `record_apple_events_denied` helper when AppleEvents access is denied.
- Register the collector in the service collector list.
- Strengthen URL normalizer tests only where needed; preserve raw query parameters and fragments.

Implementation guidance:

- Reuse existing active app/window lookup code where practical instead of introducing a separate app-discovery abstraction.
- Store browser URL events through `normalize_event` with `source="browser_url"` and useful app/window metadata.
- Keep unsupported or missing URLs quiet unless there is a permission/error event worth recording.
- Avoid a broad browser automation framework; this should remain a small macOS collector.

Verification:

```bash
cd apps/service
pytest
melone start
# In Chrome, Arc, or Safari, navigate to a page.
melone events --since 10m --type browser_url_changed
melone timeline --since 10m
melone stop
```

Acceptance:

- At least one supported browser can produce a `browser_url_changed` event during manual macOS testing.
- Tests cover script selection, unsupported browsers, TTL behavior, URL change detection, and AppleEvents denied handling.

### Commit 2: Unit 10 - Add Keyboard Burst Collector

Implement `docs/dev/implement-plan.md` Commit 10.

Scope:

- Add `melone_service/collectors/keyboard.py`.
- Capture keyDown activity on macOS using CGEventTap or the smallest PyObjC wrapper that fits the existing dependency set.
- Aggregate key activity into short burst windows.
- Store aggregate metadata such as key count, special key count, and whether secure input was detected if available.
- Count the special keys or shortcuts required by the source plan: `Enter`, `Backspace`, `Cmd+C`, and `Cmd+V`.
- Emit `keyboard_burst` events for aggregate typing activity.
- Emit `clipboard_shortcut` events for copy and paste shortcuts.
- Mark the collector disabled or no-op when Accessibility permission or platform support is unavailable.
- Register the collector in the service collector list.

Implementation guidance:

- Never store raw typed text, key labels for ordinary characters, or clipboard contents.
- Keep event tap setup failure isolated to this collector; the service loop must keep running.
- Prefer a small event source class plus a pure burst aggregation function so tests can feed synthetic key events.
- Keep metadata names simple and stable, for example `key_count`, `enter_count`, `backspace_count`, `copy_count`, `paste_count`, `secure_input`.

Verification:

```bash
cd apps/service
pytest
melone start
# Type for 2-3 minutes and use Cmd+C/Cmd+V.
melone events --since 10m --type keyboard_burst
melone events --since 10m --type clipboard_shortcut
melone stop
```

Acceptance:

- Keyboard burst rows contain aggregate metadata only.
- Copy and paste shortcuts can be inspected as `clipboard_shortcut` events.
- Tests cover aggregation, special key counts, copy/paste detection, no raw text storage, and permission/platform fallback.

### Commit 3: Unit 11 - Add Mouse Activity Collector

Implement `docs/dev/implement-plan.md` Commit 11.

Scope:

- Add `melone_service/collectors/mouse.py`.
- Capture mouse activity on macOS using the same simple collector style as the keyboard work.
- Aggregate click count, scroll count, drag activity, move density, last position, and active display when available.
- Emit `mouse_activity` events at bounded intervals.
- Register the collector in the service collector list.

Implementation guidance:

- Do not store the raw mouse movement stream.
- Aggregate within short intervals so normal use does not create excessive database rows.
- Keep the collector no-op on unsupported platforms or missing Accessibility permission.
- Prefer testable data structures for mouse samples and a pure aggregation function.

Verification:

```bash
cd apps/service
pytest
melone start
# Click, scroll, move, and drag.
melone events --since 10m --type mouse_activity
melone stop
```

Acceptance:

- Mouse activity events provide enough metadata for activity classification.
- Event volume stays bounded by the aggregation interval.
- Tests cover click/scroll/drag/move aggregation, last position metadata, and fallback behavior.

### Commit 4: Unit 12 - Add Activity State Classifier

Implement `docs/dev/implement-plan.md` Commit 12.

Scope:

- Add `melone_service/pipeline/activity.py`.
- Classify user state as `active`, `reading`, or `idle` from recent keyboard and mouse events.
- Implement the MVP rules:
  - `idle`: no keyboard or mouse activity for at least the configured idle timeout, default 5 minutes.
  - `active`: keyboard burst, click, or scroll activity within the last 30 seconds.
  - `reading`: anything between active and idle.
- Add activity thresholds to `ServiceConfig` with minimal environment support only if consistent with existing config style.
- Store activity state as normalized `activity_state_changed` events when the state changes; avoid adding a new table unless a clear need appears.
- Implement `melone context` so it shows the latest app/window/url context and the current activity state.
- Add unit tests for the classifier and CLI context output.

Implementation guidance:

- Keep classification close to a pure function that accepts recent `NormalizedEvent` values and threshold settings.
- Use existing event queries where possible; add small repository helpers only if they make the code clearer.
- Do not implement screenshot/session metadata work from later commit units.
- Keep CLI output simple, consistent with current `status`, `events`, and `timeline` text output.

Verification:

```bash
cd apps/service
pytest
melone context
```

Acceptance:

- Test fixtures classify `active`, `reading`, and `idle` as expected.
- `melone context` reports the current activity state without requiring screenshot or session features.
- The service can persist state transitions without duplicating unchanged state every poll.

## Router Note

No PR lock or selected existing plan candidate; created a new plan.
