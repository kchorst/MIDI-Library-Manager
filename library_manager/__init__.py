"""MIDI Library Manager package.

The package root intentionally does not import PySide6.  Release tooling and
version/API compatibility checks must be usable on headless systems without
instantiating the Manager UI.
"""

__all__ = ["MidiLibraryManagerWindow", "main"]


def __getattr__(name: str):
    if name in __all__:
        from .window import MidiLibraryManagerWindow, main
        return MidiLibraryManagerWindow if name == "MidiLibraryManagerWindow" else main
    raise AttributeError(name)
