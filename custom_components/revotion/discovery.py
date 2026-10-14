"""Dynamic-discovery bookkeeping shared by the native and Connect platforms.

Every platform adds entities from a coordinator listener as nodes and
capabilities show up. :class:`EntityTracker` is the single source of truth for
"already added" in those listeners, and it follows HA's entity lifecycle rather
than the coordinator snapshot:

- The coordinator fires on every MQTT message and a REST sync rebuilds the whole
  Brain tree, so a node can briefly be missing (optimistic unpair drop while the
  backend still lists it) or a Connect cap can lack its device code for one
  update. Its entities stay live in HA meanwhile -- building them again would add
  a second entity with the same unique_id ("Platform revotion does not generate
  unique IDs"). Absence from an update is therefore never a reason to forget.
- A node that is really unpaired has its device pruned
  (``RevotionCoordinator._reconcile_node_devices``), which deletes its entity
  registry rows and removes the entities. Only then may a re-pair of the same MAC
  in the same session build them again -- so the tracker forgets an entity
  exactly when its registry row is removed.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import Event, callback
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity


class EntityTracker:
    """The dynamically discovered entities of one platform that are live in HA.

    ``key in tracker`` means the entity for that key exists (or its add is in
    flight) and must not be built again. Two ways to key it:

    - :meth:`track` with a caller-built key -- the Connect discovery computes
      ``(node_mac, cap_index, key)`` before building, so it never constructs an
      entity that is already live;
    - :meth:`claim` keyed by the entity's unique_id -- the native discovery
      builds a node's entities first and keeps only those HA does not have.

    A key is forgotten only when HA really lost the entity:

    - :meth:`forget` -- the platform removes it itself (presence gate off);
    - its entity-registry row is removed -- node-device pruning after an
      unpair, or a user deleting it -- so a later re-pair rebuilds it.

    Registry-row removal is the right signal: every path that really removes one
    of these entities goes through the registry, while an entity HA merely
    skipped (disabled in the registry) keeps its row and so stays tracked instead
    of being rebuilt on every update.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Start following entity-registry removals for the entry's lifetime."""
        self._entities: dict[Hashable, Entity] = {}
        entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_registry_updated))

    def __contains__(self, key: object) -> bool:
        """Return whether the entity for ``key`` is live (or being added)."""
        return key in self._entities

    def __len__(self) -> int:
        """Return the number of tracked entities."""
        return len(self._entities)

    def get(self, key: Hashable) -> Entity | None:
        """Return the tracked entity for ``key``, if any."""
        return self._entities.get(key)

    def track(self, key: Hashable, entity: Entity) -> None:
        """Record ``entity`` as added for ``key`` (call right before adding it)."""
        self._entities[key] = entity

    def claim(self, entities: Iterable[Entity]) -> list[Entity]:
        """Track and return those of ``entities`` whose unique_id is not live yet.

        The returned list is what the caller must add. A node re-evaluated after
        it briefly left coordinator.data yields nothing; one whose entities HA
        removed (device pruned) gets all of them back.
        """
        new: list[Entity] = []
        for entity in entities:
            if entity.unique_id in self._entities:
                continue
            self._entities[entity.unique_id] = entity
            new.append(entity)
        return new

    def forget(self, key: Hashable) -> None:
        """Drop ``key`` after the platform removed its entity itself."""
        self._entities.pop(key, None)

    @callback
    def _async_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Forget an entity whose registry row was removed (entity gone from HA)."""
        if event.data["action"] != "remove":
            return
        entity_id = event.data["entity_id"]
        for key, entity in list(self._entities.items()):
            # entity_id is only set once HA registered the entity, so an add
            # still in flight can never match.
            if entity.entity_id == entity_id:
                del self._entities[key]
