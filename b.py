from __future__ import annotations

"""
Improved Cambridge Battlecode bot.

Key improvements over the original:
  - Dynamic builder spawning: no hardcap at 5; spawns more scouts/builders
    whenever titanium exceeds a generous surplus threshold.
  - Scout role: dedicated builder bots that explore toward the enemy core,
    then place offensive turrets (gunners) adjacent to it.
  - Refined-axionite delivery: after foundries are online, expander/feeder
    bots route refined axionite conveyors back to the core storage.
  - Layered core defence: ring-2 barriers first, then ring-2 turrets facing
    outward, then ring-3 turrets on any remaining vacant slots; re-checks
    every turn so new slots are filled as resources allow.
  - Dead scouts are implicitly replaced because the core continues spawning
    whenever the titanium surplus allows.
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
# Directional helpers
# ---------------------------------------------------------------------------

DIRECTIONS = [d for d in Direction if d != Direction.CENTRE]
CARDINAL_DIRECTIONS = [
    Direction.NORTH,
    Direction.EAST,
    Direction.SOUTH,
    Direction.WEST,
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

# Economic / production tuning.
TARGET_FOUNDRIES = 2
EXTRA_SPAWN_SURPLUS = 80
EXTRA_SPAWN_COOLDOWN = 6
OFFENSE_MIN_ROUND = 80
DEFENSE_MIN_ROUND = 140
TITANIUM_ONLY_OFFENSE_SURPLUS = 160
PRE_REFINERY_ASSAULT_CAP = 18


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

    def add(direction: Direction) -> None:
        if direction != Direction.CENTRE and direction not in ordered:
            ordered.append(direction)

    add(primary)
    left = primary
    right = primary
    for _ in range(3):
        left = left.rotate_left()
        right = right.rotate_right()
        add(left)
        add(right)
    add(primary.opposite())
    for direction in DIRECTIONS:
        add(direction)
    return ordered[:8]


def cardinal_left(direction: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.WEST,
        Direction.EAST: Direction.NORTH,
        Direction.SOUTH: Direction.EAST,
        Direction.WEST: Direction.SOUTH,
    }.get(direction, Direction.NORTH)


def cardinal_right(direction: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.EAST,
        Direction.EAST: Direction.SOUTH,
        Direction.SOUTH: Direction.WEST,
        Direction.WEST: Direction.NORTH,
    }.get(direction, Direction.EAST)


def cardinal_opposite(direction: Direction) -> Direction:
    return {
        Direction.NORTH: Direction.SOUTH,
        Direction.EAST: Direction.WEST,
        Direction.SOUTH: Direction.NORTH,
        Direction.WEST: Direction.EAST,
    }.get(direction, Direction.SOUTH)


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

    def add(direction: Direction) -> None:
        if direction in CARDINAL_DIRECTIONS and direction not in ordered:
            ordered.append(direction)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        if base in CARDINAL_DIRECTIONS:
            add(base)
            add(cardinal_left(base))
            add(cardinal_right(base))
            continue
        for direction in diagonal_map.get(base, ()):
            add(direction)
    for direction in CARDINAL_DIRECTIONS:
        add(direction)
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
    for entity_id in ct.get_nearby_buildings():
        if ct.get_entity_type(entity_id) != EntityType.CORE:
            continue
        if ct.get_team(entity_id) == ct.get_team():
            return entity_id
    return None


def building_type_at(ct: Controller, pos: Position) -> EntityType | None:
    if not ct.is_in_vision(pos):
        return None
    building_id = ct.get_tile_building_id(pos)
    if building_id is None:
        return None
    return ct.get_entity_type(building_id)


def can_afford(
    ct: Controller,
    titanium_cost: int,
    axionite_cost: int = 0,
    reserve: int = 0,
) -> bool:
    titanium, axionite = ct.get_global_resources()
    return titanium - titanium_cost >= reserve and axionite >= axionite_cost


# ---------------------------------------------------------------------------
# Reserve helpers – how much titanium to keep in the bank before spending
# ---------------------------------------------------------------------------

def reserve_direct(ct: Controller) -> int:
    return ct.get_harvester_cost()[0] + ct.get_conveyor_cost()[0] * 8 + 40


def reserve_raw(ct: Controller) -> int:
    return (
        ct.get_foundry_cost()[0]
        + ct.get_harvester_cost()[0]
        + ct.get_conveyor_cost()[0] * 8
        + 80
    )


def reserve_defense(ct: Controller) -> int:
    return ct.get_gunner_cost()[0] * 3 + 80


def titanium_low(ct: Controller) -> bool:
    titanium, _ = ct.get_global_resources()
    return titanium < reserve_direct(ct)


def titanium_healthy_for_raw(ct: Controller) -> bool:
    titanium, _ = ct.get_global_resources()
    return titanium >= reserve_raw(ct) + 30


def titanium_ready_for_extra_builders(ct: Controller) -> bool:
    titanium, _ = ct.get_global_resources()
    return titanium >= reserve_raw(ct) + ct.get_builder_bot_cost()[0]


def titanium_surplus_for_spawn(ct: Controller) -> bool:
    """True when we have a comfortable surplus beyond all reserves."""
    titanium, _ = ct.get_global_resources()
    return titanium >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + EXTRA_SPAWN_SURPLUS


def reserve_offense(ct: Controller) -> int:
    return ct.get_builder_bot_cost()[0] + ct.get_barrier_cost()[0] * 2 + 40


def direction_rank(preferred: Direction, candidate: Direction) -> int:
    if preferred == Direction.CENTRE:
        return 0
    prefs = directional_preferences(preferred)
    try:
        return prefs.index(candidate)
    except ValueError:
        return len(prefs)


def directional_preferences(
    primary: Direction, secondary: Direction | None = None
) -> list[Direction]:
    ordered: list[Direction] = []

    def add(direction: Direction) -> None:
        if direction != Direction.CENTRE and direction not in ordered:
            ordered.append(direction)

    for base in (primary, secondary):
        if base is None or base == Direction.CENTRE:
            continue
        add(base)
        left = base
        right = base
        for _ in range(3):
            left = left.rotate_left()
            right = right.rotate_right()
            add(left)
            add(right)
    for direction in DIRECTIONS:
        add(direction)
    return ordered


# ---------------------------------------------------------------------------
# Main Player class
# ---------------------------------------------------------------------------

class Player:
    def __init__(self):
        # --- shared / common state ---
        self.core_pos: Position | None = None
        self.core_id: int | None = None
        self.width: int | None = None
        self.height: int | None = None
        self.strategy_dirs: list[Direction] | None = None
        self.lane_dirs: list[Direction] | None = None
        self.spawn_tiles: list[Position] | None = None
        self.initial_pos: Position | None = None

        # --- builder state ---
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
        self.refined_route_done = False  # NEW: refined axionite conveyor to core

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

        # --- scout state ---
        self.enemy_core_pos: Position | None = None
        self.scout_phase = "advance"   # "advance" | "attack"
        self.scout_advance_target: Position | None = None
        self.assault_mode = False

        # --- core (spawner) state ---
        self.spawned = 0
        self.last_extra_spawn_round = -999

    # -----------------------------------------------------------------------
    # Top-level dispatch
    # -----------------------------------------------------------------------

    def run(self, ct: Controller) -> None:
        if self.core_pos is None:
            self.bootstrap_common(ct)

        entity_type = ct.get_entity_type()
        if entity_type == EntityType.CORE:
            self.run_core(ct)
        elif entity_type == EntityType.BUILDER_BOT:
            self.run_builder(ct)
        elif entity_type == EntityType.GUNNER:
            self.run_gunner(ct)
        elif entity_type == EntityType.SENTINEL:
            self.run_sentinel(ct)
        elif entity_type == EntityType.BREACH:
            self.run_breach(ct)
        elif entity_type == EntityType.LAUNCHER:
            self.run_launcher(ct)

    # -----------------------------------------------------------------------
    # Common bootstrap (shared by core and builders)
    # -----------------------------------------------------------------------

    def bootstrap_common(self, ct: Controller) -> None:
        self.width = ct.get_map_width()
        self.height = ct.get_map_height()
        if ct.get_entity_type() == EntityType.CORE:
            self.core_pos = ct.get_position()
            self.core_id = ct.get_id()
        else:
            core_id = nearby_core_id(ct)
            if core_id is not None:
                self.core_id = core_id
                self.core_pos = ct.get_position(core_id)
            else:
                self.core_pos = ct.get_position()

        assert (
            self.width is not None
            and self.height is not None
            and self.core_pos is not None
        )
        centre = Position(self.width // 2, self.height // 2)
        ordered = unique_dirs(self.core_pos.direction_to(centre))
        usable = [
            d
            for d in ordered
            if in_bounds(slot_for(self.core_pos, d), self.width, self.height)
        ]
        for direction in DIRECTIONS:
            if direction not in usable and in_bounds(
                slot_for(self.core_pos, direction), self.width, self.height
            ):
                usable.append(direction)
        self.strategy_dirs = usable[:8]

        lanes = [
            d
            for d in CARDINAL_DIRECTIONS
            if in_bounds(slot_for(self.core_pos, d), self.width, self.height)
        ]
        lanes.sort(
            key=lambda d: (
                slot_for(self.core_pos, d).distance_squared(centre),
                direction_rank(self.core_pos.direction_to(centre), d),
            )
        )
        if not lanes:
            lanes = [Direction.NORTH]
        while len(lanes) < 4:
            lanes.append(lanes[len(lanes) % len(lanes)])
        self.lane_dirs = lanes[:4]

        # Build a large spawn-tile pool: feeder slots first, then every
        # adjacent tile around the core.  No cap — the spawner cycles through
        # all of them so there is always a free slot for a new scout.
        role_tiles: list[Position] = []
        for direction in self.lane_dirs[:3]:
            pos = slot_target_for(self.core_pos, direction)
            if pos not in role_tiles:
                role_tiles.append(pos)
        for direction in self.strategy_dirs:
            pos = slot_target_for(self.core_pos, direction)
            if pos not in role_tiles:
                role_tiles.append(pos)
            if len(role_tiles) >= 5:
                break
        scout_tiles: list[Position] = []
        for direction in DIRECTIONS:
            pos = self.core_pos.add(direction)
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
        for entity_id in ct.get_nearby_buildings():
            if ct.get_entity_type(entity_id) != EntityType.CORE:
                continue
            if ct.get_team(entity_id) == ct.get_team():
                continue
            self.enemy_core_pos = ct.get_position(entity_id)
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
        for entity_id in ct.get_nearby_entities():
            if ct.get_team(entity_id) == ct.get_team():
                continue
            pos = ct.get_position(entity_id)
            entity_type = ct.get_entity_type(entity_id)
            hp = ct.get_hp(entity_id)
            max_hp = max(1, ct.get_max_hp(entity_id))
            score = priorities.get(entity_type, 0)
            score += (max_hp - hp) * 3
            if self.enemy_core_pos is not None:
                score -= pos.distance_squared(self.enemy_core_pos)
            scored[pos] = scored.get(pos, 0) + score
        return [
            pos
            for pos, _ in sorted(
                scored.items(),
                key=lambda item: (-item[1], item[0].x, item[0].y),
            )
        ]

    def preferred_core_targets(self, ct: Controller) -> list[Position]:
        if (
            self.enemy_core_pos is None
            or self.width is None
            or self.height is None
        ):
            return []
        current = ct.get_position()
        return sorted(
            core_tiles(self.enemy_core_pos, self.width, self.height),
            key=lambda pos: (
                current.distance_squared(pos),
                pos.distance_squared(self.enemy_core_pos),
            ),
        )

    def prime_siege_targets(self, ct: Controller) -> list[Position]:
        if (
            self.enemy_core_pos is None
            or self.width is None
            or self.height is None
        ):
            return []
        candidates = [
            Position(self.enemy_core_pos.x, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y),
            Position(self.enemy_core_pos.x, self.enemy_core_pos.y + 2),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y + 2),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y + 2),
        ]
        current = ct.get_position()
        targets: list[Position] = []
        for pos in candidates:
            if not in_bounds(pos, self.width, self.height):
                continue
            if not ct.is_in_vision(pos):
                continue
            building_id = ct.get_tile_building_id(pos)
            if building_id is None or ct.get_team(building_id) == ct.get_team():
                continue
            targets.append(pos)
        return sorted(targets, key=lambda pos: current.distance_squared(pos))

    def run_gunner(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        for pos in self.prime_siege_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return
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
        for pos in self.prime_siege_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return
        for pos in self.preferred_core_targets(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return
        for pos in self.enemy_positions_by_score(ct):
            if ct.can_fire(pos):
                ct.fire(pos)
                return

    def breach_safe_target(self, ct: Controller, target: Position) -> bool:
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dx == 0 and dy == 0:
                    continue
                check = Position(target.x + dx, target.y + dy)
                if not ct.is_in_vision(check):
                    continue
                builder_id = ct.get_tile_builder_bot_id(check)
                if builder_id is not None and ct.get_team(builder_id) == ct.get_team():
                    return False
                building_id = ct.get_tile_building_id(check)
                if building_id is not None and ct.get_team(building_id) == ct.get_team():
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

    def launcher_targets(self, ct: Controller) -> list[Position]:
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
            candidates.sort(
                key=lambda pos: (
                    pos.distance_squared(self.enemy_core_pos),
                    -current.distance_squared(pos),
                )
            )
            return candidates
        if (
            self.core_pos is None
            or self.width is None
            or self.height is None
            or nearby_core_id(ct) is None
        ):
            return []
        mirror = Position(
            self.width - 1 - self.core_pos.x,
            self.height - 1 - self.core_pos.y,
        )
        candidates.sort(
            key=lambda pos: (
                pos.distance_squared(mirror),
                -current.distance_squared(pos),
            )
        )
        return candidates

    def run_launcher(self, ct: Controller) -> None:
        self.scan_enemy_core(ct)
        adjacent_bots: list[Position] = []
        current = ct.get_position()
        for direction in DIRECTIONS:
            pos = current.add(direction)
            if not ct.is_in_vision(pos):
                continue
            bot_id = ct.get_tile_builder_bot_id(pos)
            if bot_id is None or ct.get_team(bot_id) != ct.get_team():
                continue
            adjacent_bots.append(pos)
        if not adjacent_bots:
            return

        targets = self.launcher_targets(ct)
        for target in targets:
            for bot_pos in adjacent_bots:
                if ct.can_launch(bot_pos, target):
                    ct.launch(bot_pos, target)
                    return

    def target_foundry_count(self) -> int:
        target = 1
        if self.width is not None and self.height is not None:
            if max(self.width, self.height) >= 28 or self.width * self.height >= 850:
                target = TARGET_FOUNDRIES
        return min(target, len(self.lane_dirs or []), 3)

    def feeder_index(self) -> int | None:
        if not self.role.startswith("feeder_"):
            return None
        try:
            return int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return None

    def foundry_lane_indices(self, ct: Controller) -> list[int]:
        assert self.core_pos is not None and self.lane_dirs is not None
        preferred: list[int] = []
        fallback: list[int] = []
        for index, direction in enumerate(self.lane_dirs[:3]):
            pos = slot_for(self.core_pos, direction)
            if not ct.is_in_vision(pos):
                fallback.append(index)
            elif ct.get_tile_env(pos) == Environment.EMPTY:
                preferred.append(index)
            else:
                fallback.append(index)
        ordered = preferred + fallback
        return ordered[: self.target_foundry_count()]

    def foundry_lane_directions(self, ct: Controller) -> list[Direction]:
        assert self.lane_dirs is not None
        return [self.lane_dirs[index] for index in self.foundry_lane_indices(ct)]

    def feeder_should_build_foundry(self, ct: Controller) -> bool:
        index = self.feeder_index()
        return index is not None and index in self.foundry_lane_indices(ct)

    def defense_ready(self, ct: Controller) -> bool:
        if not self.refinery_started(ct):
            return False
        titanium, _ = ct.get_global_resources()
        return (
            ct.get_current_round() >= DEFENSE_MIN_ROUND
            or titanium >= reserve_defense(ct) + 80
        )

    def offense_ready(self, ct: Controller) -> bool:
        titanium, axionite = ct.get_global_resources()
        if ct.get_current_round() < OFFENSE_MIN_ROUND:
            return False
        if axionite > 0 or self.count_foundries(ct) > 0:
            return titanium >= reserve_offense(ct) + 80
        return titanium >= reserve_raw(ct) + TITANIUM_ONLY_OFFENSE_SURPLUS

    def missing_primary_lane_index(self, ct: Controller) -> int | None:
        assert self.core_pos is not None and self.lane_dirs is not None
        for index, direction in enumerate(self.lane_dirs[:3]):
            pos = slot_for(self.core_pos, direction)
            if building_type_at(ct, pos) not in ROUTABLE_ENDPOINTS:
                return index
        return None

    def reserved_foundry_slots(self, ct: Controller) -> set[Position]:
        assert self.core_pos is not None and self.lane_dirs is not None
        return {
            slot_for(self.core_pos, direction)
            for direction in self.foundry_lane_directions(ct)
        }

    def maybe_vacate_reserved_slot(self, ct: Controller) -> bool:
        assert self.core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()
        reserved = self.reserved_foundry_slots(ct)
        if current not in reserved or current == self.home_slot:
            return False
        for direction in directional_preferences(current.direction_to(self.core_pos)):
            dest = current.add(direction)
            if not in_bounds(dest, self.width, self.height):
                continue
            if dest in reserved and dest != self.home_slot:
                continue
            if ct.can_move(direction):
                ct.move(direction)
                return True
        return False

    def should_join_assault(self, ct: Controller) -> bool:
        if not self.offense_ready(ct):
            return False
        current_round = ct.get_current_round()
        foundries_online = self.count_foundries(ct)
        if self.role == "scout":
            return True
        if self.role == "expander":
            return current_round >= 140
        if self.role == "fortifier":
            return current_round >= 190 and foundries_online >= 1
        if self.role.startswith("feeder"):
            if self.feeder_should_build_foundry(ct):
                return False
            return (
                current_round >= 200
                and foundries_online >= 1
                and self.primary_titanium_done
            )
        return False

    # -----------------------------------------------------------------------
    # Core logic
    # -----------------------------------------------------------------------

    def run_core(self, ct: Controller) -> None:
        assert self.core_pos is not None and self.strategy_dirs is not None

        # Spawn phase 1: first 3 feeders (unconditional once we can afford them)
        if self.spawned < 3:
            self.try_spawn_builder(ct, self.spawned)
            return

        current_round = ct.get_current_round()
        titanium, _ = ct.get_global_resources()
        primary_online = self.count_active_primary_lanes(ct)
        foundries_online = self.count_foundries(ct)
        late_fallback = current_round >= 80 and titanium_ready_for_extra_builders(ct)

        # Spawn phase 2: hold back builders 4 & 5 until the refinery branch is
        # online, so the opening really is "3 titanium feeders first".
        if self.spawned < 5:
            refinery_expansion_ready = (
                self.refinery_started(ct)
                or current_round >= 180
                or titanium >= reserve_raw(ct) + TITANIUM_ONLY_OFFENSE_SURPLUS
            )
            if (
                titanium_ready_for_extra_builders(ct)
                and (primary_online >= 3 or late_fallback)
                and refinery_expansion_ready
            ):
                self.try_spawn_builder(ct, self.spawned)
            return

        recovery_index = self.missing_primary_lane_index(ct)
        if (
            recovery_index is not None
            and current_round >= 70
            and titanium_ready_for_extra_builders(ct)
        ):
            self.try_spawn_builder(ct, recovery_index)
            return

        pre_refinery_extra = (
            not self.refinery_started(ct)
            and self.spawned < PRE_REFINERY_ASSAULT_CAP
            and current_round >= 110
            and titanium
            >= reserve_raw(ct) + ct.get_builder_bot_cost()[0] + TITANIUM_ONLY_OFFENSE_SURPLUS
            and current_round - self.last_extra_spawn_round
            >= EXTRA_SPAWN_COOLDOWN * 2
        )
        post_refinery_extra = (
            self.refinery_started(ct)
            and current_round >= 90
            and titanium_surplus_for_spawn(ct)
            and current_round - self.last_extra_spawn_round >= EXTRA_SPAWN_COOLDOWN
        )
        assault_swarm = (
            self.offense_ready(ct)
            and ct.get_unit_count() < 36
            and titanium >= reserve_offense(ct) + ct.get_builder_bot_cost()[0] + 60
            and current_round - self.last_extra_spawn_round >= max(3, EXTRA_SPAWN_COOLDOWN // 2)
        )
        if primary_online >= 3 and (pre_refinery_extra or post_refinery_extra or assault_swarm):
            before = self.spawned
            self.try_spawn_builder(ct, max(self.spawned, 5))
            if self.spawned > before:
                self.last_extra_spawn_round = current_round
                return

        if not self.offense_ready(ct):
            if primary_online >= 3 and (
                foundries_online >= self.target_foundry_count()
                or self.defense_ready(ct)
            ):
                self._core_build_defense(ct)
            return

        if primary_online >= 3 and (
            foundries_online >= self.target_foundry_count()
            or self.defense_ready(ct)
        ):
            self._core_build_defense(ct)

    def _core_build_defense(self, ct: Controller) -> None:
        """Core itself places barriers/turrets in its immediate ring when affordable."""
        assert self.core_pos is not None and self.lane_dirs is not None
        if self.width is None or self.height is None:
            return
        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
        for direction in self.foundry_lane_directions(ct):
            raw_slot = self.choose_raw_slot(direction)
            reserved.add(raw_slot)
            refined_slot = self.choose_refined_slot(direction, raw_slot)
            if refined_slot is not None:
                reserved.add(refined_slot)
        for radius in (2, 3):
            for pos in ring_positions(self.core_pos, radius, self.width, self.height):
                if pos in reserved:
                    continue
                etype = building_type_at(ct, pos)
                if etype in (EntityType.BARRIER, EntityType.GUNNER):
                    continue
                # Prefer barriers on ring-2, turrets on ring-3
                if radius == 2:
                    cost = ct.get_barrier_cost()
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                        if ct.can_build_barrier(pos):
                            ct.build_barrier(pos)
                            return
                else:
                    cost = ct.get_gunner_cost()
                    face = self.core_pos.direction_to(pos)
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct)):
                        if ct.can_build_gunner(pos, face):
                            ct.build_gunner(pos, face)
                            return

    def try_spawn_builder(self, ct: Controller, spawn_index: int) -> None:
        assert self.core_pos is not None and self.spawn_tiles is not None
        cost = ct.get_builder_bot_cost()
        reserve = reserve_direct(ct)
        if spawn_index < 3:
            reserve = max(120, reserve // 2)
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
            self.run_scout(ct)
            return

        # Scout role has its own execution path
        if self.role == "scout":
            if self.assault_mode or self.offense_ready(ct):
                self.assault_mode = True
                self.run_scout(ct)
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
            elif self.should_build_more_defense(ct):
                return

        if self.mission == "idle" and self.handle_opportunistic_turrets(ct):
            return

        self.execute_mission(ct)

    def bootstrap_builder(self, ct: Controller) -> None:
        if self.initialized:
            return

        if self.core_pos is None or self.strategy_dirs is None:
            self.bootstrap_common(ct)

        assert (
            self.core_pos is not None
            and self.strategy_dirs is not None
            and self.lane_dirs is not None
            and self.spawn_tiles is not None
        )

        self.initial_pos = ct.get_position()

        if self.initial_pos in self.spawn_tiles[:3]:
            index = self.spawn_tiles[:3].index(self.initial_pos)
            self.role = f"feeder_{index}"
            self.home_dir = self.lane_dirs[index]
            extra_cycle = [3, 0, 1]
            self.extra_dir = self.lane_dirs[extra_cycle[index] % len(self.lane_dirs)]
            self.sweep_sign = -1 if index % 2 == 0 else 1
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
            # Extra bots (spawned beyond index 4) become scouts
            self.role = "scout"
            # Pick a direction toward the map centre / enemy side
            assert self.width is not None and self.height is not None
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

        # Determine raw_dir for each feeder
        if self.role.startswith("feeder") and self.raw_dir is None:
            self._init_raw_dir()

        self.initialized = True

    def _init_raw_dir(self) -> None:
        if self.home_dir is None or self.core_pos is None:
            self.raw_dir = self.home_dir
            return
        assert self.width is not None and self.height is not None
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
            index = int(self.role.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            index = 0
        feeder_dirs = [primary, second, third]
        self.raw_dir = feeder_dirs[index % len(feeder_dirs)]

    def raw_preferred_dir(self) -> Direction | None:
        return self.raw_dir or self.home_dir

    # -----------------------------------------------------------------------
    # Scout logic
    # -----------------------------------------------------------------------

    def run_scout(self, ct: Controller) -> None:
        """
        Assault builders advance toward the enemy core, establish a blocking
        perimeter, and then add inward-facing gunners on valid siege tiles.
        """
        assert self.core_pos is not None and self.width is not None and self.height is not None

        # Scan all visible buildings for the enemy core.
        if self.enemy_core_pos is None:
            for eid in ct.get_nearby_buildings():
                if (
                    ct.get_entity_type(eid) == EntityType.CORE
                    and ct.get_team(eid) != ct.get_team()
                ):
                    self.enemy_core_pos = ct.get_position(eid)
                    self.scout_phase = "attack"
                    break

        if self.scout_phase == "attack" and self.enemy_core_pos is not None:
            self.run_siege(ct)
        else:
            self._scout_advance(ct)

    def _scout_advance(self, ct: Controller) -> None:
        """Walk straight toward the mirrored enemy-core position."""
        assert self.width is not None and self.height is not None and self.core_pos is not None
        if self.scout_advance_target is None:
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
        self._assault_step_toward(ct, self.scout_advance_target)

    def siege_bridge_site(
        self, ct: Controller, ore_pos: Position, gunner_pos: Position
    ) -> Position | None:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        candidates = [
            ore_pos.add(Direction.NORTH),
            ore_pos.add(Direction.EAST),
            ore_pos.add(Direction.SOUTH),
            ore_pos.add(Direction.WEST),
        ]
        best: Position | None = None
        best_score = 10**9
        for pos in candidates:
            if not in_bounds(pos, self.width, self.height):
                continue
            if on_core_tile(pos, self.enemy_core_pos) or pos == gunner_pos:
                continue
            if pos.distance_squared(gunner_pos) > 9:
                continue
            score = pos.distance_squared(gunner_pos) * 5 + pos.distance_squared(self.enemy_core_pos)
            if score < best_score:
                best = pos
                best_score = score
        return best

    def siege_battery_support(
        self, ct: Controller, gunner_pos: Position
    ) -> tuple[Position, Position] | None:
        assert self.enemy_core_pos is not None
        best: tuple[Position, Position] | None = None
        best_score = 10**9
        for ore_pos in ct.get_nearby_tiles():
            if ct.get_tile_env(ore_pos) != Environment.ORE_TITANIUM:
                continue
            if on_core_tile(ore_pos, self.enemy_core_pos):
                continue
            ore_type = building_type_at(ct, ore_pos)
            if ore_type not in (None, EntityType.HARVESTER):
                continue
            bridge_pos = self.siege_bridge_site(ct, ore_pos, gunner_pos)
            if bridge_pos is None:
                continue
            score = (
                ore_pos.distance_squared(gunner_pos) * 2
                + bridge_pos.distance_squared(gunner_pos) * 5
                + ore_pos.distance_squared(self.enemy_core_pos)
            )
            if score < best_score:
                best = (ore_pos, bridge_pos)
                best_score = score
        return best

    def siege_batteries(
        self, ct: Controller, gunner_tiles: list[Position]
    ) -> list[tuple[Position, Position, Position]]:
        batteries: list[tuple[Position, Position, Position]] = []
        for gunner_pos in gunner_tiles:
            support = self.siege_battery_support(ct, gunner_pos)
            if support is None:
                continue
            ore_pos, bridge_pos = support
            batteries.append((gunner_pos, ore_pos, bridge_pos))
        batteries.sort(
            key=lambda item: (
                item[0].distance_squared(self.enemy_core_pos),
                item[1].distance_squared(self.enemy_core_pos),
                item[2].distance_squared(self.enemy_core_pos),
            )
        )
        return batteries

    def has_bridge_target(
        self, ct: Controller, pos: Position, target: Position
    ) -> bool:
        building_id = ct.get_tile_building_id(pos)
        if building_id is None or ct.get_entity_type(building_id) != EntityType.BRIDGE:
            return False
        return ct.get_bridge_target(building_id) == target

    def gunner_feed_tiles(self, gunner_pos: Position) -> list[Position]:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        face = gunner_pos.direction_to(self.enemy_core_pos)
        tiles: list[Position] = []
        for direction in CARDINAL_DIRECTIONS:
            if face in CARDINAL_DIRECTIONS and direction == face:
                continue
            pos = gunner_pos.add(direction)
            if not in_bounds(pos, self.width, self.height):
                continue
            if on_core_tile(pos, self.enemy_core_pos):
                continue
            if pos not in tiles:
                tiles.append(pos)
        return tiles

    def siege_supply_tile_clear(self, ct: Controller, pos: Position) -> bool:
        assert self.enemy_core_pos is not None
        if not ct.is_in_vision(pos):
            return False
        if ct.get_tile_env(pos) != Environment.EMPTY:
            return False
        if on_core_tile(pos, self.enemy_core_pos):
            return False
        etype = building_type_at(ct, pos)
        return etype is None or etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER

    def cardinal_paths(self, start: Position, end: Position) -> list[list[Position]]:
        def build_path(x_first: bool) -> list[Position]:
            path = [start]
            current = start
            if x_first:
                while current.x != end.x:
                    step_x = 1 if end.x > current.x else -1
                    current = Position(current.x + step_x, current.y)
                    path.append(current)
                while current.y != end.y:
                    step_y = 1 if end.y > current.y else -1
                    current = Position(current.x, current.y + step_y)
                    path.append(current)
            else:
                while current.y != end.y:
                    step_y = 1 if end.y > current.y else -1
                    current = Position(current.x, current.y + step_y)
                    path.append(current)
                while current.x != end.x:
                    step_x = 1 if end.x > current.x else -1
                    current = Position(current.x + step_x, current.y)
                    path.append(current)
            return path

        options = [build_path(True), build_path(False)]
        unique: list[list[Position]] = []
        for path in options:
            if path not in unique:
                unique.append(path)
        return unique

    def siege_conveyor_support(
        self,
        ct: Controller,
        gunner_pos: Position,
        used_ores: set[Position],
        used_tiles: set[Position],
    ) -> tuple[Position, list[Position]] | None:
        assert self.enemy_core_pos is not None
        best: tuple[Position, list[Position]] | None = None
        best_score = 10**9
        feed_tiles = self.gunner_feed_tiles(gunner_pos)
        for ore_pos in ct.get_nearby_tiles():
            if ore_pos in used_ores:
                continue
            if ct.get_tile_env(ore_pos) != Environment.ORE_TITANIUM:
                continue
            if on_core_tile(ore_pos, self.enemy_core_pos):
                continue
            ore_type = building_type_at(ct, ore_pos)
            if ore_type not in (None, EntityType.HARVESTER):
                continue
            entries = [
                ore_pos.add(Direction.NORTH),
                ore_pos.add(Direction.EAST),
                ore_pos.add(Direction.SOUTH),
                ore_pos.add(Direction.WEST),
            ]
            for entry in entries:
                if not self.siege_supply_tile_clear(ct, entry):
                    continue
                for feed in feed_tiles:
                    if not self.siege_supply_tile_clear(ct, feed):
                        continue
                    for path in self.cardinal_paths(entry, feed):
                        if any(tile in used_tiles for tile in path):
                            continue
                        if all(self.siege_supply_tile_clear(ct, tile) for tile in path):
                            score = (
                                len(path) * 8
                                + ore_pos.distance_squared(gunner_pos) * 2
                                + ore_pos.distance_squared(self.enemy_core_pos)
                            )
                            if score < best_score:
                                best = (ore_pos, path)
                                best_score = score
        return best

    def siege_supply_routes(
        self,
        ct: Controller,
        gunner_tiles: list[Position],
        reserved_ores: set[Position],
    ) -> list[tuple[Position, Position, list[Position]]]:
        routes: list[tuple[Position, Position, list[Position]]] = []
        used_ores = set(reserved_ores)
        used_tiles: set[Position] = set()
        for gunner_pos in gunner_tiles:
            support = self.siege_conveyor_support(ct, gunner_pos, used_ores, used_tiles)
            if support is None:
                continue
            ore_pos, path = support
            routes.append((gunner_pos, ore_pos, path))
            used_ores.add(ore_pos)
            used_tiles.update(path)
        routes.sort(
            key=lambda item: (
                item[0].distance_squared(self.enemy_core_pos),
                len(item[2]),
                item[1].distance_squared(self.enemy_core_pos),
            )
        )
        return routes

    def run_siege(self, ct: Controller) -> None:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()
        barrier_tiles, gunner_tiles = self.siege_positions()
        current_building = ct.get_tile_building_id(current)
        if (
            current in set(barrier_tiles + gunner_tiles)
            and current_building is not None
            and ct.get_team(current_building) != ct.get_team()
            and ct.can_fire(current)
        ):
            ct.fire(current)
            return

        heal_priorities = {
            EntityType.BREACH: 5,
            EntityType.GUNNER: 4,
            EntityType.LAUNCHER: 3,
            EntityType.SENTINEL: 3,
            EntityType.BARRIER: 2,
            EntityType.BUILDER_BOT: 1,
        }
        heal_targets: list[tuple[int, Position]] = []
        for pos in [current, *ct.get_nearby_tiles(GameConstants.ACTION_RADIUS_SQ)]:
            if not ct.can_heal(pos):
                continue
            priority = 0
            building_id = ct.get_tile_building_id(pos)
            if building_id is not None and ct.get_team(building_id) == ct.get_team():
                priority = max(priority, heal_priorities.get(ct.get_entity_type(building_id), 0))
            builder_id = ct.get_tile_builder_bot_id(pos)
            if builder_id is not None and ct.get_team(builder_id) == ct.get_team():
                priority = max(priority, heal_priorities.get(ct.get_entity_type(builder_id), 0))
            if priority > 0:
                heal_targets.append((priority, pos))
        if heal_targets:
            heal_targets.sort(key=lambda item: (-item[0], current.distance_squared(item[1])))
            ct.heal(heal_targets[0][1])
            return

        def buildable_target(pos: Position, desired: EntityType) -> bool:
            etype = building_type_at(ct, pos)
            if etype == desired:
                return False
            return etype is None or etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER

        batteries = self.siege_batteries(ct, gunner_tiles)
        routes = self.siege_supply_routes(
            ct,
            [pos for pos in gunner_tiles if pos not in {item[0] for item in batteries}],
            {item[1] for item in batteries},
        )
        pressure_targets: list[Position] = []

        for pos, ore, _ in batteries[:1]:
            ore_type = building_type_at(ct, ore)
            if ore_type == EntityType.HARVESTER:
                continue
            if ore_type in WALKABLE_BUILDINGS or ore_type == EntityType.MARKER:
                if ct.can_destroy(ore):
                    ct.destroy(ore)
                    return
                pressure_targets.append(ore)
                continue
            cost = ct.get_harvester_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                if ct.can_build_harvester(ore):
                    ct.build_harvester(ore)
                    return
            pressure_targets.append(ore)

        for pos, _, bridge_pos in batteries[:1]:
            etype = building_type_at(ct, pos)
            if etype != EntityType.GUNNER:
                if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                    if ct.can_destroy(pos):
                        ct.destroy(pos)
                        return
                    pressure_targets.append(pos)
                    continue
                if etype is not None:
                    continue
                face = pos.direction_to(self.enemy_core_pos)
                cost = ct.get_gunner_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                    if ct.can_build_gunner(pos, face):
                        ct.build_gunner(pos, face)
                        return
                pressure_targets.append(pos)

            bridge_type = building_type_at(ct, bridge_pos)
            if bridge_type == EntityType.BRIDGE and self.has_bridge_target(ct, bridge_pos, pos):
                continue
            if bridge_type in WALKABLE_BUILDINGS or bridge_type == EntityType.MARKER:
                if ct.can_destroy(bridge_pos):
                    ct.destroy(bridge_pos)
                    return
                pressure_targets.append(bridge_pos)
            elif bridge_type is not None:
                continue
            bridge_cost = ct.get_bridge_cost()
            if can_afford(ct, bridge_cost[0], bridge_cost[1], reserve=reserve_offense(ct)):
                if ct.can_build_bridge(bridge_pos, pos):
                    ct.build_bridge(bridge_pos, pos)
                    return
            pressure_targets.append(bridge_pos)

        for gunner_pos, ore, path in routes[:1]:
            ore_type = building_type_at(ct, ore)
            if ore_type != EntityType.HARVESTER:
                if ore_type in WALKABLE_BUILDINGS or ore_type == EntityType.MARKER:
                    if ct.can_destroy(ore):
                        ct.destroy(ore)
                        return
                    pressure_targets.append(ore)
                    continue
                if ore_type is None:
                    cost = ct.get_harvester_cost()
                    if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                        if ct.can_build_harvester(ore):
                            ct.build_harvester(ore)
                            return
                    pressure_targets.append(ore)
                continue

            route_ready = True
            for index, tile in enumerate(path):
                target = gunner_pos if index == len(path) - 1 else path[index + 1]
                direction = tile.direction_to(target)
                if self.has_conveyor_tile(ct, tile, direction):
                    continue
                etype = building_type_at(ct, tile)
                if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                    if ct.can_destroy(tile):
                        ct.destroy(tile)
                        return
                    pressure_targets.append(tile)
                    route_ready = False
                    break
                if etype is not None:
                    route_ready = False
                    break
                cost = ct.get_conveyor_cost()
                if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                    if ct.can_build_conveyor(tile, direction):
                        ct.build_conveyor(tile, direction)
                        return
                pressure_targets.append(tile)
                route_ready = False
                break

            if not route_ready:
                continue

            gunner_type = building_type_at(ct, gunner_pos)
            if gunner_type == EntityType.GUNNER:
                continue
            if gunner_type in WALKABLE_BUILDINGS or gunner_type == EntityType.MARKER:
                if ct.can_destroy(gunner_pos):
                    ct.destroy(gunner_pos)
                    return
                pressure_targets.append(gunner_pos)
                continue
            if gunner_type is not None:
                continue
            face = gunner_pos.direction_to(self.enemy_core_pos)
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                if ct.can_build_gunner(gunner_pos, face):
                    ct.build_gunner(gunner_pos, face)
                    return
            pressure_targets.append(gunner_pos)

        for pos in gunner_tiles:
            etype = building_type_at(ct, pos)
            if etype == EntityType.GUNNER:
                continue
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return
                pressure_targets.append(pos)
                continue
            face = pos.direction_to(self.enemy_core_pos)
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct)):
                if ct.can_build_gunner(pos, face):
                    ct.build_gunner(pos, face)
                    return
            pressure_targets.append(pos)

        if pressure_targets:
            target = min(pressure_targets, key=lambda p: current.distance_squared(p))
            self._assault_step_toward(ct, target)
            return

        built_gunners = sum(
            1 for pos in gunner_tiles if building_type_at(ct, pos) == EntityType.GUNNER
        )

        for pos in barrier_tiles:
            etype = building_type_at(ct, pos)
            if etype == EntityType.BARRIER:
                continue
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return
                continue
            cost = ct.get_barrier_cost()
            if (
                built_gunners >= max(4, len(gunner_tiles) // 2)
                and can_afford(ct, cost[0], cost[1], reserve=reserve_offense(ct))
            ):
                if ct.can_build_barrier(pos):
                    ct.build_barrier(pos)
                    return

        open_barriers = [
            pos
            for pos in barrier_tiles
            if buildable_target(pos, EntityType.BARRIER)
        ]
        if open_barriers:
            target = min(open_barriers, key=lambda p: current.distance_squared(p))
            self._assault_step_toward(ct, target)
            return

        open_gunners = [
            pos
            for pos in gunner_tiles
            if buildable_target(pos, EntityType.GUNNER)
        ]
        if open_gunners:
            target = min(open_gunners, key=lambda p: current.distance_squared(p))
            self._assault_step_toward(ct, target)

    def siege_positions(self) -> tuple[list[Position], list[Position]]:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None

        gunner_candidates: list[Position] = []
        for direction in CARDINAL_DIRECTIONS:
            for distance in (2, 3, 4):
                gunner_candidates.append(step(self.enemy_core_pos, direction, distance))
        for direction in (
            Direction.NORTHEAST,
            Direction.SOUTHEAST,
            Direction.SOUTHWEST,
            Direction.NORTHWEST,
        ):
            for distance in (2, 3):
                gunner_candidates.append(step(self.enemy_core_pos, direction, distance))

        barrier_candidates = [
            Position(self.enemy_core_pos.x - 4, self.enemy_core_pos.y - 1),
            Position(self.enemy_core_pos.x - 4, self.enemy_core_pos.y + 1),
            Position(self.enemy_core_pos.x + 4, self.enemy_core_pos.y - 1),
            Position(self.enemy_core_pos.x + 4, self.enemy_core_pos.y + 1),
            Position(self.enemy_core_pos.x - 1, self.enemy_core_pos.y - 4),
            Position(self.enemy_core_pos.x + 1, self.enemy_core_pos.y - 4),
            Position(self.enemy_core_pos.x - 1, self.enemy_core_pos.y + 4),
            Position(self.enemy_core_pos.x + 1, self.enemy_core_pos.y + 4),
            Position(self.enemy_core_pos.x - 3, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x - 3, self.enemy_core_pos.y + 2),
            Position(self.enemy_core_pos.x + 3, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x + 3, self.enemy_core_pos.y + 2),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y - 3),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y - 3),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y + 3),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y + 3),
        ]

        def clean(positions: list[Position]) -> list[Position]:
            unique: list[Position] = []
            for pos in positions:
                if not in_bounds(pos, self.width, self.height):
                    continue
                if on_core_tile(pos, self.enemy_core_pos):
                    continue
                if pos not in unique:
                    unique.append(pos)
            return unique

        return clean(barrier_candidates), clean(gunner_candidates)

    def _assault_step_toward(self, ct: Controller, target: Position) -> bool:
        assert self.width is not None and self.height is not None
        current = ct.get_position()
        if current == target:
            return False

        for direction in directional_preferences(current.direction_to(target)):
            dest = current.add(direction)
            if not in_bounds(dest, self.width, self.height):
                continue
            if ct.can_move(direction):
                ct.move(direction)
                return True
            if (
                ct.get_tile_env(dest) == Environment.EMPTY
                and ct.get_tile_building_id(dest) is None
            ):
                cost = ct.get_road_cost()
                if can_afford(ct, cost[0], cost[1], reserve=ct.get_barrier_cost()[0]):
                    if ct.can_build_road(dest):
                        ct.build_road(dest)
                        if ct.can_move(direction):
                            ct.move(direction)
                            return True
        return False

    def _scout_attack(self, ct: Controller) -> None:
        """
        Find the best empty attack tile adjacent to the enemy core, move
        next to it, and build a gunner facing the core.  Scouts also fill
        ring-2 tiles once ring-1 is saturated.

        Key design decisions:
          - We look for *empty* slots (no gunner yet) so multiple scouts
            spread out rather than all clustering on the same tile.
          - We check can_build_gunner first; if we can build right now we
            do so immediately without moving.
          - Gunners face the enemy core so every shot hits it.
          - We skip the tile the enemy core itself occupies.
        """
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()

        # Collect all candidate attack positions sorted rings outward.
        # ring-1 (directly adjacent) first, then ring-2.
        attack_tiles: list[Position] = []
        for radius in (1, 2, 3):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    pos = Position(
                        self.enemy_core_pos.x + dx,
                        self.enemy_core_pos.y + dy,
                    )
                    if in_bounds(pos, self.width, self.height):
                        attack_tiles.append(pos)

        # 1) Try to build a gunner on any tile we are already standing next to.
        for pos in attack_tiles:
            if pos == self.enemy_core_pos:
                continue
            if building_type_at(ct, pos) == EntityType.GUNNER:
                continue
            face = pos.direction_to(self.enemy_core_pos)
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=0):
                if ct.can_build_gunner(pos, face):
                    ct.build_gunner(pos, face)
                    return

        # 2) Find the nearest empty attack tile and walk toward it.
        empty_tiles = [
            p for p in attack_tiles
            if p != self.enemy_core_pos
            and building_type_at(ct, p) != EntityType.GUNNER
        ]
        if not empty_tiles:
            # All slots filled — stay put or nudge away to free up pathways.
            return

        target = min(empty_tiles, key=lambda p: current.distance_squared(p))
        self._scout_step_toward(ct, target)

    def _scout_step_toward(self, ct: Controller, target: Position) -> bool:
        """
        Simple greedy step toward *target* using all 4 cardinal directions.
        Unlike move_towards() this does NOT skip core tiles (scouts need to
        walk off the core at the start) and does NOT maintain a trail.
        Returns True if a move was made.
        """
        assert self.width is not None and self.height is not None
        current = ct.get_position()
        if current == target:
            return False

        dx = target.x - current.x
        dy = target.y - current.y

        # Build preference list: best cardinal first, then perpendiculars.
        prefs: list[Direction] = []
        if abs(dx) >= abs(dy):
            prefs.append(Direction.EAST if dx > 0 else Direction.WEST)
            if dy != 0:
                prefs.append(Direction.SOUTH if dy > 0 else Direction.NORTH)
        else:
            prefs.append(Direction.SOUTH if dy > 0 else Direction.NORTH)
            if dx != 0:
                prefs.append(Direction.EAST if dx > 0 else Direction.WEST)
        # Add remaining cardinals as fallback
        for d in CARDINAL_DIRECTIONS:
            if d not in prefs:
                prefs.append(d)

        for direction in prefs:
            dest = current.add(direction)
            if not in_bounds(dest, self.width, self.height):
                continue
            if ct.can_move(direction):
                ct.move(direction)
                return True
            # Try paving a road on empty passable terrain
            if (
                ct.get_tile_env(dest) == Environment.EMPTY
                and ct.get_tile_building_id(dest) is None
            ):
                cost = ct.get_road_cost()
                if can_afford(ct, cost[0], cost[1], reserve=0):
                    if ct.can_build_road(dest):
                        ct.build_road(dest)
                        if ct.can_move(direction):
                            ct.move(direction)
                            return True
        return False

    # -----------------------------------------------------------------------
    # Mission assignment
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

        # 2) Only the early dedicated feeders convert into foundries.
        if (
            should_build_foundry
            and foundries_online < target_foundries
            and not self.raw_axionite_done
            and (
                titanium_healthy_for_raw(ct)
                or (foundries_online == 0 and ct.get_current_round() >= 170)
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

        # 3) Keep the remaining feeder on titanium while the refinery package
        # is still being assembled.
        if foundries_online < target_foundries:
            if not self.extra_titanium_done and titanium_low(ct):
                self.assign_extra_titanium_mission()
            elif not should_build_foundry and not self.extra_titanium_done:
                self.assign_extra_titanium_mission()
            return

        # 4) Once the foundry is online, add a bridge for refined output.
        home_foundry_online = (
            self.home_slot is not None
            and building_type_at(ct, self.home_slot) == EntityType.FOUNDRY
        )
        self.foundry_online = home_foundry_online
        if should_build_foundry and home_foundry_online and not self.refined_route_done:
            self._start_refined_delivery_mission(ct)
            return

        # 5) Defense
        if self.should_build_more_defense(ct):
            return

        # 6) Extra titanium lane
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

    # -----------------------------------------------------------------------
    # Refined axionite delivery (NEW)
    # -----------------------------------------------------------------------

    def _start_refined_delivery_mission(self, ct: Controller) -> None:
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
        if sink_pos is None or sink_target is None or preferred_dir is None:
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

    def execute_mission(self, ct: Controller) -> None:
        if self.mission == "idle":
            return
        if self.sink_pos is None or self.sink_target is None or self.preferred_dir is None:
            self.mission = "idle"
            return

        if self.mission == "refined_delivery":
            self._execute_refined_delivery(ct)
            return

        if self.mission.endswith("_connecting"):
            self.execute_connecting(ct)
            return

        self.execute_searching(ct)

    # -----------------------------------------------------------------------
    # Refined delivery execution (NEW)
    # -----------------------------------------------------------------------

    def _execute_refined_delivery(self, ct: Controller) -> None:
        """
        Build a bridge next to the foundry so refined axionite can hop
        straight back into the core.
        """
        assert self.home_target is not None and self.core_pos is not None

        current = ct.get_position()
        if self.refined_slot is None:
            self.refined_route_done = True
            self.mission = "idle"
            return

        if not on_core_tile(current, self.core_pos):
            move_dir = current.direction_to(self.core_pos)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
            return

        if current != self.home_target:
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
        assert (
            self.sink_pos is not None
            and self.sink_target is not None
            and self.preferred_dir is not None
        )
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

                harvester_cost = ct.get_harvester_cost()
                reserve = (
                    reserve_raw(ct)
                    if self.mission == "raw_axionite"
                    else reserve_direct(ct)
                )
                if (
                    can_afford(ct, harvester_cost[0], harvester_cost[1], reserve)
                    and ct.can_build_harvester(self.target_ore)
                ):
                    ct.build_harvester(self.target_ore)
                    self.mission = f"{self.mission}_connecting"
                    return
                return

            move_target = (
                self.target_stand if self.target_stand is not None else self.target_ore
            )
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
            and self.width is not None
            and self.height is not None
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
        assert self.preferred_dir is not None
        segment = (len(self.trail) // 6) % 4
        direction = self.preferred_dir
        if segment == 1:
            return cardinal_right(direction) if self.sweep_sign > 0 else cardinal_left(direction)
        if segment == 2:
            return cardinal_opposite(direction)
        return direction

    # -----------------------------------------------------------------------
    # Connecting phase
    # -----------------------------------------------------------------------

    def execute_connecting(self, ct: Controller) -> None:
        assert self.sink_target is not None and self.target_env is not None
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
                if not self.ensure_conveyor_tile(ct, target_tile, target_dir):
                    return
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
            if self.home_slot is None:
                return
            if not self.ensure_conveyor_tile(
                ct,
                self.trail[0],
                self.trail[0].direction_to(self.home_slot),
            ):
                return
            home_builder = ct.get_tile_builder_bot_id(self.home_slot)
            if home_builder is not None:
                return
            # The foundry sits on the titanium lane so that titanium can
            # continue feeding directly into it.
            if not self.ensure_foundry(ct, self.home_slot):
                return
            self.raw_axionite_done = True
            self.foundry_online = True
            self.mission = "idle"
            return

        if not self.ensure_conveyor_tile(
            ct,
            self.trail[0],
            self.trail[0].direction_to(self.sink_target),
        ):
            return

        if self.mission == "primary_titanium_connecting":
            self.primary_titanium_done = True
        elif self.mission == "expansion_titanium_connecting":
            self.primary_titanium_done = True
        elif self.mission == "extra_titanium_connecting":
            self.extra_titanium_done = True

        self.mission = "idle"

    # -----------------------------------------------------------------------
    # Building helpers
    # -----------------------------------------------------------------------

    def has_conveyor_tile(
        self, ct: Controller, pos: Position, direction: Direction
    ) -> bool:
        building_id = ct.get_tile_building_id(pos)
        if building_id is None:
            return False
        return (
            ct.get_entity_type(building_id) == EntityType.CONVEYOR
            and ct.get_direction(building_id) == direction
        )

    def ensure_conveyor_tile(
        self, ct: Controller, pos: Position, direction: Direction
    ) -> bool:
        building_id = ct.get_tile_building_id(pos)
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if (
                entity_type == EntityType.CONVEYOR
                and ct.get_direction(building_id) == direction
            ):
                return True
            if entity_type in WALKABLE_BUILDINGS or entity_type == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
            else:
                return False
        cost = ct.get_conveyor_cost()
        reserve = (
            reserve_raw(ct)
            if self.mission == "raw_axionite_connecting"
            else reserve_direct(ct)
        )
        if not can_afford(ct, cost[0], cost[1], reserve):
            return False
        if ct.can_build_conveyor(pos, direction):
            ct.build_conveyor(pos, direction)
            return True
        return False

    def ensure_foundry(self, ct: Controller, pos: Position | None) -> bool:
        if pos is None:
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.FOUNDRY:
                return True
            if entity_type in WALKABLE_BUILDINGS or entity_type == EntityType.MARKER:
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

    def ensure_bridge(
        self, ct: Controller, pos: Position | None, target: Position | None
    ) -> bool:
        if pos is None or target is None:
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if entity_type == EntityType.BRIDGE and ct.get_bridge_target(building_id) == target:
                return True
            if entity_type in WALKABLE_BUILDINGS or entity_type == EntityType.MARKER:
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
    # Movement
    # -----------------------------------------------------------------------

    def move_to_sink(self, ct: Controller) -> None:
        assert (
            self.sink_pos is not None
            and self.preferred_dir is not None
            and self.core_pos is not None
        )
        current = ct.get_position()
        if current == self.sink_pos:
            self.trail = [current]
            return

        if ct.is_in_vision(self.sink_pos) and not ct.is_tile_passable(self.sink_pos):
            road_cost = ct.get_road_cost()
            if (
                can_afford(ct, road_cost[0], road_cost[1], reserve=reserve_direct(ct))
                and ct.can_build_road(self.sink_pos)
            ):
                ct.build_road(self.sink_pos)

        move_dir = current.direction_to(self.sink_pos)
        if move_dir != Direction.CENTRE and ct.can_move(move_dir):
            ct.move(move_dir)
            if ct.get_position() == self.sink_pos:
                self.trail = [self.sink_pos]

    def move_towards(
        self, ct: Controller, target: Position | None, fallback: Direction
    ) -> bool:
        assert (
            self.core_pos is not None
            and self.width is not None
            and self.height is not None
        )
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
            for direction in candidates:
                dest = current.add(direction)
                if not in_bounds(dest, self.width, self.height):
                    continue
                if on_core_tile(dest, self.core_pos):
                    continue
                if not allow_recent and dest in recent:
                    continue
                if self.try_step(ct, direction):
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
        self,
        ct: Controller,
        target_env: Environment,
        preferred_dir: Direction,
    ) -> tuple[Position | None, Position | None]:
        current = ct.get_position()
        best_ore: Position | None = None
        best_stand: Position | None = None
        best_score = 10**9
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
                best_ore = pos
                best_stand = stand
                best_score = score
        return best_ore, best_stand

    def ore_target_still_valid(
        self, ct: Controller, pos: Position, target_env: Environment | None
    ) -> bool:
        if target_env is None:
            return False
        if not ct.is_in_vision(pos):
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
        candidates.sort(
            key=lambda pos: (
                current.distance_squared(pos),
                direction_rank(
                    self.preferred_dir or Direction.CENTRE,
                    current.direction_to(pos),
                ),
            )
        )
        return candidates[0]

    # -----------------------------------------------------------------------
    # Defense
    # -----------------------------------------------------------------------

    def count_active_primary_lanes(self, ct: Controller) -> int:
        assert self.core_pos is not None and self.lane_dirs is not None
        count = 0
        for direction in self.lane_dirs[:3]:
            pos = slot_for(self.core_pos, direction)
            if building_type_at(ct, pos) in ROUTABLE_ENDPOINTS:
                count += 1
        return count

    def count_foundries(self, ct: Controller) -> int:
        assert self.core_pos is not None and self.lane_dirs is not None
        count = 0
        for direction in self.lane_dirs[:3]:
            pos = slot_for(self.core_pos, direction)
            if building_type_at(ct, pos) == EntityType.FOUNDRY:
                count += 1
        return count

    def refinery_started(self, ct: Controller) -> bool:
        _, axionite = ct.get_global_resources()
        return axionite > 0 or self.count_foundries(ct) > 0

    def should_build_more_defense(self, ct: Controller) -> bool:
        if not self.defense_ready(ct):
            return False
        return self.handle_defense(ct, barriers_first=False)

    def handle_opportunistic_turrets(self, ct: Controller) -> bool:
        if not self.defense_ready(ct):
            return False
        if self.core_pos is None or not on_core_tile(ct.get_position(), self.core_pos):
            return False
        return self.handle_defense(ct, barriers_first=False)

    def handle_defense(self, ct: Controller, barriers_first: bool) -> bool:
        assert self.core_pos is not None

        # Move onto the core tile if not already there
        if not on_core_tile(ct.get_position(), self.core_pos):
            move_dir = ct.get_position().direction_to(self.core_pos)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
                return True

        barrier_positions, turret_positions = self.defense_positions()

        # Barriers first (optional)
        if barriers_first:
            for pos in barrier_positions:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if (
                    can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                    and ct.can_build_barrier(pos)
                ):
                    ct.build_barrier(pos)
                    return True

        # Turrets on ring-2 (outward facing)
        for pos in turret_positions:
            if building_type_at(ct, pos) == EntityType.GUNNER:
                continue
            cost = ct.get_gunner_cost()
            face = self.core_pos.direction_to(pos)
            if (
                can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                and ct.can_build_gunner(pos, face)
            ):
                ct.build_gunner(pos, face)
                return True

        # Extra turrets on ring-3 (deeper perimeter) – NEW
        if self.width is not None and self.height is not None:
            reserved = {slot_for(self.core_pos, d) for d in (self.lane_dirs or [])}
            for pos in ring_positions(self.core_pos, 3, self.width, self.height):
                if pos in reserved:
                    continue
                if building_type_at(ct, pos) in (EntityType.GUNNER, EntityType.BARRIER):
                    continue
                cost = ct.get_gunner_cost()
                face = self.core_pos.direction_to(pos)
                if (
                    can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                    and ct.can_build_gunner(pos, face)
                ):
                    ct.build_gunner(pos, face)
                    return True

        # Barriers afterward if not done first
        if not barriers_first:
            for pos in barrier_positions:
                if building_type_at(ct, pos) == EntityType.BARRIER:
                    continue
                cost = ct.get_barrier_cost()
                if (
                    can_afford(ct, cost[0], cost[1], reserve=reserve_defense(ct))
                    and ct.can_build_barrier(pos)
                ):
                    ct.build_barrier(pos)
                    return True

        return False

    def defense_positions(self) -> tuple[list[Position], list[Position]]:
        assert (
            self.core_pos is not None
            and self.lane_dirs is not None
            and self.width is not None
            and self.height is not None
        )
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
        for direction in self.lane_dirs[: self.target_foundry_count()]:
            refined_slot = self.choose_refined_slot(direction, self.choose_raw_slot(direction))
            if refined_slot is not None:
                reserved.add(refined_slot)

        available = [pos for pos in ring_two if pos not in reserved]
        available.sort(
            key=lambda p: (
                abs(p.x - self.core_pos.x) + abs(p.y - self.core_pos.y),
                p.y,
                p.x,
            )
        )

        # Put more slots into turrets than before (2/3 turrets, 1/3 barriers)
        split = max(0, len(available) // 3)
        barriers = available[:split]
        turrets = available[split:]
        return barriers, turrets

    def choose_raw_slot(self, direction: Direction) -> Position:
        assert (
            self.core_pos is not None
            and self.width is not None
            and self.height is not None
        )
        foundry_slot = slot_for(self.core_pos, direction)
        centre = Position(self.width // 2, self.height // 2)
        candidates = [
            foundry_slot.add(cardinal_left(direction)),
            foundry_slot.add(cardinal_right(direction)),
        ]
        home_slots = {slot_for(self.core_pos, d) for d in self.lane_dirs or []}
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
                best = pos
                best_score = score
        return best

    def choose_refined_slot(
        self, direction: Direction, raw_slot: Position | None
    ) -> Position | None:
        assert (
            self.core_pos is not None
            and self.width is not None
            and self.height is not None
        )
        foundry_slot = slot_for(self.core_pos, direction)
        candidates = [
            foundry_slot.add(cardinal_left(direction)),
            foundry_slot.add(cardinal_right(direction)),
        ]
        home_slots = {slot_for(self.core_pos, d) for d in self.lane_dirs or []}
        best: Position | None = None
        best_score = 999
        for pos in candidates:
            score = 0
            if raw_slot is not None and pos == raw_slot:
                score += 200
            if pos in home_slots:
                score += 50
            if not in_bounds(pos, self.width, self.height):
                score += 100
            if on_core_tile(pos, self.core_pos):
                score += 100
            if score < best_score:
                best = pos
                best_score = score
        if best_score >= 100:
            return None
        return best
