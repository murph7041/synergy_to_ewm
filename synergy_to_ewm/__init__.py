"""
synergy_to_ewm — migrate IBM Rational Synergy to IBM Engineering Workflow Management.
"""
from .config import EWMConfig, MigrationConfig, SynergyConfig

# Migrator is imported lazily to avoid the RuntimeWarning that occurs when
# running `python -m synergy_to_ewm.migrate`.  Python loads the package
# __init__.py before executing the migrate module as __main__; if __init__.py
# eagerly imports migrate, the module ends up in sys.modules twice under
# different names, which triggers the warning and can cause unpredictable
# behaviour with isinstance checks and module-level state.
def __getattr__(name: str):
    if name == "Migrator":
        from .migrate import Migrator
        return Migrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["Migrator", "MigrationConfig", "SynergyConfig", "EWMConfig"]
