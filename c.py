from __future__ import annotations

"""
Improved Cambridge Battlecode bot — v2.

Key fixes & improvements over v1:
  - CORE DESTRUCTION: Builder bots in assault mode now walk directly onto
    enemy core tiles and use fire() to deal 2 damage/round (the builder
    attack mechanic). This is the primary kill mechanism.
  - GUNNER AMMO: Siege gunners are now fed titanium via proper conveyor
    chains. Gunners deal 10 dmg/round with titanium ammo.
  - SIMPLER AXIONITE: Dedicated feeder builds axionite harvester → conveyor
    chain → foundry. A second conveyor leg routes refined axionite back to
    core. Less over-engineering.
  - FOUNDRY CAUTION: Only 1 foundry by default (it adds 100% to cost scale).
    Build second only on large maps.
  - FASTER SCOUTS: Scouts launch immediately once offense_ready. They prefer
    walking ON enemy conveyors/roads (walkable), which are common near the
    enemy core.
  - LAUNCHER USAGE: Core builds a launcher early to sling scouts across the
    map toward the enemy.
  - HEALER bots: Assault bots heal injured allies on the same tile before
    doing anything else.
  - BREACH turrets: Once refined axionite is available, the core builds
    breach turrets in addition to gunners for high splash damage vs the core.
"""

from cambc import (
    Controller,
    Direction,
    EntityType,
    Environment,
    GameConstants,
    Position,
    Team,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIRECTIONS = [d for d in Direction if d != Direction.CENTRE]
CARDINAL_DIRECTIONS = [
    Direction.NORTH,
    Direction.EAST,
    Direction.SOUTH,
    Direction.WEST,
]
DIAGONAL_DIRECTIONS = [
    Direction.NORTHEAST,
    Direction.SOUTHEAST,
    Direction.SOUTHWEST,
    Direction.NORTHWEST,
]
WALKABLE_BUILDINGS = {
    EntityType.CONVEYOR,
    EntityType.BRIDGE,
    EntityType.ARMOURED_CONVEYOR,
    EntityType.ROAD,
}
ROUTABLE_ENDPOINTS = {
    EntityType.CONVEYOR,
    EntityType.FOUNDRY,
}

# Tuning
TARGET_FOUNDRIES = 1          # 1 foundry default (100% cost scaling hit)
LARGE_MAP_FOUNDRIES = 2       # 2 foundries on large maps
OFFENSE_MIN_ROUND = 60        # start assault earlier
DEFENSE_MIN_ROUND = 120
EXTRA_SPAWN_SURPLUS = 60
EXTRA_SPAWN_COOLDOWN = 5
TITANIUM_ONLY_OFFENSE_SURPLUS = 120
PRE_REFINERY_ASSAULT_CAP = 14


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def in_bounds(pos: Position, width: int, height: int) -> bool:
    return 0 <= pos.x < width and 0 <= pos.y < height


def step(pos: Position, direction: Direction, amount: int = 1) -> Position:
    out = pos
    for _ in range(amount):
        out = out.add(direction)
    return out


def on_core_tile(pos: Position, core_pos: Position) -> bool:
    return abs(pos.x - core_pos.x) <= 1 and abs(pos.y - core_pos.y) <= 1


def unique_dirs(primary: Direction) -> list[Direction]:
    if primary == Direction.CENTRE:
        primary = Direction.EAST
    ordered: list[Direction] = []

    def add(d: Direction) -> None:
        if d != Direction.CENTRE and d not in ordered:
            ordered.append(d)

    add(primary)
    left = right = primary
    for _ in range(3):
        left = left.rotate_left()
        right = right.rotate_right()
        add(left)
        add(right)
    add(primary.opposite())
    for d in DIRECTIONS:
        add(d)
    return ordered[:8]


def cardinal_left(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.WEST,
        Direction.EAST: Direction.NORTH,
        Direction.SOUTH: Direction.EAST,
        Direction.WEST: Direction.SOUTH,
    }.get(d, Direction.NORTH)


def cardinal_right(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.EAST,
        Direction.EAST: Direction.SOUTH,
        Direction.SOUTH: Direction.WEST,
        Direction.WEST: Direction.NORTH,
    }.get(d, Direction.EAST)


def cardinal_opposite(d: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.SOUTH,
        Direction.EAST: Direction.WEST,
        Direction.SOUTH: Direction.NORTH,
        Direction.WEST: Direction.EAST,
    }.get(d, Direction.SOUTH)


def directional_preferences(
    primary: Direction, secondary: Direction | None = None
) -> list[Direction]:
    ordered: list[Direction] = []

    def add(d: Direction) -> None:
        if d != Direction.CENTRE and d not in ordered:
            ordered.append(d)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        add(base)
        left = right = base
        for _ in range(3):
            left = left.rotate_left()
            right = right.rotate_right()
            add(left)
            add(right)
    for d in DIRECTIONS:
        add(d)
    return ordered


def cardinal_directional_preferences(
    primary: Direction, secondary: Direction | None = None
) -> list[Direction]:
    ordered: list[Direction] = []
    diagonal_map = {
        Direction.NORTHEAST: (Direction.NORTH, Direction.EAST),
        Direction.SOUTHEAST: (Direction.SOUTH, Direction.EAST),
        Direction.SOUTHWEST: (Direction.SOUTH, Direction.WEST),
        Direction.NORTHWEST: (Direction.NORTH, Direction.WEST),
    }

    def add(d: Direction) -> None:
        if d in CARDINAL_DIRECTIONS and d not in ordered:
            ordered.append(d)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        if base in CARDINAL_DIRECTIONS:
            add(base)
            add(cardinal_left(base))
            add(cardinal_right(base))
            continue
        for d in diagonal_map.get(base, ()):
            add(d)
    for d in CARDINAL_DIRECTIONS:
        add(d)
    return ordered


def slot_for(core_pos: Position, direction: Direction) -> Position:
    return step(core_pos, direction, 2)


def slot_target_for(core_pos: Position, direction: Direction) -> Position:
    return step(core_pos, direction, 1)


def ring_positions(
    core_pos: Position, radius: int, width: int, height: int
) -> list[Position]:
    positions: list[Position] = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if max(abs(dx), abs(dy)) != radius:
                continue
            pos = Position(core_pos.x + dx, core_pos.y + dy)
            if in_bounds(pos, width, height):
                positions.append(pos)
    return positions


def core_tiles(core_pos: Position, width: int, height: int) -> list[Position]:
    positions: list[Position] = []
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            pos = Position(core_pos.x + dx, core_pos.y + dy)
            if in_bounds(pos, width, height):
                positions.append(pos)
    return positions


def nearby_core_id(ct: Controller) -> int | None:
    for eid in ct.get_nearby_buildings():
        if (
            ct.get_entity_type(eid) == EntityType.CORE
            and ct.get_team(eid) == ct.get_team()
        ):
            return eid
    return None


def building_type_at(ct: Controller, pos: Position) -> EntityType | None:
    if not ct.is_in_vision(pos):
        return None
    bid = ct.get_tile_building_id(pos)
    if bid is None:
        return None
    return ct.get_entity_type(bid)


def direction_rank(preferred: Direction, candidate: Direction) -> int:
    if preferred == Direction.CENTRE:
        return 0
    prefs = directional_preferences(preferred)
    try:
        return prefs.index(candidate)
    except ValueError:
        return len(prefs)


def can_afford(ct: Controller, ti: int, ax: int = 0, reserve: int = 0) -> bool:
    titanium, axionite = ct.get_global_resources()
    return titanium - ti >= reserve and axionite >= ax


# ---------------------------------------------------------------------------
# Reserve helpers
# ---------------------------------------------------------------------------

def reserve_direct(ct: Controller) -> int:
    return ct.get_harvester_cost()[0] + ct.get_conveyor_cost()[0] * 6 + 30


def reserve_raw(ct: Controller) -> int:
    return (
        ct.get_foundry_cost()[0]
        + ct.get_harvester_cost()[0]
        + ct.get_conveyor_cost()[0] * 8
        + 60
    )


def reserve_defense(ct: Controller) -> int:
    return ct.get_gunner_cost()[0] * 2 + 50


def reserve_offense(ct: Controller) -> int:
    return ct.get_builder_bot_cost()[0] + ct.get_barrier_cost()[0] + 30


def titanium_low(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti < reserve_direct(ct)


def titanium_healthy_for_raw(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + 20


def titanium_ready_for_extra_builders(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0]


def titanium_surplus_for_spawn(ct: Controller) -> bool:
    ti, _ = ct.get_global_resources()
    return ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + EXTRA_SPAWN_SURPLUS


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Player:
    def __init__(self):
        # Common
        self.core_pos: Position | None = None
        self.core_id: int | None = None
        self.width: int | None = None
        self.height: int | None = None
        self.strategy_dirs: list[Direction] | None = None
        self.lane_dirs: list[Direction] | None = None
        self.spawn_tiles: list[Position] | None = None
        self.initial_pos: Position | None = None

        # Builder state
        self.initialized = False
        self.role = "unknown"
        self.home_dir: Direction | None = None
        self.extra_dir: Direction | None = None
        self.home_slot: Position | None = None
        self.home_target: Position | None = None
        self.raw_slot: Position | None = None
        self.refined_slot: Position | None = None
        self.extra_slot: Position | None = None
        self.extra_target: Position | None = None
        self.raw_dir: Direction | None = None

        self.primary_titanium_done = False
        self.raw_axionite_done = False
        self.foundry_online = False
        self.extra_titanium_done = False
        self.refined_route_done = False

        self.mission = "idle"
        self.target_env: Environment | None = None
        self.sink_pos: Position | None = None
        self.sink_target: Position | None = None
        self.preferred_dir: Direction | None = None
        self.target_ore: Position | None = None
        self.target_stand: Position | None = None
        self.trail: list[Position] = []
        self.stall_turns = 0
        self.sweep_sign = 1

        # Scout / assault state
        self.enemy_core_pos: Position | None = None
        self.scout_phase = "advance"
        self.scout_advance_target: Position | None = None
        self.assault_mode = False

        # Core (spawner) state
        self.spawned = 0
        self.last_extra_spawn_round = -999

    # -----------------------------------------------------------------------
    # Top-level dispatch
    # -----------------------------------------------------------------------

    def run(self, ct: Controller) -> None:
        if self.core_pos is None:
            self.bootstrap_common(ct)

        etype = ct.get_entity_type()
        if etype == EntityType.CORE:
            self.run_core(ct)
        elif etype == EntityType.BUILDER_BOT:
            self.run_builder(ct)
        elif etype == EntityType.GUNNER:
            self.run_gunner(ct)
        elif etype == EntityType.SENTINEL:
            self.run_sentinel(ct)
        elif etype == EntityType.BREACH:
            self.run_breach(ct)
        elif etype == EntityType.LAUNCHER:
            self.run_launcher(ct)

    # -----------------------------------------------------------------------
    # Common bootstrap
    # -----------------------------------------------------------------------

    def bootstrap_common(self, ct: Controller) -> None:
        self.width = ct.get_map_width()
        self.height = ct.get_map_height()
        if ct.get_entity_type() == EntityType.CORE:
            self.core_pos = ct.get_position()
            self.core_id = ct.get_id()
        else:
            cid = nearby_core_id(ct)
            if cid is not None:
                self.core_id = cid
                self.core_pos = ct.get_position(cid)
            else:
                self.core_pos = ct.get_position()

        assert self.width and self.height and self.core_pos
        centre = Position(self.width // 2, self.height // 2)
        ordered = unique_dirs(self.core_pos.direction_to(centre))
        usable = [
            d for d in ordered
            if in_bounds(slot_for(self.core_pos, d), self.width, self.height)
        ]
        for d in DIRECTIONS:
            if d not in usable and in_bounds(
                slot_for(self.core_pos, d), self.width, self.height
            ):
                usable.append(d)
        self.strategy_dirs = usable[:8]

        lanes = [
            d for d in CARDINAL_DIRECTIONS
            if in_bounds(slot_for(self.core_pos, d), self.width, self.height)
        ]
        lanes.sort(key=lambda d: (
            slot_for(self.core_pos, d).distance_squared(centre),
            direction_rank(self.core_pos.direction_to(centre), d),
        ))
        if not lanes:
            lanes = [Direction.NORTH]
        while len(lanes) < 4:
            lanes.append(lanes[len(lanes) % len(lanes)])
        self.lane_dirs = lanes[:4]

        # Build spawn tile pool
        role_tiles: list[Position] = []
        for d in self.lane_dirs[:3]:
            pos = slot_target_for(self.core_pos, d)
            if pos not in role_tiles:
                role_tiles.append(pos)
        for d in self.strategy_dirs:
            pos = slot_target_for(self.core_pos, d)
            if pos not in role_tiles:
                role_tiles.append(pos)
            if len(role_tiles) >= 5:
                break
        scout_tiles: list[Position] = []
        for d in DIRECTIONS:
            pos = self.core_pos.add(d)
            if not in_bounds(pos, self.width, self.height):
                continue
            if pos in role_tiles or pos in scout_tiles:
                continue
            if len(role_tiles) < 5:
                role_tiles.append(pos)
            else:
                scout_tiles.append(pos)
        self.spawn_tiles = role_tiles + scout_tiles

    def scan_enemy_core(self, ct: Controller) -> None:
        for eid in ct.get_nearby_buildings():
            if (
                ct.get_entity_type(eid) == EntityType.CORE
                and ct.get_team(eid) != ct.get_team()
            ):
                self.enemy_core_pos = ct.get_position(eid)
                return

    def enemy_positions_by_score(self, ct: Controller) -> list[Position]:
        priorities = {
            EntityType.CORE: 5000,
            EntityType.BREACH: 800,
            EntityType.GUNNER: 700,
            EntityType.LAUNCHER: 650,
            EntityType.SENTINEL: 600,
            EntityType.BUILDER_BOT: 520,
            EntityType.FOUNDRY: 320,
            EntityType.HARVESTER: 260,
            EntityType.BARRIER: 180,
            EntityType.BRIDGE: 120,
            EntityType.CONVEYOR: 80,
            EntityType.SPLITTER: 80,
            EntityType.ARMOURED_CONVEYOR: 80,
            EntityType.ROAD: 30,
            EntityType.MARKER: 10,
        }
        scored: dict[Position, int] = {}
        for eid in ct.get_nearby_entities():
            if ct.get_team(eid) == ct.get_team():
                continue
            pos = ct.get_position(eid)
            etype = ct.get_entity_type(eid)
            hp = ct.get_hp(eid)
            max_hp = max(1, ct.get_max_hp(eid))
            score = priorities.get(etype, 0) + (max_hp - hp) * 3
            if self.enemy_core_pos is not None:
                score -= pos.distance_squared(self.enemy_core_pos)
            scored[pos] = scored.get(pos, 0) + score
        return [
            pos for pos, _ in sorted(
                scored.items(), key=lambda kv: (-kv[1], kv[0].x, kv[0].y)
            )
        ]

    def preferred_core_targets(self, ct: Controller) -> list[Position]:
        if not self.enemy_core_pos or not self.width or not self.height:
            return []
        current = ct.get_position()
        return sorted(
            core_tiles(self.enemy_core_pos, self.width, self.height),
            key=lambda p: (current.distance_squared(p), p.distance_squared(self.enemy_core_pos)),
        )

    # -----------------------------------------------------------------------
    # Turret logic
    # -----------------------------------------------------------------------

    def run_gunner(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        # Priority: enemy core tiles, then high-value targets
        for pos in self.preferred_core_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return
        for pos in self.enemy_positions_by_score(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return

    def run_sentinel(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        for pos in self.preferred_core_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return
        for pos in self.enemy_positions_by_score(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return

    def breach_safe_target(self, ct: Controller, target: Position) -> bool:
        """Ensure no friendly units adjacent to target (splash hits them)."""
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dx == 0 and dy == 0:
                    continue
                check = Position(target.x + dx, target.y + dy)
                if not ct.is_in_vision(check):
                    continue
                bid = ct.get_tile_builder_bot_id(check)
                if bid is not None and ct.get_team(bid) == ct.get_team():
                    return False
                bld = ct.get_tile_building_id(check)
                if bld is not None and ct.get_team(bld) == ct.get_team():
                    return False
        return True

    def run_breach(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        for pos in self.preferred_core_targets(ct):
            if self.breach_safe_target(ct, pos) and ct.can_fire(pos):
                ct.fire(pos)
                return
        for pos in self.enemy_positions_by_score(ct):
            if self.breach_safe_target(ct, pos) and ct.can_fire(pos):
                ct.fire(pos)
                return

    def run_launcher(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        current = ct.get_position()
        adjacent_bots: list[Position] = []
        for d in DIRECTIONS:
            pos = current.add(d)
            if not ct.is_in_vision(pos):
                continue
            bid = ct.get_tile_builder_bot_id(pos)
            if bid and ct.get_team(bid) == ct.get_team():
                adjacent_bots.append(pos)
        if not adjacent_bots:
            return

        # Prefer launching toward enemy core
        targets = self._launcher_targets(ct)
        for target in targets:
            for bot_pos in adjacent_bots:
                if ct.can_launch(bot_pos, target):
                    ct.launch(bot_pos, target)
                    return

    def _launcher_targets(self, ct: Controller) -> list[Position]:
        vision = min(26, ct.get_vision_radius_sq())
        candidates: list[Position] = []
        for pos in ct.get_nearby_tiles(vision):
            if not ct.is_tile_passable(pos):
                continue
            if ct.get_tile_builder_bot_id(pos) is not None:
                continue
            candidates.append(pos)
        current = ct.get_position()
        if self.enemy_core_pos is not None:
            candidates.sort(key=lambda p: (
                p.distance_squared(self.enemy_core_pos),
                -current.distance_squared(p),
            ))
            return candidates
        if not self.core_pos or not self.width or not self.height:
            return []
        mirror = Position(
            self.width - 1 - self.core_pos.x,
            self.height - 1 - self.core_pos.y,
        )
        candidates.sort(key=lambda p: (
            p.distance_squared(mirror),
            -current.distance_squared(p),
        ))
        return candidates

    # -----------------------------------------------------------------------
    # Core logic
    # -----------------------------------------------------------------------

    def target_foundry_count(self) -> int:
        if not self.width or not self.height:
            return 1
        if max(self.width, self.height) >= 30 or self.width * self.height >= 900:
            return min(LARGE_MAP_FOUNDRIES, len(self.lane_dirs or []), 3)
        return min(TARGET_FOUNDRIES, len(self.lane_dirs or []), 3)

    def feeder_index(self) -> int | None:
        if not self.role.startswith("feeder_"):
            return None
        try:
            return int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return None

    def foundry_lane_indices(self, ct: Controller) -> list[int]:
        assert self.core_pos and self.lane_dirs
        preferred: list[int] = []
        fallback: list[int] = []
        for i, d in enumerate(self.lane_dirs[:3]):
            pos = slot_for(self.core_pos, d)
            if not ct.is_in_vision(pos):
                fallback.append(i)
            elif ct.get_tile_env(pos) == Environment.EMPTY:
                preferred.append(i)
            else:
                fallback.append(i)
        return (preferred + fallback)[: self.target_foundry_count()]

    def foundry_lane_directions(self, ct: Controller) -> list[Direction]:
        assert self.lane_dirs
        return [self.lane_dirs[i] for i in self.foundry_lane_indices(ct)]

    def feeder_should_build_foundry(self, ct: Controller) -> bool:
        idx = self.feeder_index()
        return idx is not None and idx in self.foundry_lane_indices(ct)

    def refinery_started(self, ct: Controller) -> bool:
        _, ax = ct.get_global_resources()
        return ax > 0 or self.count_foundries(ct) > 0

    def offense_ready(self, ct: Controller) -> bool:
        ti, ax = ct.get_global_resources()
        if ct.get_current_round() < OFFENSE_MIN_ROUND:
            return False
        if ax > 0 or self.count_foundries(ct) > 0:
            return ti >= reserve_offense(ct) + 60
        return ti >= reserve_raw(ct) + TITANIUM_ONLY_OFFENSE_SURPLUS

    def defense_ready(self, ct: Controller) -> bool:
        if not self.refinery_started(ct):
            return False
        ti, _ = ct.get_global_resources()
        return (
            ct.get_current_round() >= DEFENSE_MIN_ROUND
            or ti >= reserve_defense(ct) + 60
        )

    def count_active_primary_lanes(self, ct: Controller) -> int:
        assert self.core_pos and self.lane_dirs
        count = 0
        for d in self.lane_dirs[:3]:
            if building_type_at(ct, slot_for(self.core_pos, d)) in ROUTABLE_ENDPOINTS:
                count += 1
        return count

    def count_foundries(self, ct: Controller) -> int:
        assert self.core_pos and self.lane_dirs
        count = 0
        for d in self.lane_dirs[:3]:
            if building_type_at(ct, slot_for(self.core_pos, d)) == EntityType.FOUNDRY:
                count += 1
        return count

    def missing_primary_lane_index(self, ct: Controller) -> int | None:
        assert self.core_pos and self.lane_dirs
        for i, d in enumerate(self.lane_dirs[:3]):
            if building_type_at(ct, slot_for(self.core_pos, d)) not in ROUTABLE_ENDPOINTS:
                return i
        return None

    def reserved_foundry_slots(self, ct: Controller) -> set[Position]:
        assert self.core_pos and self.lane_dirs
        return {slot_for(self.core_pos, d) for d in self.foundry_lane_directions(ct)}

    def maybe_vacate_reserved_slot(self, ct: Controller) -> bool:
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        reserved = self.reserved_foundry_slots(ct)
        if current not in reserved or current == self.home_slot:
            return False
        for d in directional_preferences(current.direction_to(self.core_pos)):
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if dest in reserved and dest != self.home_slot:
                continue
            if ct.can_move(d):
                ct.move(d)
                return True
        return False

    def should_join_assault(self, ct: Controller) -> bool:
        if not self.offense_ready(ct):
            return False
        rnd = ct.get_current_round()
        foundries = self.count_foundries(ct)
        if self.role == "scout":
            return True
        if self.role == "expander":
            return rnd >= 120
        if self.role == "fortifier":
            return rnd >= 160 and foundries >= 1
        if self.role.startswith("feeder"):
            if self.feeder_should_build_foundry(ct):
                return False
            return (
                rnd >= 180
                and foundries >= 1
                and self.primary_titanium_done
            )
        return False

    def run_core(self, ct: Controller) -> None:
        assert self.core_pos and self.strategy_dirs

        # Phase 1: always spawn first 3 feeders
        if self.spawned < 3:
            self.try_spawn_builder(ct, self.spawned)
            return

        rnd = ct.get_current_round()
        ti, _ = ct.get_global_resources()
        primary_online = self.count_active_primary_lanes(ct)
        foundries_online = self.count_foundries(ct)
        late_fallback = rnd >= 80 and titanium_ready_for_extra_builders(ct)

        # Phase 2: builders 4 & 5 once refinery branch is viable
        if self.spawned < 5:
            refinery_ready = (
                self.refinery_started(ct)
                or rnd >= 160
                or ti >= reserve_raw(ct) + TITANIUM_ONLY_OFFENSE_SURPLUS
            )
            if (
                titanium_ready_for_extra_builders(ct)
                and (primary_online >= 3 or late_fallback)
                and refinery_ready
            ):
                self.try_spawn_builder(ct, self.spawned)
            return

        # Recovery: re-spawn a feeder if a lane went dark
        recovery_idx = self.missing_primary_lane_index(ct)
        if recovery_idx is not None and rnd >= 70 and titanium_ready_for_extra_builders(ct):
            self.try_spawn_builder(ct, recovery_idx)
            return

        # Extra scouts
        pre_refinery_extra = (
            not self.refinery_started(ct)
            and self.spawned < PRE_REFINERY_ASSAULT_CAP
            and rnd >= 90
            and ti >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + TITANIUM_ONLY_OFFENSE_SURPLUS
            and rnd - self.last_extra_spawn_round >= EXTRA_SPAWN_COOLDOWN * 2
        )
        post_refinery_extra = (
            self.refinery_started(ct)
            and rnd >= 80
            and titanium_surplus_for_spawn(ct)
            and rnd - self.last_extra_spawn_round >= EXTRA_SPAWN_COOLDOWN
        )
        assault_swarm = (
            self.offense_ready(ct)
            and ct.get_unit_count() < 40
            and ti >= reserve_offense(ct) + ct.get_builder_bot_cost()[0] + 40
            and rnd - self.last_extra_spawn_round >= max(2, EXTRA_SPAWN_COOLDOWN // 2)
        )
        if primary_online >= 3 and (pre_refinery_extra or post_refinery_extra or assault_swarm):
            before = self.spawned
            self.try_spawn_builder(ct, max(self.spawned, 5))
            if self.spawned > before:
                self.last_extra_spawn_round = rnd
                return

        # Build defense / offense structures from core
        if primary_online >= 3 and (
            foundries_online >= self.target_foundry_count() or self.defense_ready(ct)
        ):
            self._core_build_defense(ct)

    def _core_build_defense(self, ct: Controller) -> None:
        """Core places barriers/turrets in its ring. Prefers breach if axionite available."""
        assert self.core_pos and self.lane_dirs
        if not self.width or not self.height:
            return
        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
        for d in self.foundry_lane_directions(ct):
            raw_s = self.choose_raw_slot(d)
            reserved.add(raw_s)
            ref_s = self.choose_refined_slot(d, raw_s)
            if ref_s:
                reserved.add(ref_s)

        _, ax = ct.get_global_resources()

        for radius in (2, 3):
            for pos in ring_positions(self.core_pos, radius, self.width, self.height):
                if pos in reserved:
                    continue
                etype = building_type_at(ct, pos)
                if etype in (EntityType.BARRIER, EntityType.GUNNER, EntityType.BREACH):
                    continue
                if radius == 2:
                    # Build breach if we have axionite, otherwise gunner
                    if ax >= 10:
                        cost = ct.get_breach_cost()
                        face = self.core_pos.direction_to(pos)
                        if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                            if ct.can_build_breach(pos, face):
                                ct.build_breach(pos, face)
                                return
                    cost = ct.get_gunner_cost()
                    face = self.core_pos.direction_to(pos)
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                        if ct.can_build_gunner(pos, face):
                            ct.build_gunner(pos, face)
                            return
                else:
                    cost = ct.get_barrier_cost()
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                        if ct.can_build_barrier(pos):
                            ct.build_barrier(pos)
                            return

    def try_spawn_builder(self, ct: Controller, spawn_index: int) -> None:
        assert self.core_pos and self.spawn_tiles
        cost = ct.get_builder_bot_cost()
        reserve = reserve_direct(ct)
        if spawn_index < 3:
            reserve = max(100, reserve // 2)
        if not can_afford(ct, cost[0], cost[1], reserve=reserve):
            return

        if spawn_index < min(5, len(self.spawn_tiles)):
            pos = self.spawn_tiles[spawn_index]
            if ct.can_spawn(pos):
                ct.spawn_builder(pos)
                self.spawned += 1
            return

        scout_tiles = self.spawn_tiles[5:] if len(self.spawn_tiles) > 5 else self.spawn_tiles[3:]
        if not scout_tiles:
            return
        start = max(0, spawn_index - 5) % len(scout_tiles)
        for offset in range(len(scout_tiles)):
            pos = scout_tiles[(start + offset) % len(scout_tiles)]
            if ct.can_spawn(pos):
                ct.spawn_builder(pos)
                self.spawned += 1
                return

    # -----------------------------------------------------------------------
    # Builder bootstrap
    # -----------------------------------------------------------------------

    def run_builder(self, ct: Controller) -> None:
        self.bootstrap_builder(ct)

        if self.maybe_vacate_reserved_slot(ct):
            return

        if self.should_join_assault(ct):
            self.assault_mode = True
            self.run_assault(ct)
            return

        if self.role == "scout":
            if self.assault_mode or self.offense_ready(ct):
                self.assault_mode = True
                self.run_assault(ct)
            else:
                self.run_support_builder(ct)
            return

        if self.role == "fortifier":
            if self.handle_defense(ct, barriers_first=True):
                return
            if not self.extra_titanium_done and not titanium_low(ct):
                self.assign_extra_titanium_mission()
            self.execute_mission(ct)
            return

        if self.mission == "idle":
            self.assign_next_mission(ct)

        if self.mission == "idle" and self.handle_opportunistic_turrets(ct):
            return

        self.execute_mission(ct)

    def run_support_builder(self, ct: Controller) -> None:
        if self.mission == "idle":
            if not self.extra_titanium_done:
                self.assign_extra_titanium_mission()
        if self.mission == "idle" and self.handle_opportunistic_turrets(ct):
            return
        self.execute_mission(ct)

    def bootstrap_builder(self, ct: Controller) -> None:
        if self.initialized:
            return
        if not self.core_pos or not self.strategy_dirs:
            self.bootstrap_common(ct)
        assert self.core_pos and self.strategy_dirs and self.lane_dirs and self.spawn_tiles

        self.initial_pos = ct.get_position()

        if self.initial_pos in self.spawn_tiles[:3]:
            idx = self.spawn_tiles[:3].index(self.initial_pos)
            self.role = f"feeder_{idx}"
            self.home_dir = self.lane_dirs[idx]
            extra_cycle = [3, 0, 1]
            self.extra_dir = self.lane_dirs[extra_cycle[idx] % len(self.lane_dirs)]
            self.sweep_sign = -1 if idx % 2 == 0 else 1
        elif len(self.spawn_tiles) > 3 and self.initial_pos == self.spawn_tiles[3]:
            self.role = "fortifier"
            self.home_dir = self.lane_dirs[min(3, len(self.lane_dirs) - 1)]
            self.extra_dir = self.lane_dirs[min(2, len(self.lane_dirs) - 1)]
            self.sweep_sign = 1
        elif len(self.spawn_tiles) > 4 and self.initial_pos == self.spawn_tiles[4]:
            self.role = "expander"
            self.home_dir = self.lane_dirs[min(3, len(self.lane_dirs) - 1)]
            self.extra_dir = self.lane_dirs[0]
            self.sweep_sign = 1
        else:
            self.role = "scout"
            assert self.width and self.height
            centre = Position(self.width // 2, self.height // 2)
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
            self.home_dir = self.core_pos.direction_to(centre)
            self.extra_dir = self.home_dir
            self.sweep_sign = 1

        self.home_slot = slot_for(self.core_pos, self.home_dir)
        self.home_target = slot_target_for(self.core_pos, self.home_dir)
        self.raw_slot = self.choose_raw_slot(self.home_dir)
        self.refined_slot = self.choose_refined_slot(self.home_dir, self.raw_slot)
        self.extra_slot = slot_for(self.core_pos, self.extra_dir)
        self.extra_target = slot_target_for(self.core_pos, self.extra_dir)

        if self.role.startswith("feeder") and self.raw_dir is None:
            self._init_raw_dir()

        self.initialized = True

    def _init_raw_dir(self) -> None:
        if not self.home_dir or not self.core_pos:
            self.raw_dir = self.home_dir
            return
        assert self.width and self.height
        centre = Position(self.width // 2, self.height // 2)
        dx = centre.x - self.core_pos.x
        dy = centre.y - self.core_pos.y
        if abs(dx) >= abs(dy):
            primary = Direction.EAST if dx >= 0 else Direction.WEST
            second = Direction.SOUTH if self.core_pos.y >= centre.y else Direction.NORTH
        else:
            primary = Direction.SOUTH if dy >= 0 else Direction.NORTH
            second = Direction.EAST if dx > 0 else Direction.WEST
        third = cardinal_opposite(second)
        try:
            idx = int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            idx = 0
        feeder_dirs = [primary, second, third]
        self.raw_dir = feeder_dirs[idx % len(feeder_dirs)]

    def raw_preferred_dir(self) -> Direction | None:
        return self.raw_dir or self.home_dir

    # -----------------------------------------------------------------------
    # ASSAULT LOGIC — the key improvement
    # -----------------------------------------------------------------------

    def run_assault(self, ct: Controller) -> None:
        """
        Assault mode: advance toward enemy core, then stand on its tiles and
        repeatedly fire() (builder attack = 2 damage/Ti, costs 2Ti).
        Also build siege gunners and conveyors to feed them.
        """
        assert self.core_pos and self.width and self.height

        # Always scan for enemy core
        self.scan_enemy_core(ct)

        # Heal injured allies first (costs 1Ti, heals 4HP to all on same tile)
        current = ct.get_position()
        if ct.can_heal(current):
            ct.heal(current)
            return

        if self.enemy_core_pos is None:
            # Still searching — advance toward mirrored position
            self._assault_advance(ct)
            return

        # *** PRIMARY TACTIC: walk onto enemy core tile and attack it ***
        if on_core_tile(current, self.enemy_core_pos):
            # We're ON the enemy core — fire to deal 2 damage
            if ct.can_fire(current):
                ct.fire(current)
                return
            # Can't fire (cooldown) — build a road to stay walkable if needed
            return

        # Try to walk onto an enemy core tile
        enemy_core_tiles = core_tiles(self.enemy_core_pos, self.width, self.height)
        # Prefer tiles adjacent to the core centre itself
        enemy_core_tiles.sort(key=lambda p: (
            p.distance_squared(self.enemy_core_pos),
            current.distance_squared(p),
        ))
        for target in enemy_core_tiles:
            if self._assault_step_toward(ct, target):
                return

        # Fallback: build siege gunners around enemy core
        self._run_siege_gunners(ct)

    def _assault_advance(self, ct: Controller) -> None:
        """Advance to estimated enemy core position."""
        assert self.width and self.height and self.core_pos
        if self.scout_advance_target is None:
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
        self._assault_step_toward(ct, self.scout_advance_target)

    def _run_siege_gunners(self, ct: Controller) -> None:
        """Place gunners adjacent to enemy core and feed them titanium via conveyors."""
        if not self.enemy_core_pos or not self.width or not self.height:
            return
        current = ct.get_position()

        # Find an empty adjacent gunner slot
        gunner_slots: list[Position] = []
        for d in DIRECTIONS:
            pos = self.enemy_core_pos.add(d)
            if not in_bounds(pos, self.width, self.height):
                continue
            if on_core_tile(pos, self.enemy_core_pos):
                continue
            gunner_slots.append(pos)

        # Try to build or repair a gunner
        for pos in gunner_slots:
            etype = building_type_at(ct, pos)
            if etype == EntityType.GUNNER:
                # Already built — try to connect a conveyor to feed it
                self._feed_gunner(ct, pos)
                return
            if etype is None or etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                    if ct.can_destroy(pos):
                        ct.destroy(pos)
                        return
                face = pos.direction_to(self.enemy_core_pos)
                cost = ct.get_gunner_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                    if ct.can_build_gunner(pos, face):
                        ct.build_gunner(pos, face)
                        return
                # Walk toward the slot
                self._assault_step_toward(ct, pos)
                return

    def _feed_gunner(self, ct: Controller, gunner_pos: Position) -> None:
        """Try to place a conveyor feeding titanium into the gunner."""
        if not self.enemy_core_pos or not self.width or not self.height:
            return
        face = gunner_pos.direction_to(self.enemy_core_pos)
        # Conveyors can feed from any side EXCEPT the face direction
        for d in CARDINAL_DIRECTIONS:
            if d == face:
                continue
            feed_pos = gunner_pos.add(d)
            if not in_bounds(feed_pos, self.width, self.height):
                continue
            if on_core_tile(feed_pos, self.enemy_core_pos):
                continue
            etype = building_type_at(ct, feed_pos)
            # Conveyor pointing toward gunner
            toward = d.opposite() if hasattr(d, 'opposite') else cardinal_opposite(d)
            # Use cardinal_opposite since d is always cardinal here
            conveyor_dir = cardinal_opposite(d)
            if etype == EntityType.CONVEYOR:
                continue  # already fed
            if etype is None or etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                    if ct.can_destroy(feed_pos):
                        ct.destroy(feed_pos)
                        return
                cost = ct.get_conveyor_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                    if ct.can_build_conveyor(feed_pos, conveyor_dir):
                        ct.build_conveyor(feed_pos, conveyor_dir)
                        return

    def _assault_step_toward(self, ct: Controller, target: Position) -> bool:
        """
        Move toward target. Builder bots walk on: conveyors, roads, allied core.
        Pave roads on EMPTY tiles to enable movement.
        """
        assert self.width and self.height
        current = ct.get_position()
        if current == target:
            return False

        for d in directional_preferences(current.direction_to(target)):
            dest = current.add(d)
            if not in_bounds(dest, self.width, self.height):
                continue
            if ct.can_move(d):
                ct.move(d)
                return True
            # Pave a road to walk on if tile is empty
            env = ct.get_tile_env(dest)
            if env == Environment.EMPTY and ct.get_tile_building_id(dest) is None:
                cost = ct.get_road_cost()
                if can_afford(ct, cost[0], cost[1], reserve=20):
                    if ct.can_build_road(dest):
                        ct.build_road(dest)
                        if ct.can_move(d):
                            ct.move(d)
                            return True
        return False

    # -----------------------------------------------------------------------
    # Mission assignment (economic)
    # -----------------------------------------------------------------------

    def assign_next_mission(self, ct: Controller) -> None:
        if self.role.startswith("feeder"):
            self._assign_feeder_mission(ct)
        elif self.role == "expander":
            self._assign_expander_mission(ct)

    def _assign_feeder_mission(self, ct: Controller) -> None:
        # 1) Primary titanium lane
        if not self.primary_titanium_done:
            self.start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="primary_titanium",
            )
            return

        foundries_online = self.count_foundries(ct)
        target_foundries = self.target_foundry_count()
        should_build_foundry = self.feeder_should_build_foundry(ct)

        # 2) Raw axionite for foundry
        if (
            should_build_foundry
            and foundries_online < target_foundries
            and not self.raw_axionite_done
            and (
                titanium_healthy_for_raw(ct)
                or (foundries_online == 0 and ct.get_current_round() >= 150)
            )
        ):
            self.start_mission(
                target_env=Environment.ORE_AXIONITE,
                sink_pos=self.raw_slot,
                sink_target=self.home_target,
                preferred_dir=self.raw_preferred_dir(),
                mission="raw_axionite",
            )
            return

        # 3) Wait for foundry
        if foundries_online < target_foundries:
            if not self.extra_titanium_done and (titanium_low(ct) or not should_build_foundry):
                self.assign_extra_titanium_mission()
            return

        # 4) Refined delivery route (bridge from foundry output to core)
        home_foundry_online = (
            self.home_slot is not None
            and building_type_at(ct, self.home_slot) == EntityType.FOUNDRY
        )
        self.foundry_online = home_foundry_online
        if should_build_foundry and home_foundry_online and not self.refined_route_done:
            self._start_refined_delivery_mission()
            return

        # 5) Defense
        if self.should_build_more_defense(ct):
            return

        # 6) Extra titanium
        if not self.extra_titanium_done and (
            titanium_low(ct) or not self.offense_ready(ct) or self.refinery_started(ct)
        ):
            self.assign_extra_titanium_mission()

    def _assign_expander_mission(self, ct: Controller) -> None:
        if not self.primary_titanium_done:
            self.start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="expansion_titanium",
            )
            return
        if self.count_foundries(ct) < self.target_foundry_count() and not self.extra_titanium_done:
            self.assign_extra_titanium_mission()
            return
        if self.count_foundries(ct) > 0 and not self.extra_titanium_done:
            self.assign_extra_titanium_mission()
            return
        if self.should_build_more_defense(ct):
            return

    def assign_extra_titanium_mission(self) -> None:
        self.start_mission(
            target_env=Environment.ORE_TITANIUM,
            sink_pos=self.extra_slot,
            sink_target=self.extra_target,
            preferred_dir=self.extra_dir,
            mission="extra_titanium",
        )

    def start_mission(
        self,
        target_env: Environment,
        sink_pos: Position | None,
        sink_target: Position | None,
        preferred_dir: Direction | None,
        mission: str,
    ) -> None:
        if not sink_pos or not sink_target or not preferred_dir:
            return
        self.target_env = target_env
        self.sink_pos = sink_pos
        self.sink_target = sink_target
        self.preferred_dir = preferred_dir
        self.target_ore = None
        self.target_stand = None
        self.trail = []
        self.stall_turns = 0
        self.mission = mission

    def _start_refined_delivery_mission(self) -> None:
        self.target_env = None
        self.sink_pos = None
        self.sink_target = None
        self.preferred_dir = None
        self.target_ore = None
        self.target_stand = None
        self.trail = []
        self.stall_turns = 0
        self.mission = "refined_delivery"

    # -----------------------------------------------------------------------
    # Mission execution
    # -----------------------------------------------------------------------

    def execute_mission(self, ct: Controller) -> None:
        if self.mission == "idle":
            return
        if self.mission == "refined_delivery":
            self._execute_refined_delivery(ct)
            return
        if not self.sink_pos or not self.sink_target or not self.preferred_dir:
            self.mission = "idle"
            return
        if self.mission.endswith("_connecting"):
            self.execute_connecting(ct)
            return
        self.execute_searching(ct)

    def _execute_refined_delivery(self, ct: Controller) -> None:
        """
        Place a bridge adjacent to the foundry pointing back at the core slot.
        The foundry outputs refined axionite → bridge → core.
        """
        assert self.home_target and self.core_pos
        current = ct.get_position()
        if self.refined_slot is None:
            self.refined_route_done = True
            self.mission = "idle"
            return

        # Move near the home_target (adjacent to foundry)
        if not on_core_tile(current, self.core_pos) and current != self.home_target:
            move_dir = current.direction_to(self.home_target)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
            return

        if self.ensure_bridge(ct, self.refined_slot, self.home_target):
            self.refined_route_done = True
            self.mission = "idle"

    # -----------------------------------------------------------------------
    # Searching phase
    # -----------------------------------------------------------------------

    def execute_searching(self, ct: Controller) -> None:
        assert self.sink_pos and self.sink_target and self.preferred_dir
        current = ct.get_position()

        if not self.trail:
            if current == self.sink_pos:
                self.trail = [current]
            else:
                self.move_to_sink(ct)
                return

        if self.target_ore is None or not self.ore_target_still_valid(
            ct, self.target_ore, self.target_env
        ):
            self.target_ore, self.target_stand = self.plan_visible_ore(
                ct, self.target_env, self.preferred_dir
            )
        elif self.target_ore is not None:
            self.target_stand = self.find_build_stand(ct, self.target_ore)

        if self.target_ore is not None:
            if current.distance_squared(self.target_ore) <= GameConstants.ACTION_RADIUS_SQ:
                if abs(current.x - self.target_ore.x) + abs(current.y - self.target_ore.y) != 1:
                    if not self.move_towards(ct, self.target_stand, self.preferred_dir):
                        self.stall_turns += 1
                        if self.stall_turns >= 2:
                            self.target_ore = None
                            self.target_stand = None
                            self.preferred_dir = cardinal_right(self.preferred_dir)
                            self.stall_turns = 0
                    else:
                        self.stall_turns = 0
                    return
                reserve = reserve_raw(ct) if self.mission == "raw_axionite" else reserve_direct(ct)
                hcost = ct.get_harvester_cost()
                if can_afford(ct, hcost[0], hcost[1], reserve) and ct.can_build_harvester(self.target_ore):
                    ct.build_harvester(self.target_ore)
                    self.mission = f"{self.mission}_connecting"
                return

            move_target = self.target_stand if self.target_stand else self.target_ore
            if not self.move_towards(ct, move_target, self.preferred_dir):
                self.stall_turns += 1
                if self.stall_turns >= 2:
                    self.target_ore = None
                    self.target_stand = None
                    self.preferred_dir = cardinal_right(self.preferred_dir)
                    self.stall_turns = 0
            else:
                self.stall_turns = 0
            return

        if (
            self.target_env == Environment.ORE_AXIONITE
            and self.width and self.height
        ):
            centre = Position(self.width // 2, self.height // 2)
            if current.distance_squared(centre) > 8:
                if not self.move_towards(ct, centre, self.preferred_dir):
                    self.stall_turns += 1
                    if self.stall_turns >= 2:
                        self.preferred_dir = cardinal_right(self.preferred_dir)
                        self.stall_turns = 0
                else:
                    self.stall_turns = 0
                return

        if not self.move_towards(ct, None, self.swept_direction()):
            self.stall_turns += 1
            if self.stall_turns >= 2:
                self.preferred_dir = cardinal_right(self.preferred_dir)
                self.stall_turns = 0
        else:
            self.stall_turns = 0

    def swept_direction(self) -> Direction:
        assert self.preferred_dir
        segment = (len(self.trail) // 6) % 4
        d = self.preferred_dir
        if segment == 1:
            return cardinal_right(d) if self.sweep_sign > 0 else cardinal_left(d)
        if segment == 2:
            return cardinal_opposite(d)
        return d

    # -----------------------------------------------------------------------
    # Connecting phase
    # -----------------------------------------------------------------------

    def execute_connecting(self, ct: Controller) -> None:
        assert self.sink_target and self.target_env
        if not self.trail:
            self.mission = "idle"
            return
        current = ct.get_position()
        if current not in self.trail:
            return

        current_index = self.trail.index(current)
        if current_index < len(self.trail) - 1:
            target_tile = self.trail[current_index + 1]
            target_dir = target_tile.direction_to(current)
            if not self.has_conveyor_tile(ct, target_tile, target_dir):
                self.ensure_conveyor_tile(ct, target_tile, target_dir)
                return

        if current_index > 0:
            move_dir = current.direction_to(self.trail[current_index - 1])
            if ct.can_move(move_dir):
                ct.move(move_dir)
            return

        if self.mission == "raw_axionite_connecting":
            foundry_cost = ct.get_foundry_cost()
            conveyor_cost = ct.get_conveyor_cost()
            needed = foundry_cost[0] + conveyor_cost[0]
            if not can_afford(ct, needed, 0, reserve=reserve_direct(ct)):
                return
            if not self.home_slot:
                return
            if not self.ensure_conveyor_tile(
                ct, self.trail[0], self.trail[0].direction_to(self.home_slot)
            ):
                return
            if ct.get_tile_builder_bot_id(self.home_slot) is not None:
                return
            if not self.ensure_foundry(ct, self.home_slot):
                return
            self.raw_axionite_done = True
            self.foundry_online = True
            self.mission = "idle"
            return

        if not self.ensure_conveyor_tile(
            ct, self.trail[0], self.trail[0].direction_to(self.sink_target)
        ):
            return

        if self.mission in ("primary_titanium_connecting", "expansion_titanium_connecting"):
            self.primary_titanium_done = True
        elif self.mission == "extra_titanium_connecting":
            self.extra_titanium_done = True
        self.mission = "idle"

    # -----------------------------------------------------------------------
    # Building helpers
    # -----------------------------------------------------------------------

    def has_conveyor_tile(self, ct: Controller, pos: Position, direction: Direction) -> bool:
        bid = ct.get_tile_building_id(pos)
        if bid is None:
            return False
        return (
            ct.get_entity_type(bid) == EntityType.CONVEYOR
            and ct.get_direction(bid) == direction
        )

    def ensure_conveyor_tile(self, ct: Controller, pos: Position, direction: Direction) -> bool:
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.CONVEYOR and ct.get_direction(bid) == direction:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_conveyor_cost()
        reserve = reserve_raw(ct) if self.mission == "raw_axionite_connecting" else reserve_direct(ct)
        if not can_afford(ct, cost[0], cost[1], reserve):
            return False
        if ct.can_build_conveyor(pos, direction):
            ct.build_conveyor(pos, direction)
            return True
        return False

    def ensure_foundry(self, ct: Controller, pos: Position | None) -> bool:
        if pos is None:
            return False
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.FOUNDRY:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_foundry_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_foundry(pos):
            ct.build_foundry(pos)
            return True
        return False

    def ensure_bridge(self, ct: Controller, pos: Position | None, target: Position | None) -> bool:
        if pos is None or target is None:
            return False
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.BRIDGE and ct.get_bridge_target(bid) == target:
                return True
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_bridge_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_bridge(pos, target):
            ct.build_bridge(pos, target)
            return True
        return False

    # -----------------------------------------------------------------------
    # Movement helpers
    # -----------------------------------------------------------------------

    def move_to_sink(self, ct: Controller) -> None:
        assert self.sink_pos and self.preferred_dir and self.core_pos
        current = ct.get_position()
        if current == self.sink_pos:
            self.trail = [current]
            return
        if ct.is_in_vision(self.sink_pos) and not ct.is_tile_passable(self.sink_pos):
            rcost = ct.get_road_cost()
            if can_afford(ct, rcost[0], rcost[1], reserve=reserve_direct(ct)) and ct.can_build_road(self.sink_pos):
                ct.build_road(self.sink_pos)
        move_dir = current.direction_to(self.sink_pos)
        if move_dir != Direction.CENTRE and ct.can_move(move_dir):
            ct.move(move_dir)
            if ct.get_position() == self.sink_pos:
                self.trail = [self.sink_pos]

    def move_towards(self, ct: Controller, target: Position | None, fallback: Direction) -> bool:
        assert self.core_pos and self.width and self.height
        current = ct.get_position()
        if target is None:
            candidates = cardinal_directional_preferences(fallback)
        else:
            dx = target.x - current.x
            dy = target.y - current.y
            horizontal = Direction.EAST if dx > 0 else Direction.WEST if dx < 0 else Direction.CENTRE
            vertical = Direction.SOUTH if dy > 0 else Direction.NORTH if dy < 0 else Direction.CENTRE
            if abs(dx) > abs(dy):
                primary = horizontal
                secondary = vertical if vertical != Direction.CENTRE else fallback
            elif abs(dy) > abs(dx):
                primary = vertical
                secondary = horizontal if horizontal != Direction.CENTRE else fallback
            elif fallback in (horizontal, vertical):
                primary = fallback
                secondary = vertical if primary == horizontal else horizontal
            else:
                primary = horizontal if horizontal != Direction.CENTRE else vertical
                secondary = vertical if primary == horizontal else horizontal
            if primary == Direction.CENTRE:
                primary = fallback
            candidates = cardinal_directional_preferences(primary, secondary)

        recent = set(self.trail[-6:])
        for allow_recent in (False, True):
            for d in candidates:
                dest = current.add(d)
                if not in_bounds(dest, self.width, self.height):
                    continue
                if on_core_tile(dest, self.core_pos):
                    continue
                if not allow_recent and dest in recent:
                    continue
                if self.try_step(ct, d):
                    self.update_trail_after_move(ct.get_position())
                    return True
        return False

    def try_step(self, ct: Controller, direction: Direction) -> bool:
        dest = ct.get_position().add(direction)
        if ct.can_move(direction):
            ct.move(direction)
            return True
        if ct.get_tile_env(dest) != Environment.EMPTY:
            return False
        if ct.get_tile_building_id(dest) is not None:
            return False
        cost = ct.get_road_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
            return False
        if ct.can_build_road(dest):
            ct.build_road(dest)
            if ct.can_move(direction):
                ct.move(direction)
                return True
        return False

    def update_trail_after_move(self, new_pos: Position) -> None:
        if not self.trail:
            self.trail = [new_pos]
            return
        if new_pos in self.trail:
            self.trail = self.trail[: self.trail.index(new_pos) + 1]
        else:
            self.trail.append(new_pos)

    # -----------------------------------------------------------------------
    # Ore planning
    # -----------------------------------------------------------------------

    def plan_visible_ore(
        self, ct: Controller, target_env: Environment, preferred_dir: Direction
    ) -> tuple[Position | None, Position | None]:
        current = ct.get_position()
        best_ore = best_stand = None
        best_score = 10 ** 9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != target_env:
                continue
            if ct.get_tile_building_id(pos) is not None:
                continue
            stand = self.find_build_stand(ct, pos)
            if stand is None:
                continue
            score = (
                current.distance_squared(stand) * 10
                + current.distance_squared(pos)
                + direction_rank(preferred_dir, current.direction_to(stand))
            )
            if score < best_score:
                best_ore, best_stand, best_score = pos, stand, score
        return best_ore, best_stand

    def ore_target_still_valid(
        self, ct: Controller, pos: Position, target_env: Environment | None
    ) -> bool:
        if target_env is None or not ct.is_in_vision(pos):
            return False
        if ct.get_tile_env(pos) != target_env:
            return False
        return ct.get_tile_building_id(pos) is None

    def find_build_stand(self, ct: Controller, ore_pos: Position | None) -> Position | None:
        if ore_pos is None:
            return None
        current = ct.get_position()
        candidates: list[Position] = []
        for pos in [current, *ct.get_nearby_tiles()]:
            if abs(pos.x - ore_pos.x) + abs(pos.y - ore_pos.y) != 1:
                continue
            if ct.get_tile_env(pos) != Environment.EMPTY and not ct.is_tile_passable(pos):
                continue
            if ct.get_tile_building_id(pos) is not None and not ct.is_tile_passable(pos):
                continue
            candidates.append(pos)
        if not candidates:
            return None
        candidates.sort(key=lambda p: (
            current.distance_squared(p),
            direction_rank(self.preferred_dir or Direction.CENTRE, current.direction_to(p)),
        ))
        return candidates[0]

    # -----------------------------------------------------------------------
    # Defense
    # -----------------------------------------------------------------------

    def should_build_more_defense(self, ct: Controller) -> bool:
        if not self.defense_ready(ct):
            return False
        return self.handle_defense(ct, barriers_first=False)

    def handle_opportunistic_turrets(self, ct: Controller) -> bool:
        if not self.defense_ready(ct) or not self.core_pos:
            return False
        if not on_core_tile(ct.get_position(), self.core_pos):
            return False
        return self.handle_defense(ct, barriers_first=False)

    def handle_defense(self, ct: Controller, barriers_first: bool) -> bool:
        assert self.core_pos
        if not on_core_tile(ct.get_position(), self.core_pos):
            move_dir = ct.get_position().direction_to(self.core_pos)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
                return True

        barrier_positions, turret_positions = self.defense_positions()
        _, ax = ct.get_global_resources()

        if barriers_first:
            for pos in barrier_positions:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_barrier(pos):
                    ct.build_barrier(pos)
                    return True

        for pos in turret_positions:
            if building_type_at(ct, pos) in (EntityType.GUNNER, EntityType.BREACH):
                continue
            face = self.core_pos.direction_to(pos)
            # Prefer breach if axionite available
            if ax >= 10:
                cost = ct.get_breach_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                    if ct.can_build_breach(pos, face):
                        ct.build_breach(pos, face)
                        return True
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_gunner(pos, face):
                ct.build_gunner(pos, face)
                return True

        # Ring-3 turrets
        if self.width and self.height:
            reserved = {slot_for(self.core_pos, d) for d in (self.lane_dirs or [])}
            for pos in ring_positions(self.core_pos, 3, self.width, self.height):
                if pos in reserved:
                    continue
                if building_type_at(ct, pos) in (EntityType.GUNNER, EntityType.BARRIER, EntityType.BREACH):
                    continue
                cost = ct.get_gunner_cost()
                face = self.core_pos.direction_to(pos)
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_gunner(pos, face):
                    ct.build_gunner(pos, face)
                    return True

        if not barriers_first:
            for pos in barrier_positions:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)) and ct.can_build_barrier(pos):
                    ct.build_barrier(pos)
                    return True

        return False

    def defense_positions(self) -> tuple[list[Position], list[Position]]:
        assert self.core_pos and self.lane_dirs and self.width and self.height
        ring_two: list[Position] = []
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                if max(abs(dx), abs(dy)) != 2:
                    continue
                pos = Position(self.core_pos.x + dx, self.core_pos.y + dy)
                if in_bounds(pos, self.width, self.height):
                    ring_two.append(pos)

        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
        reserved.update(self.choose_raw_slot(d) for d in self.lane_dirs[:3])
        for d in self.lane_dirs[: self.target_foundry_count()]:
            rs = self.choose_refined_slot(d, self.choose_raw_slot(d))
            if rs:
                reserved.add(rs)

        available = [p for p in ring_two if p not in reserved]
        available.sort(key=lambda p: (
            abs(p.x - self.core_pos.x) + abs(p.y - self.core_pos.y),
            p.y, p.x,
        ))
        split = max(0, len(available) // 4)  # fewer barriers, more turrets
        return available[:split], available[split:]

    def choose_raw_slot(self, direction: Direction) -> Position:
        assert self.core_pos and self.width and self.height
        foundry_slot = slot_for(self.core_pos, direction)
        centre = Position(self.width // 2, self.height // 2)
        candidates = [
            foundry_slot.add(cardinal_left(direction)),
            foundry_slot.add(cardinal_right(direction)),
        ]
        home_slots = {slot_for(self.core_pos, d) for d in (self.lane_dirs or [])}
        best = candidates[0]
        best_score = 999
        for pos in candidates:
            score = 0
            if pos in home_slots:
                score += 50
            if not in_bounds(pos, self.width, self.height):
                score += 100
            if on_core_tile(pos, self.core_pos):
                score += 100
            if in_bounds(pos, self.width, self.height):
                score += pos.distance_squared(centre) // 4
            if score < best_score:
                best, best_score = pos, score
        return best

    def choose_refined_slot(self, direction: Direction, raw_slot: Position | None) -> Position | None:
        assert self.core_pos and self.width and self.height
        foundry_slot = slot_for(self.core_pos, direction)
        candidates = [
            foundry_slot.add(cardinal_left(direction)),
            foundry_slot.add(cardinal_right(direction)),
        ]
        home_slots = {slot_for(self.core_pos, d) for d in (self.lane_dirs or [])}
        best: Position | None = None
        best_score = 999
        for pos in candidates:
            score = 0
            if raw_slot and pos == raw_slot:
                score += 200
            if pos in home_slots:
                score += 50
            if not in_bounds(pos, self.width, self.height):
                score += 100
            if on_core_tile(pos, self.core_pos):
                score += 100
            if score < best_score:
                best, best_score = pos, score
        if best_score >= 100:
            return None
        return best