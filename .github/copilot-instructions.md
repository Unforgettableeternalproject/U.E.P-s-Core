# GitHub Copilot Instructions — U.E.P Project

You are assisting on **U.E.P (Unified Experience Partner)**, a modular Python project.  
Main priorities: correctness, modularity, stability, and respect for existing naming conventions.

---

## Core Rules

- **Do not** arbitrarily rename classes, functions, or variables.  
  - Especially avoid suffixes like `V2`, `V3`, etc. unless explicitly instructed.  
- **Always** gather enough context before analyzing or modifying code.  
  - Read surrounding files/functions to understand how parts interact.  
- **Every time you suggest terminal commands**, prepend instructions to activate the Python virtual environment first (since each session starts fresh). Example:  
  ```bash
  source venv/bin/activate   # Linux/macOS
  .\venv\Scripts\activate    # Windows PowerShell
```

* Keep code changes **minimal and targeted**. Suggest only the section that needs modification, not the entire file.
* Use **Python 3.11+**, type hints, and docstrings (Google style).
* No hardcoding secrets, absolute paths, or adding stray `print()` calls.
* Use our logging and debug system (`debug_level`, `log_level`) instead of raw prints.

---

## State & Flow

* Respect the central **controller/state manager**.
* Always end up in a valid state (e.g., IDLE).
* Avoid rapid flicker in transitions; debounce or queue state changes where needed.
* When adding new behavior, make sure it wires cleanly through the controller/registry without breaking existing flows.

---

## Development Practices

* Add tests when creating new functions.
* Use parameterized configs (`configs/*.yaml`) for paths and API keys.
* Document changes with clear commit messages (imperative style).
* Internal code/comments in English; user-facing strings in zh-TW.

---

## Golden Rule

Produce the **smallest viable change** that solves the problem,
with correct context, correct naming, and proper environment setup.
Use Traditional Chinese (zh-TW) for user-facing text.