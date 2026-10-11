"""Finds the installed vendor plugins.

Core code gets plugins only through list_plugins(). A plugin directory is
any subdirectory of plugins/ containing a plugin.py that defines a
VendorPlugin subclass. Directory names aren't required to be valid Python
identifiers (e.g. "amazon.com"), so each plugin.py is loaded by file path
rather than import statement.
"""

import functools
import importlib.util
import inspect
from pathlib import Path

from plugins.plugin import VendorPlugin

PLUGINS_DIR = Path(__file__).resolve().parent


def _load_plugin_class(plugin_dir: Path) -> type[VendorPlugin] | None:
    spec = importlib.util.spec_from_file_location(f"plugin_{plugin_dir.name}", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, VendorPlugin) and obj is not VendorPlugin:
            return obj
    return None


@functools.cache
def list_plugins() -> list[VendorPlugin]:
    """Every installed plugin, one instance each. Plugins are found and
    loaded on the first call; later calls return the same instances."""
    plugins = []
    for entry in sorted(PLUGINS_DIR.iterdir()):
        if entry.is_dir() and (entry / "plugin.py").exists():
            plugin_cls = _load_plugin_class(entry)
            if plugin_cls is not None:
                plugins.append(plugin_cls())
    return plugins

