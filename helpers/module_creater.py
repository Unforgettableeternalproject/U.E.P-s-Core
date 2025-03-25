# create_module.py

import os
import sys
from pathlib import Path

TEMPLATE_FILES = {
    "__init__.py": "from .{name}_module import {class_name}\n\ndef register():\n    return {class_name}(config={{}})\n",
    "{name}_module.py": "class {class_name}:\n    def __init__(self, config: dict):\n        self.config = config\n\n    def handle(self, data: dict) -> dict:\n        return {{}}\n",
    "config.yaml": "# Default config for {name}_module\n",
    "schemas.py": "from pydantic import BaseModel\n\nclass Input(BaseModel):\n    pass\n\nclass Output(BaseModel):\n    pass\n",
    "example_input.json": "{{\n  \"example\": \"data\"\n}}",
    "example_output.json": "{{\n  \"response\": \"output\"\n}}",
    "tests/test_{name}_module.py": "from modules.{name}_module.{name}_module import {class_name}\n\ndef test_handle():\n    mod = {class_name}(config={{}})\n    result = mod.handle({{}})\n    assert isinstance(result, dict)\n"
}

def create_module(name):
    module_path = Path(f"../modules/{name}")
    if module_path.exists():
        print(f"[!] Module '{name}' already exists.")
        return
    
    print(f"[+] Creating module: {name}")
    (module_path / "tests").mkdir(parents=True, exist_ok=True)

    class_name = ''.join([w.capitalize() for w in name.split('_')])  # nlp_module -> NlpModule

    for filename, content in TEMPLATE_FILES.items():
        path = module_path / filename.format(name=name)
        with open(path, "w", encoding="utf-8") as f:
            if filename.endswith(".json"):
                f.write(content)
            else:
                f.write(content.format(name=name, class_name=class_name))
        print(f"  ├─ Created {path}")

    print(f"[+] Module '{name}' created successfully. :)")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python create_module.py <module_name>")
    else:
        create_module(sys.argv[1])
