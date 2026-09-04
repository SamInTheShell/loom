# The JsApi bridge (Python ↔ JavaScript)

Methods on the object you pass as `js_api=` to `webview.create_window()`
are exposed to JavaScript as `window.pywebview.api.<method>()`. This file
is the contract; get any line of it wrong and the symptom is usually a
silent failure or a cryptic JS error.

## The rules

- **Explicit exposure.** Only methods defined on the `JsApi` class are
  callable. pywebview does not auto-expose anything. If the HTML calls
  `window.pywebview.api.foo()`, the class needs `def foo(self, ...)`.
  Missing methods surface in JS as "is not a function".
- **All calls are async in JS.** Every bridge call returns a Promise.
  Always `await`:

  ```js
  const result = await window.pywebview.api.some_method(arg1, arg2);
  ```

  Without `await` you get a Promise object (or `null` on error) instead
  of the result.
- **Return values become JSON.** Python return values are serialized to
  the JS side. Return `True`/`False` for success flags, dicts for
  structured data. Keep return shapes consistent - a good convention is
  `{ok: bool, error: str|None, ...payload}` so the frontend handles every
  call the same way.
- **Store the window reference** via a setter so API methods can use it:

  ```python
  class JsApi:
      def __init__(self):
          self._window = None

      def set_window(self, window):
          self._window = window

      def some_method(self, arg):
          if self._window is None:
              return False
          # do something with self._window
          return True
  ```

  Call `api.set_window(window)` (or `api.bind(window)`) right after
  `create_window` returns.

## Verify bridge methods from Python before wiring up buttons

The cheapest test is importing the class and checking the surface:

```bash
uv run python -c "
from src.your_app.app import JsApi
api = JsApi()
print('Methods:', [m for m in dir(api) if not m.startswith('_')])
assert hasattr(api, 'open_devtools'), 'Missing open_devtools method'
"
```

Then, with the app running and DevTools open, every `JsApi` method is
callable from the JS console as `window.pywebview.api.<method>(...)` - so
you can drive the Python side live without restarting (see debugging.md).

## Common failures

| Symptom in JS | Cause |
| --- | --- |
| `... is not a function` | Method not defined on the `JsApi` class. |
| Result is `null` or a `Promise` object | Call not `await`ed. |
| Method exists but does nothing | `set_window` never called, method returns early. |
| Works in Python test, breaks in JS | Return value not JSON-serializable (e.g. `Path`, sets). |

## Frontend-side wrapper (optional but recommended)

Wrap the bridge in one tiny helper so the whole UI degrades gracefully
when opened in a plain browser (where `window.pywebview` is undefined):
a `has()` check plus a `call()` that short-circuits to a synthetic error
lets pure-UI work proceed without the backend. See debugging.md.
