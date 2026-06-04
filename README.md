# AutoAPIs v1.1.0

A lightweight DSL compiler that turns `.api` files into clean, ready-to-use Python source files. Write structured declarations, run the compiler, get Python - no boilerplate, no fuss.

---

## Installation

No dependencies beyond the Python standard library. Just drop `autoapis.py` anywhere and run it.

**Requirements:** Python 3.10+

---

## Usage

```
python autoapis.py [--cache yes|no] <file.api>
```

This produces a `<file>.py` in the same directory as your `.api` file.

**Examples:**

```
python autoapis.py MyAPI.api
python autoapis.py --cache yes MyAPI.api
```

---

## Caching

When `--cache yes` is passed, AutoAPIs stores the compiled output and a SHA-256 hash of your `.api` file inside an `autoapi_cache/` folder next to the compiler. On subsequent runs, if the `.api` file has not changed, the cached output is restored instantly without recompiling.

```
python autoapis.py --cache yes MyAPI.api
# -> Compiled 'MyAPI.api' -> 'MyAPI.py'

python autoapis.py --cache yes MyAPI.api
# -> Cache hit - 'MyAPI.api' unchanged, restored 'MyAPI.py'
```

Cache files live at `autoapi_cache/<name>.py` and `autoapi_cache/<name>.meta.json`. You can safely delete the folder to force a full recompile.

---

## Syntax Overview

Every `.api` file is made up of **items**. Each item opens with `item <type>` at the top level, or `ClassName <type>` when nested inside a class. Every item ends with `close`.

Lines starting with `#` are comments and are ignored entirely.

```
# This is a comment

item <type>
...
close
```

### Nesting

Items can be nested inside classes using either style:

```
item class
name Outer
  item function         # style 1: item keyword
  ...
  close

  Outer function        # style 2: ClassName keyword
  ...
  close
close
```

Both styles work identically. You can nest classes inside classes, functions inside classes, enums inside classes, and so on, to any depth.

---

## Item Types

### `item class`

Generates a Python class.

| Keyword       | Required | Description                              |
|---------------|----------|------------------------------------------|
| `name`        | Yes      | Class name                               |
| `inherit`     | No       | Comma-separated base classes             |
| `description` | No       | Docstring                                |

```
item class
name MyError
inherit Exception
description Raised when something goes wrong.
close
```

Output:
```python
class MyError(Exception):
    """Raised when something goes wrong."""
    pass
```

Multiple bases:
```
item class
name MyClass
inherit BaseA, BaseB
close
```

Output:
```python
class MyClass(BaseA, BaseB):
    pass
```

---

### `item function`

Generates a Python function or method.

| Keyword       | Required | Description                                           |
|---------------|----------|-------------------------------------------------------|
| `funcname`    | Yes      | Function name                                         |
| `arguments`   | No       | Full argument signature including parentheses         |
| `type`        | No       | Decorator (see decorator table below)                 |
| `description` | No       | Docstring                                             |

All header keywords (`funcname`, `arguments`, `type`, `description`) can appear in any order. Everything after the headers and before `close` is treated as raw Python body lines.

```
item function
funcname greet
description Says hello.
arguments (name: str)
print(f"Hello, {name}!")
close
```

Output:
```python
def greet(name: str):
    """Says hello."""
    print(f"Hello, {name}!")
```

#### Decorators (`type`)

| Value              | Generated decorator   | Auto-import added               |
|--------------------|-----------------------|---------------------------------|
| `staticmethod`     | `@staticmethod`       | -                               |
| `classmethod`      | `@classmethod`        | -                               |
| `abstractmethod`   | `@abstractmethod`     | `from abc import abstractmethod`|
| `property`         | `@property`           | -                               |
| `property.setter`  | `@property.setter`    | -                               |
| `property.deleter` | `@property.deleter`   | -                               |
| `override`         | `@override`           | `from typing import override`   |

Required imports are automatically added to the top of the generated file.

```
item class
name Shape
  Shape function
  funcname area
  type abstractmethod
  arguments (self)
  close

  Shape function
  funcname name
  type property
  arguments (self)
  return self._name
  close

  Shape function
  funcname name
  type property.setter
  arguments (self, value: str)
  self._name = value
  close
close
```

Output:
```python
from abc import abstractmethod

class Shape:
    @abstractmethod
    def area(self):
        pass

    @property
    def name(self):
        return self._name

    @property.setter
    def name(self, value: str):
        self._name = value
```

#### Argument auto-fix

If you accidentally write a `*args` or `**kwargs` parameter with a type-annotation style (`name: *args`), AutoAPIs automatically corrects it and prints a warning:

```
arguments (x: int, rest: *args, opts: **kwargs)
```

```
  Warning  Auto-converted argument: 'rest: *args'  ->  '*args'  (in function 'myFunc')
  Warning  Auto-converted argument: 'opts: **kwargs'  ->  '**kwargs'  (in function 'myFunc')
```

This conversion is skipped for anything inside string literals.

#### Body indentation

Raw Python body lines preserve their relative indentation. Indent your body however you like - the compiler tracks the base indent of the first body line and keeps everything relative to it.

```
item function
funcname process
arguments (items: list)
for item in items:
  if item.active:
    print(item)
close
```

Output:
```python
def process(items: list):
    for item in items:
      if item.active:
        print(item)
```

---

### `item variable`

Generates a module-level or class-level variable assignment. Values can span multiple lines for dicts, lists, and other multi-line literals.

| Keyword | Required | Description        |
|---------|----------|--------------------|
| `name`  | Yes      | Variable name      |
| `value` | Yes      | Value (first line) |

Single-line:
```
item variable
name API_URL
value "https://api.example.com/v1"
close
```

Output:
```python
API_URL = "https://api.example.com/v1"
```

Multi-line:
```
item variable
name HEADERS
value {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
close
```

Output:
```python
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
```

---

### `item enum`

Generates a Python enum class. Automatically adds `from enum import Enum` to the output.

| Keyword       | Required | Description                              |
|---------------|----------|------------------------------------------|
| `name`        | Yes      | Enum class name                          |
| `inherit`     | No       | Base class (default: `Enum`)             |
| `description` | No       | Docstring                                |
| `field`       | No       | `field NAME = value` (repeat per member) |

```
item enum
name Color
description RGB color options.
field RED = 1
field GREEN = 2
field BLUE = 3
close
```

Output:
```python
from enum import Enum

class Color(Enum):
    """RGB color options."""
    RED = 1
    GREEN = 2
    BLUE = 3
```

Use `inherit` to change the base:
```
item enum
name Status
inherit IntEnum
field PENDING = 0
field ACTIVE = 1
field CLOSED = 2
close
```

Output:
```python
class Status(IntEnum):
    PENDING = 0
    ACTIVE = 1
    CLOSED = 2
```

---

### `item dataclass`

Generates a `@dataclass` decorated class. Automatically adds `from dataclasses import dataclass, field` to the output.

| Keyword       | Required | Description                                       |
|---------------|----------|---------------------------------------------------|
| `name`        | Yes      | Class name                                        |
| `inherit`     | No       | Comma-separated base classes                      |
| `description` | No       | Docstring                                         |
| `frozen`      | No       | `yes` or `no` (default: `no`)                     |
| `eq`          | No       | `yes` or `no` (default: `yes`)                    |
| `order`       | No       | `yes` or `no` (default: `no`)                     |
| `field`       | No       | `field NAME: TYPE` or `field NAME: TYPE = default`|
| `post_init`   | No       | Opens a `__post_init__` body block                |
| `end_post_init` | -      | Closes the `post_init` block                      |

```
item dataclass
name Point
description A 2D point.
field x: float
field y: float = 0.0
close
```

Output:
```python
from dataclasses import dataclass, field

@dataclass
class Point:
    """A 2D point."""
    x: float
    y: float = 0.0
```

With options and `post_init`:
```
item dataclass
name Config
frozen yes
order yes
description Immutable sorted config.
field host: str
field port: int = 8080
field tags: list = field(default_factory=list)
post_init
  if self.port < 1 or self.port > 65535:
    raise ValueError(f"Invalid port: {self.port}")
end_post_init
close
```

Output:
```python
@dataclass(frozen=True, order=True)
class Config:
    """Immutable sorted config."""
    host: str
    port: int = 8080
    tags: list = field(default_factory=list)

    def __post_init__(self):
        if self.port < 1 or self.port > 65535:
          raise ValueError(f"Invalid port: {self.port}")
```

---

## Full Example

```
# myapi.api

item variable
name BASE_URL
value "https://api.example.com/v1"
close

item enum
name HttpMethod
field GET = "GET"
field POST = "POST"
field DELETE = "DELETE"
close

item dataclass
name RequestConfig
frozen yes
field url: str
field method: str = "GET"
field timeout: int = 30
close

item class
name APIError
inherit Exception
description Base class for API errors.
  APIError function
  funcname __init__
  arguments (self, status: int, message: str)
  super().__init__(message)
  self.status = status
  close
close

item class
name Client
description Simple HTTP API client.
  Client function
  funcname __init__
  arguments (self, base_url: str = BASE_URL)
  self.base_url = base_url
  close

  Client function
  funcname get
  arguments (self, path: str)
  description Perform a GET request.
  import requests
  response = requests.get(f"{self.base_url}{path}")
  if not response.ok:
    raise APIError(response.status_code, response.text)
  return response.json()
  close

  Client function
  funcname from_env
  type classmethod
  arguments (cls)
  description Create a client from the BASE_URL environment variable.
  import os
  return cls(os.environ.get("BASE_URL", BASE_URL))
  close
close
```

Run:
```
python autoapis.py --cache yes myapi.api
```

---

## Error Reporting

All parse errors include the offending line number and a descriptive message:

```
Parse error: Line 7: Unknown item type 'banana'. Expected: class, dataclass, enum, function, variable
Parse error: Line 3: Unclosed 'class MyClass': missing 'close'
Parse error: Line 12: Function is missing a 'funcname'
Parse error: Line 5: Invalid type 'async'. Valid types: abstractmethod, classmethod, override, property, property.deleter, property.setter, staticmethod
```

---

## Quick Reference

```
item class
name Name
inherit Base1, Base2       # optional
description Text           # optional -> docstring
  ...nested items...
close

item function
funcname name
type staticmethod          # optional decorator
arguments (self, x: int)   # optional, default ()
description Text           # optional -> docstring
  ...raw python body...
close

item variable
name NAME
value <first line>
<continuation lines>
close

item enum
name Name
inherit IntEnum            # optional, default Enum
description Text           # optional
field KEY = value
close

item dataclass
name Name
inherit Base               # optional
frozen yes|no              # optional, default no
eq yes|no                  # optional, default yes
order yes|no               # optional, default no
description Text           # optional
field name: type
field name: type = default
post_init
  ...raw python body...
end_post_init
close
```