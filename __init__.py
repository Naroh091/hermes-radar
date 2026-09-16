"""Directory-plugin shim; loads the implementation package without core imports."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

_impl_name = "_hermes_proactive_impl"
_impl_path = Path(__file__).parent / "proactive" / "__init__.py"
_spec = spec_from_file_location(_impl_name, _impl_path, submodule_search_locations=[str(_impl_path.parent)])
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load proactive implementation")
_impl = module_from_spec(_spec)
sys.modules[_impl_name] = _impl
_spec.loader.exec_module(_impl)
register = _impl.register
__all__ = ["register"]
