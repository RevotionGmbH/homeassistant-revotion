"""Runtime presence-gating for Connect entities.

Brain availability flags (Alde ``z2_av``, Dometic ``fan_one_av``, Truma
``is_con`` / ``light_av``, FreshJet ``in_light_av`` ...) report whether an
optional unit/feature is physically present on this install -- not a transient
outage. Such entities are therefore created and removed at runtime rather than
shown greyed-out as "unavailable": the coordinator listener reconciles the live
entity set on every update via :func:`reconcile_gated_entities`.

This is the descriptor-pattern analogue of HA's standard "only create entities
for capabilities the device actually has" rule, extended to flags that resolve
*after* pairing (deferred discovery). A missing flag means "present" so a device
that simply omits the flag is never hidden. See Ha-Integration-Docs/connect-integration.md.

Removal is keyed on the entity's ``unique_id`` (not just a live reference) so a
ghost registry row left by an earlier session -- e.g. a zone-2 climate created
before this gate existed -- is cleaned up on the first reconcile after a
restart, not only when a flag flips during a running session.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from ..const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

# A reconciliation candidate for one gated spec of one Connect capability:
#   key        -- tracking key; its first element is the node MAC
#   present    -- whether the entity should exist now (gate truthy / absent)
#   unique_id  -- the entity's unique_id (used to find + remove its registry row)
#   factory    -- zero-arg builder, called only when the entity must be added
Candidate = tuple[tuple[Hashable, ...], bool, str, Callable[[], "Entity"]]


def remove_gated_entity(hass: HomeAssistant, entity_domain: str, unique_id: str) -> None:
    """Remove a presence-gated entity by unique_id -- live state *and* registry row.

    ``EntityRegistry.async_remove`` drops the registry entry and signals the
    owning platform to remove the live entity, so a single synchronous call
    (safe from a coordinator listener) makes the entity disappear with no
    greyed-out "unavailable" leftover and no orphaned registry row that would
    re-surface as a ghost on the next restart. Keying on unique_id (rather than a
    held entity reference) also cleans up a row left by a *previous* session, the
    first time the gate is evaluated after a restart. No-op when no such row
    exists (already removed, or never created).
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(entity_domain, DOMAIN, unique_id)
    if entity_id is not None:
        registry.async_remove(entity_id)


def reconcile_gated_entities(
    *,
    hass: HomeAssistant,
    entity_domain: str,
    entities: dict[tuple[Hashable, ...], Entity],
    current_macs: set[str],
    candidates: list[Candidate],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add/remove presence-gated entities so the live set matches the Brain flags.

    ``entity_domain`` is the HA platform domain ("climate" / "switch" / "light")
    used to resolve a unique_id back to an entity_id in the registry.
    ``entities`` is the platform's persistent ``key -> entity`` map (the source
    of truth for "already created this session"). ``candidates`` is the full list
    of ``(key, present, unique_id, factory)`` for every gated spec of every
    *currently known* Connect capability.

    - present & not yet created  -> build via ``factory`` and add.
    - not present                -> remove the entity (live + registry row,
      including a ghost left by an earlier session) and drop any tracking.
    - tracked key absent from ``candidates`` (its node unpaired) -> drop tracking
      only; node-device teardown takes the entity unavailable, matching the
      prior behaviour (we must not delete its registry row here).
    """
    present_keys: set[tuple[Hashable, ...]] = set()
    candidate_keys: set[tuple[Hashable, ...]] = set()
    new_entities: list[Entity] = []

    for key, present, unique_id, factory in candidates:
        candidate_keys.add(key)
        if present:
            present_keys.add(key)
            if key not in entities:
                entity = factory()
                entities[key] = entity
                new_entities.append(entity)
        else:
            # Gated off: forget any live reference and remove the entity + any
            # orphaned registry row (idempotent -- no-op once gone).
            entities.pop(key, None)
            remove_gated_entity(hass, entity_domain, unique_id)

    # Tracked keys no longer produced at all -> the node unpaired. Drop the
    # reference only; the node-device teardown handles the entity itself.
    for key in list(entities):
        if key not in candidate_keys:
            entities.pop(key, None)

    if new_entities:
        async_add_entities(new_entities)
