"""Lifecycle-owned policy observation. HA access stays on the event loop."""

import asyncio
import time
from copy import deepcopy

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import TrackStates, async_track_state_change_filtered
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations

from .attention import attention_snapshot, source_ids
from .attention_journal import record_attention
from .handling import episode_detail, project_handling, record_handling, validate_request
from .models import digest
from .policy import presentation, validate_policy

ATTRS = set(
    """friendly_name device_class unit_of_measurement restored installed_version latest_version
    in_progress title subject description importance supported_features release_summary""".split()
)


def state_data(state, household=False):
    if state is None:
        return None
    attrs = {
        k: deepcopy(v)
        for k, v in state.attributes.items()
        if k in ATTRS or household and k in ("schema_version", "revision", "rules")
    }
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": attrs,
        "last_changed": state.last_changed.isoformat(),
    }


def read_only_script(entity):
    config = getattr(entity, "raw_config", None)
    sequence = config.get("sequence") if isinstance(config, dict) else None
    return bool(
        sequence
        and isinstance(sequence, list)
        and all(
            isinstance(step, dict)
            and ("variables" in step or "stop" in step)
            and not set(step)
            - {"alias", "variables", "stop", "response_variable", "error", "enabled"}
            for step in sequence
        )
    )


class AttentionRuntime:
    def __init__(self, coordinator):
        self.owner = coordinator
        self.hass = coordinator.hass
        self.store = Store(self.hass, 1, f"home_insights.{coordinator.entry.entry_id}.attention")
        self.policies = {}
        self.inputs = {}
        self.results = {}
        self.dependencies = set()
        self.guards = set()
        self.proof_task = None
        self.resumed = set()
        self.removers = []
        self.last_proof = 0
        self.policy_lock = asyncio.Lock()
        self.started = False

    async def load(self):
        saved = await self.store.async_load() or {}
        self.policies = {name: validate_policy(p) for name, p in saved.items()}

    async def set_policy(self, name, policy, expected_hash=None):
        async with self.policy_lock:
            return await self._set_policy(name, policy, expected_hash)

    async def _set_policy(self, name, policy, expected_hash):
        if not name or len(name) > 80 or not all(c.isalnum() or c in "_-" for c in name):
            raise ValueError("Invalid policy ID")
        value = validate_policy(policy)
        old = self.policies.get(name)
        if old and expected_hash != digest(old):
            raise ValueError("Policy changed; read its current hash before replacing it")
        candidate = {**self.policies, name: value}
        bindings = {
            (p["household"]["entity"], p["household"]["evaluate_script"])
            for p in candidate.values()
            if p.get("household")
        }
        if len(bindings) > 1:
            raise ValueError("Policies must use the same shared household evaluator")
        await self.store.async_save(candidate)
        self.policies = candidate
        self.resumed.discard(name)
        self.capture()
        if value.get("household"):
            await self.refresh_proofs()
        self.queue()
        await self.owner.async_flush()
        return {"policy_id": name, "policy_hash": digest(value), "revision": value["revision"]}

    @callback
    def start(self):
        if self.started:
            return
        self.started = True
        tracker = async_track_state_change_filtered(
            self.hass, TrackStates(True, set(), set()), self.changed
        )
        self.removers.extend(
            [
                tracker.async_remove,
                self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self.registry_changed),
            ]
        )
        self.capture()
        self.queue()
        self.schedule_proofs()

    @callback
    def capture(self):
        household = {p["household"]["entity"] for p in self.policies.values() if p.get("household")}
        states = {
            s.entity_id: state_data(s, s.entity_id in household)
            for s in self.hass.states.async_all()
        }
        registry = {
            s.entity_id: {
                "platform": s.platform,
                "device_id": s.device_id,
                "disabled_by": s.disabled_by,
                "labels": list(s.labels),
            }
            for s in er.async_get(self.hass).entities.values()
        }
        old = self.inputs
        self.inputs = {
            "states": states,
            "registry": registry,
            "external": old.get(
                "external",
                {"readiness": {k: "loading" for k in ("notifications", "repairs", "integrations")}},
            ),
        }
        dependencies = set()
        guards = set()
        for policy in self.policies.values():
            if policy.get("acknowledgements_entity"):
                dependencies.add(policy["acknowledgements_entity"])
            dependencies.update(policy.get("adapters", {}).get("events", []))
            for rule in policy["rules"]:
                sources = (
                    [rule["entity"]] if rule.get("entity") else source_ids(states, registry, rule)
                )
                dependencies.update(sources)
                for source in sources:
                    suffix = rule.get("related_suffix")
                    if suffix:
                        dependencies.add(source.replace(suffix["from"], suffix["to"], 1))
                    device = registry.get(source, {}).get("device_id")
                    for item in rule.get("context", []):
                        if item.get("entity"):
                            dependencies.add(item["entity"])
                        elif device and item.get("entity_pattern"):
                            from .attention import glob

                            dependencies.update(
                                s
                                for s in states
                                if registry.get(s, {}).get("device_id") == device
                                and glob(item["entity_pattern"], s)
                            )
            dependencies.update(
                s
                for s, e in states.items()
                if s.startswith("update.")
                and policy.get("updates", {}).get("enabled")
                or s.startswith("binary_sensor.")
                and policy.get("problems", {}).get("enabled")
                and e["attributes"].get("device_class") in ("problem", "safety", "tamper")
            )
            if config := policy.get("household"):
                guards.add(config["entity"])
                for rule in states.get(config["entity"], {}).get("attributes", {}).get("rules", []):
                    guards.update(rule.get("targets", []))
                    guards.update(w["entity"] for w in rule.get("when", []))
        self.guards = guards
        self.dependencies = dependencies | guards

    @callback
    def registry_changed(self, _event):
        if self.policies:
            self.capture()
            self.queue()

    @callback
    def changed(self, event):
        identifier = event.data["entity_id"]
        if not self.policies:
            return
        if event.data["old_state"] is None:
            self.capture()
            self.queue()
            return
        if identifier not in self.dependencies:
            return
        value = state_data(event.data["new_state"], identifier in self.guards)
        states = {**self.inputs["states"]}
        if value is None:
            states.pop(identifier, None)
        else:
            states[identifier] = value
        self.inputs = {**self.inputs, "states": states}
        if identifier in self.guards:
            self.schedule_proofs(invalidate=True)
        self.queue()

    @callback
    def native(self, adapter, rows, error=False):
        external = {**self.inputs.get("external", {})}
        readiness = {
            **external.get("readiness", {}),
            "registry": "ready",
            adapter: "error" if error else "ready",
        }
        external["readiness"] = readiness
        errors = {**external.get("errors", {})}
        if error:
            errors[adapter] = "The data source could not be read"
            external.pop(adapter, None)
        else:
            errors.pop(adapter, None)
            if adapter == "notifications":
                external[adapter] = [
                    {
                        "notification_id": r.source_id,
                        "title": r.title,
                        "message": r.message,
                        "created_at": r.source_time,
                    }
                    for r in rows
                ]
            elif adapter == "repairs":
                external[adapter] = {
                    "issues": [
                        {
                            "domain": r.facts["domain"],
                            "issue_id": r.facts["issue_id"],
                            "severity": r.severity,
                            "dismissed_version": "ignored" if r.status == "excluded" else None,
                            "translation_key": r.facts["translation_key"],
                            "translation_placeholders": r.facts["placeholders"],
                        }
                        for r in rows
                    ]
                }
                # Build a translation projection from HA-enriched immutable observations.
                external["repairTranslations"] = {
                    k: v
                    for r in rows
                    for k, v in (
                        (
                            f"component.{r.facts['domain']}.issues."
                            f"{r.facts['translation_key'] or r.facts['issue_id']}.title",
                            r.title,
                        ),
                        (
                            f"component.{r.facts['domain']}.issues."
                            f"{r.facts['translation_key'] or r.facts['issue_id']}.description",
                            r.message,
                        ),
                    )
                }
            else:
                external[adapter] = [
                    {
                        "entry_id": r.source_id,
                        "domain": r.facts["domain"],
                        "title": r.title,
                        "state": r.facts["state"],
                        "disabled_by": "excluded" if r.status == "excluded" else None,
                    }
                    for r in rows
                ]
        external["errors"] = errors
        self.inputs = {**self.inputs, "external": external}

    @callback
    def queue(self):
        if self.policies and self.started:
            self.owner.enqueue("attention", self.inputs)

    @callback
    def tick(self):
        self.queue()
        if time.time() - self.last_proof >= 30:
            self.schedule_proofs()

    @callback
    def schedule_proofs(self, invalidate=False):
        if invalidate:
            self.inputs = {
                **self.inputs,
                "external": {
                    **self.inputs.get("external", {}),
                    "household": None,
                    "householdStatus": "loading",
                },
            }
        if self.proof_task is None or self.proof_task.done():
            self.proof_task = self.hass.async_create_background_task(
                self.refresh_proofs(), "home_insights_household_read"
            )

    async def refresh_proofs(self):
        configs = [p["household"] for p in self.policies.values() if p.get("household")]
        if not configs:
            return
        # A single shared registry/evaluator is the v1 contract; reject ambiguous imports.
        config = configs[0]
        self.last_proof = time.time()
        try:
            component = self.hass.data.get("script")
            entity = component.get_entity(config["evaluate_script"]) if component else None
            if not read_only_script(entity):
                raise ValueError("Household evaluator is not a read-only variables/response script")
            async with asyncio.timeout(10):
                proof = await self.hass.services.async_call(
                    "script",
                    config["evaluate_script"].split(".", 1)[1],
                    {},
                    blocking=True,
                    return_response=True,
                )
            if proof.get("schema_version") != 1 or not isinstance(proof.get("evaluations"), list):
                raise ValueError("Invalid household response")
            status = "ready" if proof.get("status") == "ready" else "error"
        except Exception:
            proof = None
            status = "error"
        if self.owner.closed:
            return
        self.inputs = {
            **self.inputs,
            "external": {
                **self.inputs.get("external", {}),
                "household": proof,
                "householdStatus": status,
            },
        }
        self.queue()

    async def evaluate(self, inputs):
        for name, policy in self.policies.items():
            external = {**inputs["external"], "registry": inputs["registry"]}
            now = time.time() * 1000
            rows = await self.owner.execute(
                attention_snapshot,
                inputs["states"],
                inputs["registry"],
                policy,
                external,
                now,
                self.hass.config.language,
            )
            complete = not any(r["status"] in ("unavailable", "loading") for r in rows)
            persisted = await self.owner.execute(
                record_attention,
                self.owner.journal,
                name,
                policy,
                rows,
                now / 1000,
                name not in self.resumed,
                complete,
                {source: entity.get("last_changed") for source, entity in inputs["states"].items()},
                (external.get("household") or {}).get("revision"),
                inputs["states"]
                .get(policy.get("household", {}).get("entity"), {})
                .get("attributes")
                if policy.get("household")
                else None,
            )
            self.resumed.add(name)
            self.results[name] = {
                "schema_version": 1,
                "policy_id": name,
                "policy_hash": digest(policy),
                "policy_revision": policy["revision"],
                "generated_at": now,
                "rows": persisted,
                "policy": presentation(policy),
            }

    async def read(self, name, language="en", user=None, debug=False):
        await self.owner.async_flush()
        if self.owner.failed or self.owner.closed or name not in self.results:
            raise ValueError("Attention backend is not ready")
        if time.time() * 1000 - self.results[name]["generated_at"] > 45000:
            raise ValueError("Attention observation is stale")
        policy = self.policies[name]
        inputs = self.inputs
        external = {**inputs["external"]}
        states = inputs["states"]
        registry = inputs["registry"]
        if user is not None and not user.is_admin:

            def allowed(entity):
                return user.permissions.check_entity(entity, POLICY_READ)

            states = {k: v for k, v in states.items() if allowed(k)}
            registry = {k: v for k, v in registry.items() if allowed(k)}
            policy = deepcopy(policy)
            policy["rules"] = [
                r for r in policy["rules"] if not r.get("entity") or allowed(r["entity"])
            ]
            for rule in policy["rules"]:
                rule["context"] = [
                    c
                    for c in rule.get("context", [])
                    if not c.get("entity") or allowed(c["entity"])
                ]
        domains = {i["domain"] for i in external.get("repairs", {}).get("issues", [])}
        if domains:
            try:
                external["repairTranslations"] = await async_get_translations(
                    self.hass, language, "issues", domains
                )
            except Exception:
                external.pop("repairTranslations", None)
        external["registry"] = registry
        now = time.time() * 1000
        rows = await self.owner.execute(
            attention_snapshot, states, registry, policy, external, now, language
        )
        persisted = {r["id"]: r for r in self.results[name]["rows"]}
        for row in rows:
            old = persisted.get(row["id"])
            if old and old["status"] == row["status"]:
                row["key"] = old["key"]
                if old.get("episodeId"):
                    row["episodeId"] = old["episodeId"]
        rows = await self.owner.execute(project_handling, self.owner.journal.db, rows, now / 1000)
        for row in rows:
            row["canHandle"] = bool(
                row.get("episodeId")
                and user is not None
                and (
                    user.is_admin
                    or self.hass.states.get(row["source"]) is not None
                    and user.permissions.check_entity(row["source"], POLICY_CONTROL)
                )
            )
        result = {
            **self.results[name],
            "rows": rows,
            "policy": presentation(policy),
            "capabilities": {"handling": True},
        }
        if debug and user is not None and user.is_admin:
            result["contract_input"] = {
                "states": states,
                "registry": registry,
                "policy": policy,
                "external": external,
                "now": now,
                "language": language,
            }
        return result

    async def episode(self, policy, episode, user, before=None, limit=20, control=False):
        await self.owner.async_flush()
        if self.owner.failed or self.owner.closed:
            raise ValueError("Attention backend is not ready")
        result = await self.owner.execute(
            episode_detail, self.owner.journal.db, policy, episode, before, limit
        )
        permission = POLICY_CONTROL if control else POLICY_READ
        if user is None or not (
            user.is_admin
            or self.hass.states.get(result["source"]) is not None
            and user.permissions.check_entity(result["source"], permission)
        ):
            raise Unauthorized()
        return result

    async def handle(self, value, user):
        if user is None:
            raise Unauthorized()
        validate_request(value, time.time(), retry=True)
        # Authorize even idempotent retries against the actual stored episode.
        for target in value.get("targets", []):
            await self.episode(value["policy_id"], target["episode_id"], user, control=True)
        result = await self.owner.execute(
            record_handling,
            self.owner.journal,
            value,
            user.id,
            user.name or "Home Assistant user",
            time.time(),
        )
        self.owner.data = await self.owner.execute(self.owner.journal.snapshot)
        self.owner._notify()
        return result

    async def close(self):
        for remove in self.removers:
            remove()
        self.removers = []
        if self.proof_task and not self.proof_task.done():
            self.proof_task.cancel()
            try:
                await self.proof_task
            except asyncio.CancelledError:
                pass
