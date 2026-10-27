"""Langflow launcher with Python 3.14 dataclass compatibility patch."""

from __future__ import annotations

import sys


def _apply_python314_dataclass_patch() -> None:
    """Fix CPython 3.14 dataclasses._add_slots AttributeError when init=False and slots=True."""
    if sys.version_info < (3, 14):
        return

    import dataclasses
    import inspect
    import itertools
    import types

    g = dataclasses.__dict__

    def _patched_add_slots(cls, is_frozen, weakref_slot, defined_fields):
        if "__slots__" in cls.__dict__:
            raise TypeError(f"{cls.__name__} already specifies __slots__")
        sys._clear_type_descriptors(cls)
        cls_dict = dict(cls.__dict__)
        field_names = tuple(f.name for f in dataclasses.fields(cls))
        inherited_slots = set(itertools.chain.from_iterable(map(g["_get_slots"], cls.__mro__[1:-1])))
        cls_dict["__slots__"] = g["_create_slots"](defined_fields, inherited_slots, field_names, weakref_slot)
        for field_name in field_names:
            cls_dict.pop(field_name, None)
        qualname = getattr(cls, "__qualname__", None)
        newcls = type(cls)(cls.__name__, cls.__bases__, cls_dict)
        if qualname is not None:
            newcls.__qualname__ = qualname
        if is_frozen:
            if "__getstate__" not in cls_dict:
                newcls.__getstate__ = g["_dataclass_getstate"]
            if "__setstate__" not in cls_dict:
                newcls.__setstate__ = g["_dataclass_setstate"]
        for member in newcls.__dict__.values():
            member = inspect.unwrap(member)
            if isinstance(member, types.FunctionType):
                if g["_update_func_cell_for__class__"](member, cls, newcls):
                    break
            elif isinstance(member, property):
                if (
                    g["_update_func_cell_for__class__"](member.fget, cls, newcls)
                    or g["_update_func_cell_for__class__"](member.fset, cls, newcls)
                    or g["_update_func_cell_for__class__"](member.fdel, cls, newcls)
                ):
                    break
        try:
            import annotationlib

            newcls_ann = annotationlib.get_annotations(newcls, format=annotationlib.Format.FORWARDREF)
            for f in getattr(newcls, g["_FIELDS"]).values():
                try:
                    ann = newcls_ann[f.name]
                except KeyError:
                    pass
                else:
                    f.type = ann
        except Exception:
            pass

        init_annotate = getattr(newcls.__init__, "__annotate__", None)
        if getattr(init_annotate, "__generated_by_dataclasses__", False):
            g["_update_func_cell_for__class__"](init_annotate, cls, newcls)
        return newcls

    dataclasses._add_slots = _patched_add_slots


def main() -> None:
    _apply_python314_dataclass_patch()
    from langflow.__main__ import app

    if len(sys.argv) == 1:
        sys.argv = ["langflow", "run", "--port", "7860"]
    else:
        sys.argv = ["langflow", *sys.argv[1:]]

    app()


if __name__ == "__main__":
    main()
