Octop local runtime portable payload
===================================
This archive contains CPython and the desktop Agent sidecar dependencies,
including wheels built from this product's local Harness/Memory source.
Use Octop Desktop for chat, cloud login and OS credential storage.

Diagnostic launch
-----------------
macOS/Linux: ./start.sh
Windows:     start.bat

Default: authenticated loopback API on a random port. Supply a strong
OCTOP_LOCAL_API_TOKEN for manual API requests. No cloud/setup wizard starts.
Data root: ./data (OCTOP_HOME), with local/config.json and account namespaces.
--home selects the data root. --host/--port are process-only overrides;
local binding must remain loopback. No setting is silently persisted.

Layout
------
runtime/      portable CPython
packages/     installed product and local library wheels (no editable paths)
launch.py     .pth processing and Windows pywin32 DLL bootstrap
start.sh / start.bat, VERSION.txt, README.txt

Always use launch.py/start.sh/start.bat rather than PYTHONPATH=packages alone.
Quit/suspend interrupts local work. Unacknowledged cloud data remains pending;
wake/restart never resumes tools or pending approvals automatically.
