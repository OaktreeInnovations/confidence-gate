# Manual Intent Editing

When a test step keeps failing despite correct step actions, the AI-generated intent (the Playwright JSON the worker executes) may be wrong or stale. This guide explains how to inspect and override intents directly.

---

## How Intents Work

Each step execution follows this order:

1. **Cache lookup** — check `step_code_cache` collection for a previously successful intent
2. **AI generation** — if no cache, capture screenshot + accessibility tree → send to AI → get intent JSON
3. **Execute intent** — the worker runs the Playwright actions in the intent
4. **Verify** — screenshot taken → vision AI + DOM checks compare against `expected`
5. **Cache write** — on success, the intent is saved with a stability score

An intent is a JSON object:
```json
{
  "step_number": 29,
  "actions": [
    {
      "action": "click",
      "target": {
        "test_id": "delete-selected-btn"
      }
    }
  ],
  "description": "Click the delete selected button"
}
```

---

## Available Action Types

| Action | Required fields | Notes |
|---|---|---|
| `navigate` | `value` (URL) | No target needed |
| `click` | `target` | Use `test_id` when available |
| `input` | `target`, `value` | Types text into a field |
| `select` | `target`, `value` | Selects dropdown option; target = label, value = option text |
| `check` / `uncheck` | `target` | Checkboxes |
| `wait_for_text` | `value` (text) | Waits for text to appear on page |
| `wait_for_navigation` | `value` (URL glob) | e.g. `**/projects**` — waits for URL change |
| `assert_text` | `target`, `value` | Asserts element contains text — immediately passes DOM layer |
| `assert_visible` | `target` | Asserts element is visible — immediately passes DOM layer |

## Target Fields (selector priority, highest first)

```json
{
  "test_id": "data-testid value",
  "role": "ARIA role (button, textbox, checkbox...)",
  "name": "accessible name",
  "label": "associated label text",
  "placeholder": "placeholder attribute",
  "text": "visible text content",
  "css": "CSS selector (last resort)"
}
```

Only include fields that have a value — omit nulls.

---

## Inspecting a Cached Intent

```bash
docker exec qualora-mongo mongosh \
  "mongodb://qualora:qualora_dev_password@localhost:27017/qualora?authSource=admin" \
  --eval "
var cache = db.step_code_cache.findOne({
  test_case_id: '<TEST_CASE_ID>',
  step_number: <STEP_NUMBER>
});
print(cache.last_successful_code);
print('stability_score:', cache.stability_score);
"
```

To see all cached steps for a test case:

```bash
docker exec qualora-mongo mongosh \
  "mongodb://qualora:qualora_dev_password@localhost:27017/qualora?authSource=admin" \
  --eval "
db.step_code_cache
  .find({test_case_id: '<TEST_CASE_ID>'})
  .sort({step_number: 1})
  .forEach(c => {
    var intent = JSON.parse(c.last_successful_code);
    print('Step', c.step_number, '|', JSON.stringify(intent.actions));
  });
"
```

---

## Overriding an Intent

Patch the `last_successful_code` field directly. Set `stability_score: 1.0` to prevent the worker from regenerating.

```bash
docker exec qualora-mongo mongosh \
  "mongodb://qualora:qualora_dev_password@localhost:27017/qualora?authSource=admin" \
  --eval "
db.step_code_cache.updateOne(
  {test_case_id: '<TEST_CASE_ID>', step_number: <STEP_NUMBER>},
  {'\$set': {
    last_successful_code: JSON.stringify({
      step_number: <STEP_NUMBER>,
      actions: [
        {action: 'click', target: {test_id: 'my-button'}}
      ],
      description: 'Click the my-button element'
    }),
    code_type: 'intent',
    stability_score: 1.0
  }}
);
"
```

---

## Clearing a Cached Intent

Force the worker to regenerate intent from AI on the next run:

```bash
# Clear one step
docker exec qualora-mongo mongosh \
  "mongodb://qualora:qualora_dev_password@localhost:27017/qualora?authSource=admin" \
  --eval "
db.step_code_cache.deleteOne({
  test_case_id: '<TEST_CASE_ID>',
  step_number: <STEP_NUMBER>
});
"

# Clear multiple steps
db.step_code_cache.deleteMany({
  test_case_id: '<TEST_CASE_ID>',
  step_number: {$in: [29, 30, 31, 32]}
});

# Clear all steps for a test case
db.step_code_cache.deleteMany({test_case_id: '<TEST_CASE_ID>'});
```

---

## Common Intent Patterns

### Click a button by data-testid
```json
{"action": "click", "target": {"test_id": "submit-btn"}}
```

### Click a button by visible text
```json
{"action": "click", "target": {"text": "Save changes"}}
```

### Click a button by ARIA role + name
```json
{"action": "click", "target": {"role": "button", "name": "Delete"}}
```

### Type into a labelled input
```json
{"action": "input", "target": {"label": "Project Name"}, "value": "My Project"}
```

### Select a dropdown option
```json
{"action": "select", "target": {"label": "Priority"}, "value": "high"}
```

### Navigate to a URL
```json
{"action": "navigate", "value": "http://qualora-frontend:3000/projects"}
```

### Wait for text then click (two-action step)
```json
[
  {"action": "wait_for_text", "value": "No test cases yet"},
  {"action": "click", "target": {"test_id": "add-test-case-btn"}}
]
```

### Wait for navigation after a click
```json
[
  {"action": "click", "target": {"test_id": "confirm-dialog-confirm"}},
  {"action": "wait_for_navigation", "value": "**/projects"}
]
```

### Assert visible (immediately passes DOM verification)
```json
{"action": "assert_visible", "target": {"test_id": "success-banner"}}
```

---

## How Verification Works

After execution, the step result is verified in layers:

1. **DOM assertions** — if the intent contains `assert_text` or `assert_visible`, and they didn't throw, the step immediately passes. No vision needed.

2. **DOM state check** — for navigation/wait actions, checks the URL changed. For click/input, trusts that execution succeeded (no exception = success).

3. **Vision AI** — if `expected` is non-empty and vision is enabled, sends a screenshot to the vision model and compares against expected.

4. **Behavior override** — if vision returns "failed" but the DOM had mutations, extracts significant words from `expected` and checks if ≥75% appear in the vision AI's description of the page. If yes, overrides to "passed".

### Controlling verification with `expected`

| `expected` value | Effect |
|---|---|
| `""` (empty) | Vision and behavior override are skipped. Step passes on DOM state only. |
| Words that match page content | Behavior override fires at ≥75% match, bypassing slow vision AI. |
| Quoted strings like `'Submit'` | Keyword text check: looks for those exact strings in `document.body.innerText`. |

### Avoiding common verification failures

**`dialog` vs `modal`** — "modal" is a stop word (filtered from actual). If your expected says "dialog" but the page uses a modal component, the word won't match. Use "confirmation" instead.

**Stop words** (filtered from word matching): `the`, `and`, `are`, `with`, `from`, `that`, `this`, `for`, `has`, `have`, `was`, `were`, `will`, `been`, `page`, `displayed`, `visible`, `shows`, `should`, `modal`, `step`

**Async UI updates** — if a deletion or fetch is async, the verification screenshot may fire before the UI re-renders. Options:
- Add `wait_for_text` action to the intent to wait for the new UI state
- Add `wait_for_navigation` if the action causes a route change
- Set `expected` to `""` for fire-and-forget cleanup steps

---

## Finding the Test Case ID

```bash
docker exec qualora-mongo mongosh \
  "mongodb://qualora:qualora_dev_password@localhost:27017/qualora?authSource=admin" \
  --eval "
db.test_cases.find({}, {_id: 1, title: 1, project_id: 1}).forEach(t =>
  print(t._id, '|', t.title)
);
"
```

---

## Full Example: Fixing a Broken Confirmation Step

A confirm button inside a modal was being missed because the AI generated a text-based selector instead of using the `data-testid`.

**Check what intent was cached:**
```bash
db.step_code_cache.findOne({test_case_id: '69c351c2503b6f07a6aa4454', step_number: 30})
# → actions: [{"action":"click","target":{"role":"button","name":"Delete"}}]
# Wrong — ambiguous, could hit Cancel
```

**Override with the precise testid:**
```javascript
db.step_code_cache.updateOne(
  {test_case_id: '69c351c2503b6f07a6aa4454', step_number: 30},
  {$set: {
    last_successful_code: JSON.stringify({
      step_number: 30,
      actions: [{action: 'click', target: {test_id: 'confirm-dialog-confirm'}}],
      description: 'Click confirm dialog confirm button'
    }),
    stability_score: 1.0
  }}
);
```

**Set expected to empty to skip timing-sensitive verification:**
```javascript
db.test_cases.updateOne(
  {_id: ObjectId('69c351c2503b6f07a6aa4454'), 'steps.step_number': 30},
  {$set: {'steps.$.expected': ''}}
);
```
