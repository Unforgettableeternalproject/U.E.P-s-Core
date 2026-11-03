"""工具註冊表。

- ``StaticToolRegistry``：靜態記憶體 ``ToolSpecResolver``（契約 TG-1、D54），只有分類面；既有使用者（組裝層、測試）沿用。
- ``ToolRegistry``：``ToolEntry`` 單一來源（Agent 架構提案 §5，切片 A）。建構即驗證 RA-1～16 並凍結，同樣滿足
  ``EnumerableToolSpecResolver``，LLM 面宣告（tools JSON）與白名單分類都由它衍生。

切片 A 只「宣告並驗證」；切片 B 啟用投影（``project``，§6.1）與 readiness 事件（§5.8）。排程（concurrency）留給切片 D。
TaskManager 只依賴 ``uep.adapters.tool`` 的協定，不 import 本模組。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, get_args

from uep.adapters.tool import (
    APPROVAL_ORDER,
    CAPABILITY_CEILING,
    SIDE_EFFECT_ORDER,
    VISIBLE_ORIGINS,
    ApprovalLevel,
    Concurrency,
    Idempotency,
    InterruptKind,
    LoadCost,
    Origin,
    PresenceMode,
    SideEffectClass,
    Targeting,
    ToolDescriptor,
    ToolEntry,
    ToolSpec,
    ToolVariant,
)
from uep.events.envelope import scope_rank
from uep.tasks.manager import PolicyViolation, params_hash
from uep.tools.projection import ProjectionContext, audience_rank
from uep.tools.schema import freeze as _freeze
from uep.tools.schema import lint_subset, strict_schema
from uep.tools.schema import thaw as _thaw

logger = logging.getLogger("uep.tools.registry")

TOOL_UNKNOWN = "tool_unknown"


def unknown_tool(tool: str) -> PolicyViolation:
    """解析不到的統一例外（resolver 實作共用；訊息不含參數內容）。"""
    return PolicyViolation(f"未註冊的工具：{tool}", code=TOOL_UNKNOWN)


class StaticToolRegistry:
    def __init__(self, specs: Mapping[str, ToolSpec]) -> None:
        for name, spec in specs.items():
            if not isinstance(name, str) or not name:
                raise ValueError("工具名必須是非空字串")
            if not isinstance(spec, ToolSpec):
                raise TypeError(f"{name}: 必須是 ToolSpec")
            if isinstance(spec.tool_version, bool) or not isinstance(spec.tool_version, int) or spec.tool_version < 1:
                raise ValueError(f"{name}: tool_version 必須是 >= 1 的整數")
        self._specs: Mapping[str, ToolSpec] = MappingProxyType(dict(specs))

    def resolve(self, tool: str) -> ToolSpec:
        spec = self._specs.get(tool)
        if spec is None:
            raise unknown_tool(tool)
        return spec

    def specs(self) -> Mapping[str, ToolSpec]:
        return self._specs


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

LOCKFILE_PATH = Path(__file__).with_name("tools.lock.json")
_NAME_RE = re.compile(r"[a-z][a-z0-9_.]{2,63}")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})

Readiness = Literal["ready", "warming", "unavailable"]
MODULE_EVENT_STATES: Mapping[str, Readiness] = MappingProxyType({
    "module.ready": "ready", "module.warming": "warming", "module.unavailable": "unavailable",
})


class ToolRegistryError(ValueError):
    """註冊表斷言失敗（建構即失敗，fail-closed）。``rule`` 是 RA 編號；訊息不含參數內容。"""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule


_SCOPE_LEVELS = frozenset({"private", "group", "public"})


def _valid_scope_ref(value: Any) -> bool:
    """ScopeRef：層級簡寫（private／group／public，提案與目錄用法）或 ``scope_<level>_<id>`` 序列化字串。"""
    if not isinstance(value, str):
        return False
    if value in _SCOPE_LEVELS:
        return True
    try:
        scope_rank(value)
    except ValueError:
        return False
    return True


def _literal_values(literal: Any) -> frozenset[str]:
    return frozenset(get_args(literal))


def _lint_schema(schema: Any, where: str, rule: str) -> None:
    """schema linter（RA-7、RA-16）：物件型、每個屬性有明確 type、只用 JSON Schema 子集（``$ref``／``oneOf``／
    ``anyOf``／``allOf`` 等註冊時就失敗，見 ``uep.tools.schema``）、``required`` 皆為已宣告屬性。
    ``additionalProperties`` 只能寫 false；沒寫的由 ``strict_schema`` 在有效 schema 補上（舊路徑位元組不變）。"""
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise ToolRegistryError(rule, f"{where}: schema 必須是 type=object 的映射")
    props = schema.get("properties", {})
    if not isinstance(props, Mapping):
        raise ToolRegistryError(rule, f"{where}: properties 必須是映射")

    problem = lint_subset(schema)
    if problem:
        raise ToolRegistryError(rule, f"{where}: {problem}")
    for prop, definition in props.items():
        if not isinstance(definition, Mapping) or not isinstance(definition.get("type"), str):
            raise ToolRegistryError(rule, f"{where}: 屬性 {prop!r} 缺少明確 type")
    required = schema.get("required", [])
    if not isinstance(required, (list, tuple)) or any(r not in props for r in required):
        raise ToolRegistryError(rule, f"{where}: required 含未宣告的屬性")


def compute_schema_hash(entry: ToolEntry, input_schema: Mapping[str, Any] | None = None) -> str:
    """§5.5：canonical JSON（沿用 params_hash 的唯一 canonical 函式，TG-6）對
    ``{name, input_schema, side_effect_class, idempotency, approval_level, targeting}`` 的 sha256。
    description 與 tool_version 不入 hash。"""
    spec = entry.spec
    schema = entry.descriptor.input_schema if input_schema is None else input_schema
    payload = {
        "name": entry.name,
        "input_schema": json.loads(json.dumps(_thaw(schema))),
        "side_effect_class": spec.side_effect_class,
        "idempotency": spec.idempotency,
        "approval_level": spec.approval_level,
        "targeting": spec.targeting,
    }
    return params_hash("schema_hash", 0, payload)


def load_lockfile(path: Path = LOCKFILE_PATH) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


class ToolRegistry:
    """建構即驗證並凍結的工具註冊表；實作 ``EnumerableToolSpecResolver``。"""

    def __init__(self) -> None:  # 一律走 ``build``
        self._entries: Mapping[str, ToolEntry] = MappingProxyType({})
        self._specs: Mapping[str, ToolSpec] = MappingProxyType({})
        self._hashes: Mapping[str, str] = MappingProxyType({})
        self._known_modules: frozenset[str] = frozenset()
        # module coordinator 事件維護的就緒狀態（§5.8）：module_id -> (revision, state)。整份替換（copy-on-write），
        # 讀取端不需要鎖；寫入端以鎖序列化。revision 是投影版本（capability_set.revision），每次有效事件加一。
        self._module_state: Mapping[str, tuple[int, Readiness]] = MappingProxyType({})
        self._projection_revision = 0
        self._event_lock = threading.Lock()

    @classmethod
    def build(
        cls,
        entries: Iterable[ToolEntry],
        *,
        known_modules: Iterable[str] = (),
        lockfile: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ToolRegistry:
        """驗證 RA-1～16。``lockfile`` 給了就做 RA-8 單調檢查（沒給略過，CI 測試負責整份比對）。

        production 路徑預設不比對 lockfile（記憶體內建表，無執行期 I/O）；熱載入時拒絕 hash 變而版本未升的
        條目，留待後續切片（§5.5 執行期防線，隨 mcp_static／platform_approved 載入實作）。
        基準比對（同一個 PR 內改 schema 又重產 lockfile）由 CI 測試以 base 分支的 lockfile 傳入本參數。"""
        items = list(entries)
        by_name: dict[str, ToolEntry] = {}
        for entry in items:
            if not isinstance(entry, ToolEntry):
                raise ToolRegistryError("RA-9", "註冊項必須是 ToolEntry")
            if not isinstance(entry.name, str):
                raise ToolRegistryError("RA-9", f"name 必須是字串，收到 {type(entry.name).__name__}")
            if not _NAME_RE.fullmatch(entry.name):
                raise ToolRegistryError("RA-9", f"{entry.name!r}: 名稱不符 [a-z][a-z0-9_.]{{2,63}}")
            if entry.name in by_name:
                raise ToolRegistryError("RA-9", f"{entry.name}: 名稱重複")
            by_name[entry.name] = entry
        hashes: dict[str, str] = {}
        frozen: dict[str, ToolEntry] = {}
        for entry in items:
            cls._check_entry(entry)
            cls._check_composite(entry, by_name)
            try:
                digest = compute_schema_hash(entry)
                variant_hashes = {
                    f"{entry.name}@{platform}": compute_schema_hash(entry, variant.input_schema)
                    for platform, variant in entry.scope.variants.items() if variant.input_schema is not None
                }
            except PolicyViolation:
                raise ToolRegistryError("RA-8", f"{entry.name}: schema 超出 canonical 限制，無法計算 schema_hash") from None
            if entry.schema_hash and entry.schema_hash != digest:
                raise ToolRegistryError("RA-8", f"{entry.name}: 手填的 schema_hash 與重算結果不符")
            hashes[entry.name] = digest
            hashes.update(variant_hashes)
            descriptor = replace(entry.descriptor, input_schema=_freeze(entry.descriptor.input_schema))
            output = _freeze(entry.output_schema) if entry.output_schema is not None else None
            frozen[entry.name] = replace(entry, schema_hash=digest, descriptor=descriptor, output_schema=output)
        if lockfile is not None:
            cls._check_lockfile(frozen, hashes, lockfile)
        registry = cls()
        registry._entries = MappingProxyType(frozen)
        registry._specs = MappingProxyType({name: e.spec for name, e in frozen.items()})
        registry._hashes = MappingProxyType(hashes)
        registry._known_modules = frozenset(known_modules)
        for entry in frozen.values():
            missing = sorted(set(entry.requires) - registry._known_modules)
            if missing:
                logger.warning("工具 %s 的 requires 含未知模組 %s：readiness 恆為 unavailable（RA-15）", entry.name, missing)
        return registry

    # --- 驗證 ---

    @staticmethod
    def _check_entry(entry: ToolEntry) -> None:
        name, spec = entry.name, entry.spec
        if not isinstance(spec, ToolSpec) or not isinstance(entry.descriptor, ToolDescriptor):
            raise ToolRegistryError("RA-1", f"{name}: spec／descriptor 型別錯誤")
        if isinstance(spec.tool_version, bool) or not isinstance(spec.tool_version, int) or spec.tool_version < 1:
            raise ToolRegistryError("RA-1", f"{name}: tool_version 必須是 >= 1 的整數")
        # RA-1：執行期檢查 Literal 值域，不信任型別標註
        domains: dict[str, tuple[Any, Any]] = {
            "origin": (spec.origin, Origin), "side_effect_class": (spec.side_effect_class, SideEffectClass),
            "idempotency": (spec.idempotency, Idempotency), "approval_level": (spec.approval_level, ApprovalLevel),
            "interruptible": (spec.interruptible, InterruptKind), "targeting": (spec.targeting, Targeting),
            "concurrency": (entry.concurrency, Concurrency), "load_cost": (entry.load_cost, LoadCost),
            "scope.presence": (entry.scope.presence, PresenceMode),
        }
        for field_name, (value, literal) in domains.items():
            if value not in _literal_values(literal):
                raise ToolRegistryError("RA-1", f"{name}: {field_name}={value!r} 不在值域內")
        for field_name, value in (
            ("result_scope", entry.result_scope), ("scope.data_scope", entry.scope.data_scope),
            ("scope.audience_min", entry.scope.audience_min),
        ):
            if not _valid_scope_ref(value):
                raise ToolRegistryError("RA-1", f"{name}: {field_name}={value!r} 不是合法 ScopeRef")
        # RA-2
        if spec.side_effect_class in ("external_effect", "high_impact") and spec.approval_level == "none":
            raise ToolRegistryError("RA-2", f"{name}: {spec.side_effect_class} 需要 approval_level >= per_action")
        # RA-4（executor 名單雙向一致由組裝層 TG-5 檢查）
        if (spec.provider != "core") != (spec.targeting == "platform_remote"):
            raise ToolRegistryError("RA-4", f"{name}: provider!=core 與 targeting=platform_remote 必須互為充要")
        # RA-5
        if spec.interruptible != "non_interruptible" and not entry.cancellable:
            raise ToolRegistryError("RA-5", f"{name}: 可中斷工具必須 cancellable=True")
        # RA-6
        ceiling = CAPABILITY_CEILING.get(spec.capability)
        if ceiling is None:
            raise ToolRegistryError("RA-6", f"{name}: capability={spec.capability!r} 不在閉集內")
        if SIDE_EFFECT_ORDER.index(spec.side_effect_class) > SIDE_EFFECT_ORDER.index(ceiling):
            raise ToolRegistryError("RA-6", f"{name}: side_effect_class 超過 capability {spec.capability} 的上限 {ceiling}")
        # RA-7
        _lint_schema(entry.descriptor.input_schema, f"{name}.input_schema", "RA-7")
        props = entry.descriptor.input_schema.get("properties", {})
        leaked = sorted(set(entry.descriptor.injected_params) & set(props))
        if leaked:
            raise ToolRegistryError("RA-7", f"{name}: injected_params 不得出現在 LLM 面 schema：{leaked}")
        # RA-10
        if spec.origin == "mcp_static" and spec.executor.replace("-", "_").startswith("echo_mcp"):
            raise ToolRegistryError("RA-10", f"{name}: mcp_static 不得指向 echo-mcp 命名空間")
        # §5.6：scope.presence 與 spec.presence_bound 同源，不可矛盾
        if (entry.scope.presence == "bound") != bool(spec.presence_bound):
            raise ToolRegistryError("RA-1", f"{name}: scope.presence 與 spec.presence_bound 矛盾")
        ToolRegistry._check_concurrency(entry)  # RA-11
        ToolRegistry._check_conflict_keys(entry)  # RA-12
        ToolRegistry._check_variants(entry)  # RA-13
        # RA-15
        if entry.warm_required and not entry.requires:
            raise ToolRegistryError("RA-15", f"{name}: warm_required=True 需要非空 requires")
        # RA-16
        if spec.side_effect_class in ("internal_read", "reversible_local") and entry.output_schema is None:
            raise ToolRegistryError("RA-16", f"{name}: {spec.side_effect_class} 必須宣告 output_schema")
        if entry.output_schema is not None:
            _lint_schema(entry.output_schema, f"{name}.output_schema", "RA-16")

    @staticmethod
    def _check_concurrency(entry: ToolEntry) -> None:
        name, spec, mode = entry.name, entry.spec, entry.concurrency
        if mode == "serial":
            return
        if entry.timeout_s is None or entry.timeout_s <= 0:
            raise ToolRegistryError("RA-11", f"{name}: 非 serial 必須設定 timeout_s > 0")
        if spec.approval_level != "none":
            raise ToolRegistryError("RA-11", f"{name}: 需核准者恆為 serial")
        if mode == "parallel_safe":
            if spec.side_effect_class != "internal_read" or entry.output_schema is None:
                raise ToolRegistryError("RA-11", f"{name}: parallel_safe 需 internal_read 且有 output_schema")
            return
        if (
            spec.side_effect_class not in ("internal_read", "reversible_local")
            or spec.idempotency not in ("native", "via_action_id")
            or not entry.cancellable or entry.composite is not None
        ):
            raise ToolRegistryError(
                "RA-11",
                f"{name}: parallel_one_shot 需 internal_read／reversible_local、idempotency native／via_action_id、"
                "cancellable、非 composite",
            )

    @staticmethod
    def _check_conflict_keys(entry: ToolEntry) -> None:
        name, props = entry.name, entry.descriptor.input_schema.get("properties", {})
        for key in entry.conflict_keys:
            if key == "tool:{name}":
                continue
            for param in _PLACEHOLDER_RE.findall(key):
                definition = props.get(param)
                if definition is None or definition.get("type") not in _SCALAR_TYPES:
                    raise ToolRegistryError(
                        "RA-12", f"{name}: conflict_keys {key!r} 的 {{{param}}} 必須是 input_schema 的純量欄位"
                    )
        if (
            entry.concurrency == "parallel_one_shot" and entry.spec.side_effect_class != "internal_read"
            and not entry.conflict_keys
        ):
            raise ToolRegistryError("RA-12", f"{name}: 非唯讀的 parallel_one_shot 必須宣告 conflict_keys")

    @staticmethod
    def _check_variants(entry: ToolEntry) -> None:
        name, base = entry.name, entry.descriptor.input_schema
        base_props, base_required = base.get("properties", {}), set(base.get("required", ()))
        for platform, variant in entry.scope.variants.items():
            if not isinstance(variant, ToolVariant):
                raise ToolRegistryError("RA-13", f"{name}@{platform}: 變體必須是 ToolVariant（不得宣告分類欄）")
            if entry.scope.platforms and platform not in entry.scope.platforms:
                raise ToolRegistryError("RA-13", f"{name}@{platform}: 變體平台不在 scope.platforms 內")
            vs = variant.input_schema
            if vs is None:
                continue
            _lint_schema(vs, f"{name}@{platform}.input_schema", "RA-13")
            for prop, definition in vs.get("properties", {}).items():
                original = base_props.get(prop)
                if original is None or original.get("type") != definition.get("type"):
                    raise ToolRegistryError("RA-13", f"{name}@{platform}: 變體只能收窄（屬性 {prop!r} 新增或型別不同）")
                if "enum" in original and not set(definition.get("enum", original["enum"])) <= set(original["enum"]):
                    raise ToolRegistryError("RA-13", f"{name}@{platform}: 變體 enum 不得放寬（{prop!r}）")
            if not set(vs.get("required", ())) <= base_required:
                raise ToolRegistryError("RA-13", f"{name}@{platform}: 變體不得新增必填欄位")

    @staticmethod
    def _check_composite(entry: ToolEntry, by_name: Mapping[str, ToolEntry]) -> None:
        comp = entry.composite
        if comp is None:
            return
        name = entry.name
        if not comp.steps:
            raise ToolRegistryError("RA-14", f"{name}: composite 至少要有一個步驟")
        if entry.concurrency != "serial":
            raise ToolRegistryError("RA-14", f"{name}: composite 自身必須 serial")
        max_effect, max_approval = 0, 0
        for step in comp.steps:
            sub = by_name.get(step.tool)
            if sub is None:
                raise ToolRegistryError("RA-14", f"{name}: 子步驟 {step.tool!r} 未註冊")
            if sub.composite is not None:
                raise ToolRegistryError("RA-14", f"{name}: composite 不得巢狀（{step.tool!r}）")
            max_effect = max(max_effect, SIDE_EFFECT_ORDER.index(sub.spec.side_effect_class))
            max_approval = max(max_approval, APPROVAL_ORDER.index(sub.spec.approval_level))
        if SIDE_EFFECT_ORDER.index(entry.spec.side_effect_class) < max_effect:
            raise ToolRegistryError("RA-14", f"{name}: side_effect_class 低於子步驟最大值")
        if APPROVAL_ORDER.index(entry.spec.approval_level) < max_approval:
            raise ToolRegistryError("RA-14", f"{name}: approval_level 低於子步驟最大值")

    @staticmethod
    def _check_lockfile(
        entries: Mapping[str, ToolEntry], hashes: Mapping[str, str], lock: Mapping[str, Mapping[str, Any]]
    ) -> None:
        """RA-8：hash 變而 version 未升 ⇒ 失敗；version 倒退 ⇒ 失敗。"""
        for key, digest in hashes.items():
            locked = lock.get(key)
            if locked is None:
                continue  # 新條目：是否已登記由 CI 的 lockfile 同步測試負責
            version = entries[key.split("@", 1)[0]].spec.tool_version
            if version < int(locked["tool_version"]):
                raise ToolRegistryError("RA-8", f"{key}: tool_version 低於 lockfile")
            if digest != locked["schema_hash"] and version == int(locked["tool_version"]):
                raise ToolRegistryError("RA-8", f"{key}: schema_hash 變更但 tool_version 未升")

    # --- resolver 協定 ---

    def resolve(self, tool: str) -> ToolSpec:
        spec = self._specs.get(tool)
        if spec is None:
            raise unknown_tool(tool)
        return spec

    def specs(self) -> Mapping[str, ToolSpec]:
        return self._specs

    # --- 查詢與衍生 ---

    def entry(self, tool: str) -> ToolEntry:
        found = self._entries.get(tool)
        if found is None:
            raise unknown_tool(tool)
        return found

    def entries(self) -> Mapping[str, ToolEntry]:
        return self._entries

    def visible_entries(self) -> list[ToolEntry]:
        """可進 LLM 的條目（RA-3）：origin 屬 VISIBLE_ORIGINS 且未停用；依註冊順序。"""
        return [e for e in self._entries.values() if e.spec.origin in VISIBLE_ORIGINS and not e.disabled]

    def tool_classes(self) -> dict[str, str]:
        """可見工具 -> side_effect_class（取代 ``access.TOOL_CLASSES`` 的獨立字面值）。"""
        return {e.name: e.spec.side_effect_class for e in self.visible_entries()}

    def llm_definition(self, tool: str) -> dict[str, Any]:
        """OpenAI function 格式：``{"type":"function","function":{name,description,parameters}}``（深複本）。"""
        entry = self.entry(tool)
        if entry.spec.origin not in VISIBLE_ORIGINS or entry.disabled:
            raise unknown_tool(tool)
        return {
            "type": "function",
            "function": {
                "name": entry.name,
                "description": entry.descriptor.description,
                "parameters": _thaw(entry.descriptor.input_schema),
            },
        }

    def llm_definitions(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """依 ``names`` 順序（預設註冊順序）衍生可見工具的 LLM 面宣告；不可見或未知的名稱略過。"""
        visible = [e.name for e in self.visible_entries()]
        order = visible if names is None else [n for n in names if n in set(visible)]
        return [self.llm_definition(n) for n in order]

    def schema_hashes(self) -> Mapping[str, str]:
        return self._hashes

    def lock_snapshot(self) -> dict[str, dict[str, Any]]:
        """lockfile 內容：``name[@variant] -> {tool_version, schema_hash}``（鍵排序）。"""
        return {
            key: {"tool_version": self._entries[key.split("@", 1)[0]].spec.tool_version, "schema_hash": self._hashes[key]}
            for key in sorted(self._hashes)
        }

    def readiness(self, tool: str) -> Readiness:
        """§5.8：``AND over requires``。任一 requires 未知（module coordinator 不認得）或 ``unavailable`` ⇒
        ``unavailable``（fail-closed，RA-15）；否則有 ``warming`` ⇒ ``warming``；否則 ``ready``。
        已知模組在收到第一個事件前視為 ready（維持切片 A 行為，S-A 測試鎖定）；因此啟動時 module coordinator 必須先發
        各模組的當前狀態事件，``warm_required`` 工具才不會在預熱前被投出。純記憶體查詢，不做 I/O。"""
        entry = self.entry(tool)
        states = self._module_state
        result: Readiness = "ready"
        for module in entry.requires:
            if module not in self._known_modules:
                return "unavailable"
            recorded = states.get(module)
            state: Readiness = recorded[1] if recorded else "ready"
            if state == "unavailable":
                return "unavailable"
            if state == "warming":
                result = "warming"
        return result

    def apply_module_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        """接 module coordinator 事件（``module.ready``／``module.warming``／``module.unavailable``，
        payload ``{module_id, revision, reason_code?}``，§5.8）。以**最新 revision** 為準：revision 不大於已記錄者
        （重送、亂序）一律忽略；未知事件、未知模組、格式不符也忽略（維持原狀態，fail-closed）並記警告。
        回傳是否採用。"""
        state = MODULE_EVENT_STATES.get(event_type)
        module = payload.get("module_id") if isinstance(payload, Mapping) else None
        revision = payload.get("revision") if isinstance(payload, Mapping) else None
        if state is None or not isinstance(module, str) or isinstance(revision, bool) or not isinstance(revision, int):
            logger.warning("忽略格式不符的模組事件：%s", event_type if state is None else "payload")
            return False
        if module not in self._known_modules:
            logger.debug("忽略未知模組的事件：%s", module)
            return False
        with self._event_lock:
            recorded = self._module_state.get(module)
            if recorded is not None and revision <= recorded[0]:
                return False
            self._module_state = MappingProxyType({**self._module_state, module: (revision, state)})
            self._projection_revision += 1
        return True

    # --- 投影（§6.1） ---

    def effective_input_schema(self, tool: str, variant: str | None = None) -> dict[str, Any]:
        """LLM 面與 Gateway 驗證共用的有效 schema（深複本）：變體 schema（若有）或基底，且每個物件節點都補上
        ``additionalProperties:false``（RA-7）。與 ``llm_definition`` 的舊路徑不同——那條路徑位元組不變。"""
        entry = self.entry(tool)
        schema = entry.descriptor.input_schema
        if variant is not None:
            override = entry.scope.variants.get(variant)
            if override is None:
                raise unknown_tool(f"{tool}@{variant}")
            if override.input_schema is not None:
                schema = override.input_schema
        return strict_schema(schema)

    def project(
        self,
        capability_tags: Iterable[str],
        ctx: ProjectionContext,
        *,
        ceiling: Iterable[str] | None = None,
    ) -> Mapping[str, Any]:
        """依已收窄的能力標籤與場域脈絡產生 ``capability_set``（§6.1，TG-12 形狀）。

        篩選（全部成立才投出）：``entry.capability ∈ tags``、可見 origin 且未停用、scope（平台／``requires_context``／
        Presence／audience，§5.6）、readiness 為 ``ready``。``warming`` 且通過其餘篩選者進 ``pending``（只給 AgentLoop，
        不渲染給模型）；``unavailable`` 不投出也不報。同平台有變體時帶 ``variant``，描述與 schema 取變體。

        AL-3 能力單調：``ceiling``（Run 開始時凍結的標籤集合）給了就以 ``tags ∩ ceiling`` 為準，所以後續投影永遠不會
        比凍結範圍更寬；標籤是閉集的精確比對（無萬用字元），閉集外的標籤直接丟棄。
        回傳唯讀結構：``{tools, pending, revision, tags}``，``revision`` 是 readiness 事件版本（審計與 resume 比對用）。"""
        if isinstance(capability_tags, str) or (ceiling is not None and isinstance(ceiling, str)):
            raise TypeError("capability_tags／ceiling 必須是標籤集合，不是單一字串")
        tags = frozenset(capability_tags) & CAPABILITY_CEILING.keys()
        if ceiling is not None:
            tags &= frozenset(ceiling)
        audience = audience_rank(ctx.audience)
        tools: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for entry in self.visible_entries():
            if entry.spec.capability not in tags:
                continue
            scope = entry.scope
            if scope.platforms and ctx.platform not in scope.platforms:
                continue
            if not scope.requires_context <= ctx.available_context:
                continue
            if scope.presence == "bound" and not ctx.presence_active():
                continue
            if audience < audience_rank(scope.audience_min):
                continue
            state = self.readiness(entry.name)
            if state == "warming":
                pending.append({"tool": entry.name, "reason": "warming"})
                continue
            if state != "ready":
                continue
            variant = ctx.platform if ctx.platform in scope.variants else None
            override = scope.variants[variant] if variant is not None else None
            tools.append({
                "tool": entry.name,
                "version": entry.spec.tool_version,
                "description": override.description if override and override.description is not None
                else entry.descriptor.description,
                "input_schema": self.effective_input_schema(entry.name, variant),
                "variant": variant,
            })
        return _freeze({
            "tools": tools, "pending": pending, "revision": self._projection_revision, "tags": sorted(tags),
        })
