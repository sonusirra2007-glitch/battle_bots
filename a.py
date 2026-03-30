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
    EntityType.ARMOURED_CONVEYOR,
    EntityType.ROAD,
}
ROUTABLE_ENDPOINTS = {
    EntityType.CONVEYOR,
    EntityType.FOUNDRY,
}

# How many *extra* titanium above the running reserve we need before the core
# spawns another builder beyond the initial five.
EXTRA_SPAWN_SURPLUS = 200


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

        # --- core (spawner) state ---
        self.spawned = 0

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

        spawn_tiles: list[Position] = []
        for direction in self.lane_dirs[:3]:
            pos = slot_target_for(self.core_pos, direction)
            if pos not in spawn_tiles:
                spawn_tiles.append(pos)
        for direction in self.strategy_dirs:
            pos = slot_target_for(self.core_pos, direction)
            if pos not in spawn_tiles:
                spawn_tiles.append(pos)
            if len(spawn_tiles) >= 8:  # extended pool for extra spawns
                break
        self.spawn_tiles = spawn_tiles[:8]

    # -----------------------------------------------------------------------
    # Core logic
    # -----------------------------------------------------------------------

    def run_core(self, ct: Controller) -> None:
        assert self.core_pos is not None and self.strategy_dirs is not None

        # Always try to fill the defence ring opportunistically.
        self._core_build_defense(ct)

        # Spawn phase 1: first 3 feeders (unconditional once we can afford them)
        if self.spawned < 3:
            self.try_spawn_builder(ct, self.spawned)
            return

        primary_online = self.count_active_primary_lanes(ct)
        late_fallback = ct.get_current_round() >= 80 and titanium_ready_for_extra_builders(ct)

        # Spawn phase 2: builders 4 & 5 (fortifier + expander)
        if self.spawned < 5:
            if titanium_ready_for_extra_builders(ct) and (
                primary_online >= 3 or late_fallback
            ):
                self.try_spawn_builder(ct, self.spawned)
            return

        # Spawn phase 3: unlimited extra scouts/builders while we have surplus
        if titanium_surplus_for_spawn(ct):
            # Alternate between scout and extra feeder every other extra spawn
            self.try_spawn_builder(ct, self.spawned)

    def _core_build_defense(self, ct: Controller) -> None:
        """Core itself places barriers/turrets in its immediate ring when affordable."""
        assert self.core_pos is not None and self.lane_dirs is not None
        if self.width is None or self.height is None:
            return
        reserved = {slot_for(self.core_pos, d) for d in self.lane_dirs}
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

        # Pick a spawn tile, cycling through the pool for extra bots
        tile_index = spawn_index % len(self.spawn_tiles)
        pos = self.spawn_tiles[tile_index]
        if ct.can_spawn(pos):
            ct.spawn_builder(pos)
            self.spawned += 1

    # -----------------------------------------------------------------------
    # Builder bootstrap
    # -----------------------------------------------------------------------

    def run_builder(self, ct: Controller) -> None:
        self.bootstrap_builder(ct)

        # Scout role has its own execution path
        if self.role == "scout":
            self.run_scout(ct)
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
    # Scout logic  (NEW)
    # -----------------------------------------------------------------------

    def run_scout(self, ct: Controller) -> None:
        """
        Scouts move toward the enemy half of the map, scanning for the enemy
        core.  Once found (or inferred), they place offensive gunners on tiles
        adjacent to it and keep trying to fill every adjacent open slot.
        """
        assert self.core_pos is not None and self.width is not None and self.height is not None

        # --- try to spot enemy core in vision ---
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
            self._scout_attack(ct)
        else:
            self._scout_advance(ct)

    def _scout_advance(self, ct: Controller) -> None:
        """Walk toward the estimated enemy core location."""
        assert self.width is not None and self.height is not None and self.core_pos is not None
        if self.scout_advance_target is None:
            self.scout_advance_target = Position(
                self.width - 1 - self.core_pos.x,
                self.height - 1 - self.core_pos.y,
            )
        target = self.scout_advance_target
        if not self.move_towards(ct, target, self.home_dir or Direction.NORTH):
            # Nudge direction on stall
            self.stall_turns += 1
            if self.stall_turns >= 3:
                self.home_dir = cardinal_right(self.home_dir or Direction.NORTH)
                self.stall_turns = 0
        else:
            self.stall_turns = 0

    def _scout_attack(self, ct: Controller) -> None:
        """
        Move adjacent to the enemy core and build gunners on every surrounding
        tile we can reach.  Prioritises tiles directly adjacent to the core.
        """
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()

        # Collect candidate attack tiles (ring-1 and ring-2 around enemy core)
        attack_tiles: list[Position] = []
        for radius in (1, 2):
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

        # Try to place a gunner on any adjacent attack tile
        for pos in attack_tiles:
            etype = building_type_at(ct, pos)
            if etype == EntityType.GUNNER:
                continue
            # Must be standing adjacent to place
            if current.distance_squared(pos) <= 2:
                cost = ct.get_gunner_cost()
                face = pos.direction_to(self.enemy_core_pos)
                if can_afford(ct, cost[0], cost[1], reserve=0):
                    if ct.can_build_gunner(pos, face):
                        ct.build_gunner(pos, face)
                        return

        # Move toward the nearest unoccupied attack tile
        target = min(
            attack_tiles,
            key=lambda p: current.distance_squared(p),
        )
        if not self.move_towards(ct, target, self.home_dir or Direction.NORTH):
            self.stall_turns += 1
            if self.stall_turns >= 3:
                self.home_dir = cardinal_right(self.home_dir or Direction.NORTH)
                self.stall_turns = 0
        else:
            self.stall_turns = 0

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
        target_foundries = 3

        # 2) Raw axionite lane (builds a foundry at the lane endpoint)
        if not self.raw_axionite_done and titanium_healthy_for_raw(ct):
            self.start_mission(
                target_env=Environment.ORE_AXIONITE,
                sink_pos=self.raw_slot,
                sink_target=self.home_target,
                preferred_dir=self.raw_preferred_dir(),
                mission="raw_axionite",
            )
            return

        # 3) Wait for enough foundries before going on offense
        if foundries_online < target_foundries:
            if not self.extra_titanium_done and titanium_low(ct):
                self.assign_extra_titanium_mission()
            return

        # 4) Refined-axionite delivery route (NEW)
        if not self.refined_route_done and self.foundry_online:
            self._start_refined_delivery_mission(ct)
            if self.mission != "idle":
                return

        # 5) Defense
        if self.should_build_more_defense(ct):
            return

        # 6) Extra titanium lane
        if not self.extra_titanium_done and (titanium_low(ct) or self.refinery_started(ct)):
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

        if self.count_foundries(ct) > 0 and not self.extra_titanium_done:
            self.assign_extra_titanium_mission()
            return

        if self.should_build_more_defense(ct):
            return

    # -----------------------------------------------------------------------
    # Refined axionite delivery (NEW)
    # -----------------------------------------------------------------------

    def _start_refined_delivery_mission(self, ct: Controller) -> None:
        """
        After a foundry is online at home_slot, route a conveyor from the
        foundry back toward the core storage tile so refined axionite flows in.
        We reuse the existing mission infrastructure: the "sink" is the core
        adjacency tile (home_target) and the ore target is the foundry itself.
        """
        if self.home_slot is None or self.home_target is None or self.home_dir is None:
            return
        # Check the foundry actually exists before routing
        if building_type_at(ct, self.home_slot) != EntityType.FOUNDRY:
            return
        self.start_mission(
            target_env=Environment.ORE_AXIONITE,   # not used for actual ore search
            sink_pos=self.home_target,              # deposit at core-adjacent tile
            sink_target=self.core_pos,              # final destination: core
            preferred_dir=cardinal_opposite(self.home_dir),
            mission="refined_delivery",
        )

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
        Walk from the foundry slot back toward the core, placing conveyors
        that carry refined axionite inward.
        """
        assert self.home_slot is not None and self.home_target is not None
        assert self.core_pos is not None and self.home_dir is not None

        current = ct.get_position()

        # Make sure the foundry->core conveyor chain exists.
        # We walk from home_slot toward home_target and place inward conveyors.
        inward = cardinal_opposite(self.home_dir)
        pos = self.home_slot
        # Place conveyor at home_slot pointing inward if not already there
        tile = building_type_at(ct, pos)
        if tile != EntityType.FOUNDRY and tile != EntityType.CONVEYOR:
            cost = ct.get_conveyor_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
                if ct.can_build_conveyor(pos, inward):
                    ct.build_conveyor(pos, inward)
                    return

        # Walk to the slot between home_slot and core (home_target),
        # place conveyor pointing toward core
        mid = self.home_target
        mid_tile = building_type_at(ct, mid)
        if mid_tile not in (EntityType.CONVEYOR, EntityType.CORE):
            cost = ct.get_conveyor_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_direct(ct)):
                if ct.can_build_conveyor(mid, inward):
                    ct.build_conveyor(mid, inward)
                    self.refined_route_done = True
                    self.mission = "idle"
                    return
            # Move toward mid to build it
            if not self.move_towards(ct, mid, inward):
                pass
            return

        # Route is already done
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
        if current == self.sink_target:
            if not self.trail:
                self.mission = "idle"
                return

            if self.mission == "raw_axionite_connecting":
                foundry_cost = ct.get_foundry_cost()
                conveyor_cost = ct.get_conveyor_cost()
                needed = foundry_cost[0] + conveyor_cost[0]
                if not can_afford(ct, needed, 0, reserve=reserve_direct(ct)):
                    return
                # The final conveyor at trail[0] must point TOWARD the foundry
                # (home_slot), not toward sink_target (home_target).  This
                # matches the original b.py execute_connecting override.
                if self.home_slot is None:
                    return
                if not self.ensure_conveyor_tile(
                    ct,
                    self.trail[0],
                    self.trail[0].direction_to(self.home_slot),
                ):
                    return
                # Build the foundry at home_slot
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
            return

        if current not in self.trail:
            return

        current_index = self.trail.index(current)
        if current_index < len(self.trail) - 1:
            target_tile = self.trail[current_index + 1]
            if not self.ensure_conveyor_tile(
                ct, target_tile, target_tile.direction_to(current)
            ):
                return

        if current_index > 0:
            move_dir = current.direction_to(self.trail[current_index - 1])
            if ct.can_move(move_dir):
                ct.move(move_dir)
            return

        move_dir = current.direction_to(self.sink_target)
        if ct.can_move(move_dir):
            ct.move(move_dir)

    # -----------------------------------------------------------------------
    # Building helpers
    # -----------------------------------------------------------------------

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

        if not on_core_tile(current, self.core_pos):
            move_dir = current.direction_to(self.core_pos)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
            return

        launch_tile = slot_target_for(self.core_pos, self.preferred_dir)
        if current != launch_tile and on_core_tile(current, self.core_pos):
            move_dir = current.direction_to(launch_tile)
            if move_dir != Direction.CENTRE and ct.can_move(move_dir):
                ct.move(move_dir)
            return

        if not ct.is_tile_passable(self.sink_pos):
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
        if not self.refinery_started(ct):
            return False
        return self.handle_defense(ct, barriers_first=False)

    def handle_opportunistic_turrets(self, ct: Controller) -> bool:
        if not self.refinery_started(ct):
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
            if score < best_score:
                best = pos
                best_score = score
        return best