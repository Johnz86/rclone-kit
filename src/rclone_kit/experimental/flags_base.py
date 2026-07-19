from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from typing import cast


def merge_flags[T](cls: type[T], dataclass_a: T, dataclass_b: T) -> T:
    if not is_dataclass(dataclass_a) or not is_dataclass(dataclass_b):
        raise ValueError("Both inputs must be dataclass instances")
    if type(dataclass_a) is not type(dataclass_b):
        raise ValueError("Dataclass instances must be of the same type")

    merged_kwargs = {}
    for field in fields(dataclass_a):
        a_value = getattr(dataclass_a, field.name)
        b_value = getattr(dataclass_b, field.name)

        if is_dataclass(a_value) and is_dataclass(b_value):
            merged_kwargs[field.name] = merge_flags(type(a_value), a_value, b_value)
        else:
            merged_kwargs[field.name] = b_value if b_value is not None else a_value

    return cls(**merged_kwargs)


def _field_name_to_flag(field_name: str) -> str:
    return f"--{field_name.replace('_', '-')}"


@dataclass
class BaseFlags:
    """provides to_args(), merge() and __repr__ methods for flags dataclasses"""

    def to_args(self) -> list[str]:
        args = []
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue

            to_args = getattr(value, "to_args", None)
            if is_dataclass(value) and callable(to_args):
                args.extend(cast(Callable[[], list[str]], to_args)())
            elif isinstance(value, bool):
                if value:
                    args.append(_field_name_to_flag(field.name))
            else:
                args.append(_field_name_to_flag(field.name))
                if isinstance(value, list):
                    args.append(",".join(map(str, value)))
                else:
                    args.append(str(value))
        return args

    def __repr__(self):
        return str(self.to_args())
