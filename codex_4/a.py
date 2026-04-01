from __future__ import annotations

"""Pure-titanium Battlecode bot.

Strategy:
  - Completely ignore axionite / foundries.
  - Mine titanium only; feed it to the core via conveyors.
  - Once enough economy is running, assault bots rush the enemy core.
  - Assault bots build siege gunners near the enemy core, then build a
    titanium harvester → conveyor chain directly to those gunners so they
    fire every single round (gunner uses 2 Ti/shot, reloads each round).
  - Extra launchers catapult builders over terrain to speed up the assault.
"""

from collections import deque

from cambc import (
    Controller,
    Direction,
    EntityType,
    Environment,
    GameConstants,
    Position,
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
    EntityType.BRIDGE,
}
ROUTABLE_ENDPOINTS = {
    EntityType.CONVEYOR,
    EntityType.BRIDGE,
}

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# How many miner-builders to spawn before sending assault bots.
OPENING_MINERS = 4
# Total assault bots to maintain.
TARGET_ASSAULT_BOTS = 14
# Titanium reserve kept in the bank at all times.
BASE_RESERVE = 80
# Round at which we allow offense even if economy is thin.
OFFENSE_MIN_ROUND = 80
# Surplus above reserves required before spending on assault spawns.
ASSAULT_SURPLUS = 120
# Cooldown between extra assault spawns (rounds).
ASSAULT_SPAWN_COOLDOWN = 8
# Stall turns before a stuck assault bot tries to build a road or launcher.
STALL_FOR_LAUNCHER = 5


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
    left = primary
    right = primary
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


def slot_for(core_pos: Position, direction: Direction) -> Position:
    """Two steps out from core in *direction* — the harvester/conveyor endpoint."""
    return step(core_pos, direction, 2)


def slot_target_for(core_pos: Position, direction: Direction) -> Position:
    """One step out from core in *direction* — the conveyor that touches the core."""
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
    tiles: list[Position] = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            pos = Position(core_pos.x + dx, core_pos.y + dy)
            if in_bounds(pos, width, height):
                tiles.append(pos)
    return tiles


def adjacent_cardinals(pos: Position) -> list[Position]:
    return [pos.add(d) for d in CARDINAL_DIRECTIONS]


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

    def add(d: Direction) -> None:
        if d != Direction.CENTRE and d not in ordered:
            ordered.append(d)

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
    for d in DIRECTIONS:
        add(d)
    return ordered


def symmetry_guesses(core_pos: Position, width: int, height: int) -> list[Position]:
    guesses = [
        Position(width - 1 - core_pos.x, height - 1 - core_pos.y),
        Position(width - 1 - core_pos.x, core_pos.y),
        Position(core_pos.x, height - 1 - core_pos.y),
    ]
    unique: list[Position] = []
    for guess in guesses:
        if guess != core_pos and guess not in unique:
            unique.append(guess)
    return unique or [core_pos]


# ---------------------------------------------------------------------------
# Reserve helpers
# ---------------------------------------------------------------------------

def reserve_economy(ct: Controller) -> int:
    """Minimum titanium to keep in the bank while building economy."""
    return ct.get_harvester_cost()[0] + ct.get_conveyor_cost()[0] * 4 + BASE_RESERVE


def reserve_assault(ct: Controller) -> int:
    """Minimum titanium to keep while assault bots are active."""
    return ct.get_gunner_cost()[0] * 2 + BASE_RESERVE


def titanium_surplus_for_assault(ct: Controller) -> bool:
    titanium, _ = ct.get_global_resources()
    return titanium >= reserve_economy(ct) + ct.get_builder_bot_cost()[0] + ASSAULT_SURPLUS


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Player:
    def __init__(self):
        # Shared
        self.core_pos: Position | None = None
        self.core_id: int | None = None
        self.width: int | None = None
        self.height: int | None = None
        self.lane_dirs: list[Direction] | None = None
        self.extra_dirs: list[Direction] | None = None
        self.enemy_guesses: list[Position] | None = None
        self.miner_spawn_tiles: list[Position] | None = None
        self.assault_spawn_tiles: list[Position] | None = None

        # Builder
        self.initialized = False
        self.role = "unknown"
        self.initial_pos: Position | None = None
        self.lane_index: int | None = None
        self.home_dir: Direction | None = None
        self.home_slot: Position | None = None
        self.home_target: Position | None = None
        self.preferred_dir: Direction | None = None

        self.mission = "idle"
        self.target_env: Environment | None = None
        self.sink_pos: Position | None = None
        self.sink_target: Position | None = None
        self.target_ore: Position | None = None
        self.target_stand: Position | None = None
        self.trail: list[Position] = []
        self.stall_turns = 0
        self.sweep_sign = 1
        self.primary_titanium_done = False

        # Assault
        self.enemy_core_pos: Position | None = None
        self.assault_guess_index = 0
        self.assault_exit_dir: Direction | None = None
        self.assault_flow_signature: tuple | None = None
        self.assault_flow: dict[tuple[int, int], int] | None = None
        # Track which gunner slot this bot is responsible for ammo-feeding
        self.ammo_gunner_pos: Position | None = None
        self.ammo_chain_done = False

        # Core (spawner)
        self.opening_spawned = 0
        self.assault_spawn_cursor = 0
        self.last_assault_spawn_round = -999

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
        elif entity_type == EntityType.LAUNCHER:
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
            core_id = nearby_core_id(ct)
            if core_id is not None:
                self.core_id = core_id
                self.core_pos = ct.get_position(core_id)
            else:
                self.core_pos = ct.get_position()

        assert self.width and self.height and self.core_pos
        centre = Position(self.width // 2, self.height // 2)

        # Cardinal lanes for titanium mining
        lanes = [
            d for d in CARDINAL_DIRECTIONS
            if in_bounds(slot_for(self.core_pos, d), self.width, self.height)
        ]
        lanes.sort(key=lambda d: slot_for(self.core_pos, d).distance_squared(centre))
        if not lanes:
            lanes = [Direction.NORTH]
        self.lane_dirs = lanes

        # Extra directions (diagonals etc.) for additional miners
        all_dirs = unique_dirs(self.core_pos.direction_to(centre))
        extras = [d for d in all_dirs if d not in self.lane_dirs]
        self.extra_dirs = extras

        self.enemy_guesses = symmetry_guesses(self.core_pos, self.width, self.height)

        # Spawn tiles for miners (adjacent to core along each lane)
        miner_tiles = [slot_target_for(self.core_pos, d) for d in self.lane_dirs]
        used = set(miner_tiles)

        # Spawn tiles for assault (remaining core-adjacent tiles)
        core_adj = [
            Position(self.core_pos.x + dx, self.core_pos.y + dy)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if in_bounds(
                Position(self.core_pos.x + dx, self.core_pos.y + dy),
                self.width, self.height,
            )
        ]
        assault_tiles = [p for p in core_adj if p not in used] or miner_tiles

        self.miner_spawn_tiles = miner_tiles
        self.assault_spawn_tiles = assault_tiles

    # -----------------------------------------------------------------------
    # Core logic
    # -----------------------------------------------------------------------

    def run_core(self, ct: Controller) -> None:
        assert (
            self.miner_spawn_tiles is not None
            and self.assault_spawn_tiles is not None
        )
        current_round = ct.get_current_round()

        # Phase 1: spawn opening miners
        if self.opening_spawned < OPENING_MINERS:
            tile = self.miner_spawn_tiles[self.opening_spawned % len(self.miner_spawn_tiles)]
            if self.try_spawn_specific(ct, tile, reserve=BASE_RESERVE):
                self.opening_spawned += 1
            return

        # Phase 2: keep spawning miners up to one per lane (re-spawn if a lane
        # is still empty)
        missing_miner = self.first_missing_lane(ct)
        if missing_miner is not None and self.opening_spawned < len(self.lane_dirs or []):
            tile = self.miner_spawn_tiles[missing_miner % len(self.miner_spawn_tiles)]
            if self.try_spawn_specific(ct, tile, reserve=reserve_economy(ct)):
                self.opening_spawned += 1
            return

        # Phase 3: spawn assault bots when we have surplus
        desired_total = min(GameConstants.MAX_TEAM_UNITS, 1 + OPENING_MINERS + TARGET_ASSAULT_BOTS)
        can_spawn_assault = (
            ct.get_unit_count() < desired_total
            and (current_round >= OFFENSE_MIN_ROUND or titanium_surplus_for_assault(ct))
            and titanium_surplus_for_assault(ct)
            and current_round - self.last_assault_spawn_round >= ASSAULT_SPAWN_COOLDOWN
        )
        if can_spawn_assault:
            if self.try_spawn_assault(ct):
                self.last_assault_spawn_round = current_round

    def first_missing_lane(self, ct: Controller) -> int | None:
        assert self.core_pos is not None and self.lane_dirs is not None
        for i, d in enumerate(self.lane_dirs):
            pos = slot_for(self.core_pos, d)
            if building_type_at(ct, pos) not in ROUTABLE_ENDPOINTS:
                return i
        return None

    def try_spawn_specific(self, ct: Controller, pos: Position, reserve: int) -> bool:
        cost = ct.get_builder_bot_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=reserve):
            return False
        if ct.can_spawn(pos):
            ct.spawn_builder(pos)
            return True
        return False

    def try_spawn_assault(self, ct: Controller) -> bool:
        assert self.assault_spawn_tiles is not None
        for offset in range(len(self.assault_spawn_tiles)):
            index = (self.assault_spawn_cursor + offset) % len(self.assault_spawn_tiles)
            pos = self.assault_spawn_tiles[index]
            if self.try_spawn_specific(ct, pos, reserve=reserve_economy(ct)):
                self.assault_spawn_cursor = (index + 1) % len(self.assault_spawn_tiles)
                return True
        return False

    # -----------------------------------------------------------------------
    # Builder dispatch
    # -----------------------------------------------------------------------

    def run_builder(self, ct: Controller) -> None:
        self.bootstrap_builder(ct)

        if self.role == "assault":
            self.run_assault(ct)
            return

        # Miner role
        if self.mission == "idle":
            self.assign_miner_mission(ct)
        if self.mission == "idle":
            self.try_support_orbit(ct)
            return
        self.execute_mission(ct)

    def bootstrap_builder(self, ct: Controller) -> None:
        if self.initialized:
            return
        if self.core_pos is None:
            self.bootstrap_common(ct)

        assert (
            self.core_pos is not None
            and self.lane_dirs is not None
            and self.enemy_guesses is not None
            and self.miner_spawn_tiles is not None
        )

        self.initial_pos = ct.get_position()

        if self.initial_pos in (self.miner_spawn_tiles or []):
            self.lane_index = self.miner_spawn_tiles.index(self.initial_pos)
            self.role = f"miner_{self.lane_index}"
            self.sweep_sign = -1 if self.lane_index % 2 == 0 else 1
            self.home_dir = self.lane_dirs[self.lane_index % len(self.lane_dirs)]
            self.home_slot = slot_for(self.core_pos, self.home_dir)
            self.home_target = slot_target_for(self.core_pos, self.home_dir)
            self.preferred_dir = self.home_dir
        else:
            self.role = "assault"
            primary = self.core_pos.direction_to(self.enemy_guesses[0])
            exit_dirs = directional_preferences(primary)
            stage = ct.get_id() % 8
            self.assault_exit_dir = exit_dirs[stage % min(5, len(exit_dirs))]

        self.initialized = True

    # -----------------------------------------------------------------------
    # Miner mission
    # -----------------------------------------------------------------------

    def assign_miner_mission(self, ct: Controller) -> None:
        if not self.primary_titanium_done:
            self.start_mission(
                target_env=Environment.ORE_TITANIUM,
                sink_pos=self.home_slot,
                sink_target=self.home_target,
                preferred_dir=self.home_dir,
                mission="primary_titanium",
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
        if self.mission.endswith("_connecting"):
            self.execute_connecting(ct)
            return
        self.execute_searching(ct)

    # -----------------------------------------------------------------------
    # Searching phase
    # -----------------------------------------------------------------------

    def execute_searching(self, ct: Controller) -> None:
        assert self.sink_pos is not None and self.sink_target is not None and self.preferred_dir is not None
        current = ct.get_position()

        if not self.trail:
            if current == self.sink_pos:
                self.trail = [current]
            else:
                self.move_to_sink(ct)
                return

        if self.target_ore is None or not self.ore_target_still_valid(ct, self.target_ore):
            self.target_ore, self.target_stand = self.plan_visible_ore(ct)
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

                cost = ct.get_harvester_cost()
                if (
                    can_afford(ct, cost[0], cost[1], reserve=reserve_economy(ct))
                    and ct.can_build_harvester(self.target_ore)
                ):
                    ct.build_harvester(self.target_ore)
                    self.mission = f"{self.mission}_connecting"
                return

            move_target = self.target_stand if self.target_stand is not None else self.target_ore
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
        d = self.preferred_dir
        if segment == 1:
            return cardinal_right(d) if self.sweep_sign > 0 else cardinal_left(d)
        if segment == 2:
            return cardinal_opposite(d)
        return d

    # -----------------------------------------------------------------------
    # Connecting phase (build conveyor from harvester back to core)
    # -----------------------------------------------------------------------

    def execute_connecting(self, ct: Controller) -> None:
        assert self.sink_target is not None
        if not self.trail:
            self.mission = "idle"
            return

        current = ct.get_position()
        if current == self.sink_target:
            if not self.trail:
                self.mission = "idle"
                return
            if not self.ensure_transport_link(ct, self.trail[0], self.sink_target):
                return
            self.primary_titanium_done = True
            self.mission = "idle"
            return

        if current not in self.trail:
            return

        idx = self.trail.index(current)
        if idx < len(self.trail) - 1:
            if not self.ensure_transport_link(ct, self.trail[idx + 1], current):
                return

        if idx > 0:
            move_dir = current.direction_to(self.trail[idx - 1])
            if ct.can_move(move_dir):
                ct.move(move_dir)
            return

        move_dir = current.direction_to(self.sink_target)
        if ct.can_move(move_dir):
            ct.move(move_dir)

    def ensure_transport_link(self, ct: Controller, pos: Position, dest: Position) -> bool:
        """Place a conveyor at *pos* pointing toward *dest*, or a bridge if diagonal/far."""
        is_diagonal = pos.x != dest.x and pos.y != dest.y
        is_far = pos.distance_squared(dest) > 1
        use_bridge = is_diagonal or is_far

        if use_bridge and pos.distance_squared(dest) > 9:
            return False

        building_id = ct.get_tile_building_id(pos)
        if building_id is not None:
            entity_type = ct.get_entity_type(building_id)
            if use_bridge and entity_type == EntityType.BRIDGE:
                if ct.get_bridge_target(building_id) == dest:
                    return True
            if (
                not use_bridge
                and entity_type == EntityType.CONVEYOR
                and ct.get_direction(building_id) == pos.direction_to(dest)
            ):
                return True
            if entity_type in WALKABLE_BUILDINGS or entity_type == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return False
            else:
                return False

        if use_bridge:
            cost = ct.get_bridge_cost()
            if not can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
                return False
            if ct.can_build_bridge(pos, dest):
                ct.build_bridge(pos, dest)
                return True
            return False

        cost = ct.get_conveyor_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
            return False
        if ct.can_build_conveyor(pos, pos.direction_to(dest)):
            ct.build_conveyor(pos, pos.direction_to(dest))
            return True
        return False

    # -----------------------------------------------------------------------
    # Assault logic
    # -----------------------------------------------------------------------

    def run_assault(self, ct: Controller) -> None:
        assert self.enemy_guesses is not None
        current = ct.get_position()

        # Shoot any enemy building we're standing on
        tile_building = ct.get_tile_building_id(current)
        if tile_building is not None and ct.get_team(tile_building) != ct.get_team():
            if ct.can_fire(current):
                ct.fire(current)
                return

        self.scan_for_enemy_core(ct)

        if self.enemy_core_pos is not None:
            # Priority 1: ensure our designated gunner has a titanium ammo feed
            if self.try_maintain_ammo_chain(ct):
                return
            # Priority 2: build/complete the siege ring
            if self.run_siege(ct):
                return

        # Move toward enemy
        target = self.enemy_core_pos or self.enemy_guesses[
            self.assault_guess_index % len(self.enemy_guesses)
        ]

        # Advance guess if we've reached it without finding the core
        if self.enemy_core_pos is None and current.distance_squared(target) <= 20:
            self.advance_assault_guess()
            target = self.enemy_guesses[self.assault_guess_index % len(self.enemy_guesses)]

        # Breakout from home base
        if self.run_assault_breakout(ct):
            self.stall_turns = 0
            return

        moved = self.advance_assault_flow(ct, target)
        if not moved:
            moved = self._assault_step_toward(ct, target)

        if moved:
            self.stall_turns = 0
            return

        self.stall_turns += 1
        if (
            self.core_pos is not None
            and current.distance_squared(self.core_pos) >= 49
            and self.stall_turns >= STALL_FOR_LAUNCHER
            and self.try_build_path_launcher(ct)
        ):
            self.stall_turns = 0

    def run_assault_breakout(self, ct: Controller) -> bool:
        assert self.core_pos is not None
        current = ct.get_position()
        if current.distance_squared(self.core_pos) > 25:
            return False
        if self.assault_exit_dir is None:
            return False
        target = self.core_pos
        for _ in range(5):
            nxt = target.add(self.assault_exit_dir)
            if not in_bounds(nxt, self.width or 0, self.height or 0):
                break
            target = nxt
        if target == current:
            return False
        return self._assault_step_toward(ct, target)

    def advance_assault_guess(self) -> None:
        assert self.enemy_guesses is not None
        if len(self.enemy_guesses) <= 1:
            return
        self.assault_guess_index = (self.assault_guess_index + 1) % len(self.enemy_guesses)
        self.assault_flow_signature = None
        self.assault_flow = None

    # -----------------------------------------------------------------------
    # Ammo chain: build Ti harvester + conveyor(s) into a siege gunner
    # -----------------------------------------------------------------------

    def try_maintain_ammo_chain(self, ct: Controller) -> bool:
        """
        Find a siege gunner near the enemy core that has no titanium supply,
        locate the nearest titanium ore tile, build a harvester on it, then
        lay a conveyor chain into the gunner's non-facing side.

        Gunners accept ammo from any side except the direction they're facing,
        so we feed from the side or back.

        Returns True if an action was taken this turn.
        """
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()

        # ---- find a gunner that needs feeding ----
        if self.ammo_gunner_pos is None or self.ammo_chain_done:
            best_gunner: Position | None = None
            best_score = 10 ** 9
            for pos in ct.get_nearby_tiles():
                if not ct.is_in_vision(pos):
                    continue
                bid = ct.get_tile_building_id(pos)
                if bid is None:
                    continue
                if ct.get_entity_type(bid) != EntityType.GUNNER:
                    continue
                if ct.get_team(bid) != ct.get_team():
                    continue
                # Check if any adjacent tile already has a conveyor feeding it
                already_fed = False
                for adj in adjacent_cardinals(pos):
                    abid = ct.get_tile_building_id(adj)
                    if abid is not None and ct.get_entity_type(abid) == EntityType.CONVEYOR:
                        ad = ct.get_direction(abid)
                        if adj.add(ad) == pos:
                            already_fed = True
                            break
                if already_fed:
                    continue
                score = pos.distance_squared(self.enemy_core_pos) + current.distance_squared(pos)
                if score < best_score:
                    best_score = score
                    best_gunner = pos
            if best_gunner is not None:
                self.ammo_gunner_pos = best_gunner
                self.ammo_chain_done = False

        if self.ammo_gunner_pos is None:
            return False

        gunner_pos = self.ammo_gunner_pos
        # Verify the gunner still exists
        bid = ct.get_tile_building_id(gunner_pos) if ct.is_in_vision(gunner_pos) else None
        if bid is None or ct.get_entity_type(bid) != EntityType.GUNNER or ct.get_team(bid) != ct.get_team():
            self.ammo_gunner_pos = None
            return False

        gunner_face: Direction = ct.get_direction(bid)

        # ---- find the nearest titanium ore within vision ----
        best_ore: Position | None = None
        best_ore_score = 10 ** 9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
                continue
            existing = ct.get_tile_building_id(pos)
            if existing is not None and ct.get_entity_type(existing) != EntityType.HARVESTER:
                continue
            # Prefer ore close to the gunner and close to us
            score = pos.distance_squared(gunner_pos) * 4 + current.distance_squared(pos)
            if score < best_ore_score:
                best_ore = pos
                best_ore_score = score

        if best_ore is None:
            return False

        # ---- step 1: build harvester ----
        ore_building = ct.get_tile_building_id(best_ore)
        if ore_building is None:
            cost = ct.get_harvester_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                stand = self.find_build_stand(ct, best_ore)
                if stand is not None and current.distance_squared(best_ore) <= GameConstants.ACTION_RADIUS_SQ:
                    if abs(current.x - best_ore.x) + abs(current.y - best_ore.y) == 1:
                        if ct.can_build_harvester(best_ore):
                            ct.build_harvester(best_ore)
                            return True
                if stand is not None:
                    return self._assault_step_toward(ct, stand)
            return False

        # ---- step 2: build conveyor(s) from harvester to gunner ----
        # We need a chain: harvester → ... → conveyor → gunner (from non-facing side)
        # Identify a valid feed side on the gunner (not the facing direction)
        feed_side: Position | None = None
        for adj in adjacent_cardinals(gunner_pos):
            if not in_bounds(adj, self.width, self.height):
                continue
            direction_to_gunner = adj.direction_to(gunner_pos)
            if direction_to_gunner == gunner_face:
                continue  # would be outputting into facing — not allowed as input
            # Check the gunner actually accepts from this side
            # Gunners accept from all sides except the one they face (cardinal)
            # For diagonal gunners all 4 sides are fine — but ours are cardinal.
            abid = ct.get_tile_building_id(adj)
            if abid is not None:
                atype = ct.get_entity_type(abid)
                if atype not in (EntityType.CONVEYOR, *WALKABLE_BUILDINGS, EntityType.MARKER):
                    continue
            feed_side = adj
            break

        if feed_side is None:
            return False

        # Build the direct link: harvester → feed_side conveyor → gunner
        # First: link harvester to feed_side with conveyor(s)
        # If they are adjacent, one conveyor is enough.
        if best_ore.distance_squared(feed_side) <= 2:
            # Direct: build a conveyor on feed_side pointing toward gunner
            if not self.place_conveyor_at(ct, feed_side, gunner_pos):
                return self._assault_step_toward(ct, feed_side)
            # Also ensure harvester output points at feed_side
            # (Harvesters output automatically in least-recently-used direction;
            # we can optionally place a conveyor between harvester and feed_side)
            return True

        # Multi-hop: build a chain
        # For simplicity, move toward feed_side and place conveyors step by step
        # from harvester toward feed_side
        if current.distance_squared(feed_side) > GameConstants.ACTION_RADIUS_SQ:
            return self._assault_step_toward(ct, feed_side)

        return self.place_conveyor_at(ct, feed_side, gunner_pos)

    def place_conveyor_at(self, ct: Controller, pos: Position, dest: Position) -> bool:
        """Place a conveyor at *pos* pointing toward *dest*. Returns True if already correct or just built."""
        bid = ct.get_tile_building_id(pos)
        if bid is not None:
            etype = ct.get_entity_type(bid)
            if etype == EntityType.CONVEYOR and ct.get_direction(bid) == pos.direction_to(dest):
                return True
            if ct.get_team(bid) != ct.get_team() or etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                return False
            return False
        cost = ct.get_conveyor_cost()
        if not can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
            return False
        if ct.can_build_conveyor(pos, pos.direction_to(dest)):
            ct.build_conveyor(pos, pos.direction_to(dest))
            return True
        return False

    # -----------------------------------------------------------------------
    # Siege ring
    # -----------------------------------------------------------------------

    def run_siege(self, ct: Controller) -> bool:
        """Build gunners in a ring around the enemy core, facing inward."""
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()
        _, gunner_tiles = self.siege_positions()

        for pos in gunner_tiles:
            etype = building_type_at(ct, pos)
            if etype == EntityType.GUNNER:
                continue
            if etype in WALKABLE_BUILDINGS or etype == EntityType.MARKER:
                if ct.can_destroy(pos):
                    ct.destroy(pos)
                    return True
                continue
            face = pos.direction_to(self.enemy_core_pos)
            if face == Direction.CENTRE:
                continue
            cost = ct.get_gunner_cost()
            if can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                if ct.can_build_gunner(pos, face):
                    ct.build_gunner(pos, face)
                    return True

        # Move toward nearest open gunner slot
        open_slots = [
            pos for pos in gunner_tiles
            if building_type_at(ct, pos) not in (EntityType.GUNNER, None) or
               (ct.is_in_vision(pos) and ct.get_tile_building_id(pos) is None)
        ]
        # Recompute properly
        open_slots = [
            pos for pos in gunner_tiles
            if (not ct.is_in_vision(pos)) or ct.get_tile_building_id(pos) is None
        ]
        if open_slots:
            target = min(open_slots, key=lambda p: current.distance_squared(p))
            return self._assault_step_toward(ct, target)
        return False

    def siege_positions(self) -> tuple[list[Position], list[Position]]:
        assert self.enemy_core_pos is not None and self.width is not None and self.height is not None

        # Barrier ring at distance 2 (to protect gunners from melee)
        barrier_candidates = []
        for dx in (-1, 0, 1):
            barrier_candidates.append(Position(self.enemy_core_pos.x + dx, self.enemy_core_pos.y - 2))
            barrier_candidates.append(Position(self.enemy_core_pos.x + dx, self.enemy_core_pos.y + 2))
        for dy in (-1, 0, 1):
            barrier_candidates.append(Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y + dy))
            barrier_candidates.append(Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y + dy))

        # Gunner ring at corners distance 2 + ring 3 for wider coverage
        gunner_candidates = [
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y - 2),
            Position(self.enemy_core_pos.x - 2, self.enemy_core_pos.y + 2),
            Position(self.enemy_core_pos.x + 2, self.enemy_core_pos.y + 2),
            *ring_positions(self.enemy_core_pos, 3, self.width, self.height),
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

    def scan_for_enemy_core(self, ct: Controller) -> None:
        for entity_id in ct.get_nearby_buildings():
            if ct.get_entity_type(entity_id) != EntityType.CORE:
                continue
            if ct.get_team(entity_id) == ct.get_team():
                continue
            self.enemy_core_pos = ct.get_position(entity_id)
            return

    # -----------------------------------------------------------------------
    # Assault flow-field navigation
    # -----------------------------------------------------------------------

    def assault_navigable(self, ct: Controller, pos: Position) -> bool:
        assert self.core_pos is not None and self.width is not None and self.height is not None
        if not in_bounds(pos, self.width, self.height):
            return False
        if on_core_tile(pos, self.core_pos):
            return True
        if not ct.is_in_vision(pos):
            return True
        if ct.is_tile_passable(pos):
            return True
        if ct.get_tile_env(pos) != Environment.EMPTY:
            return False
        if ct.get_tile_builder_bot_id(pos) is not None:
            return False
        building_id = ct.get_tile_building_id(pos)
        if building_id is None:
            return True
        entity_type = ct.get_entity_type(building_id)
        return entity_type in WALKABLE_BUILDINGS or entity_type == EntityType.MARKER

    def ensure_assault_flow(self, ct: Controller, target: Position) -> dict[tuple[int, int], int] | None:
        assert self.enemy_guesses is not None
        goals: list[Position] = []

        def extend(radius: int) -> None:
            if self.width is None or self.height is None or self.enemy_core_pos is None:
                return
            ring = ring_positions(target, radius, self.width, self.height)
            for pos in ring:
                if on_core_tile(pos, target):
                    continue
                if not self.assault_navigable(ct, pos):
                    continue
                if pos not in goals:
                    goals.append(pos)

        if self.enemy_core_pos is not None:
            _, gunner_tiles = self.siege_positions()
            for pos in gunner_tiles:
                if self.assault_navigable(ct, pos) and pos not in goals:
                    goals.append(pos)
        for radius in (4, 3, 5, 2, 6):
            extend(radius)

        if not goals:
            return None

        signature = (target.x, target.y, len(goals), self.enemy_core_pos is not None)
        if self.assault_flow_signature == signature and self.assault_flow is not None:
            return self.assault_flow

        flow: dict[tuple[int, int], int] = {}
        queue: deque[Position] = deque()
        for pos in goals:
            key = (pos.x, pos.y)
            if key not in flow:
                flow[key] = 0
                queue.append(pos)

        while queue:
            pos = queue.popleft()
            base = flow[(pos.x, pos.y)]
            for d in DIRECTIONS:
                nxt = pos.add(d)
                key = (nxt.x, nxt.y)
                if key in flow or not self.assault_navigable(ct, nxt):
                    continue
                flow[key] = base + 1
                queue.append(nxt)

        self.assault_flow_signature = signature
        self.assault_flow = flow
        return flow

    def advance_assault_flow(self, ct: Controller, target: Position) -> bool:
        flow = self.ensure_assault_flow(ct, target)
        if not flow:
            return False

        current = ct.get_position()
        current_key = (current.x, current.y)
        current_score = flow.get(current_key)
        if current_score is None:
            self.assault_flow_signature = None
            self.assault_flow = None
            flow = self.ensure_assault_flow(ct, target)
            if not flow:
                return False
            current_score = flow.get(current_key)
            if current_score is None:
                return False

        options: list[tuple[tuple, Direction]] = []
        primary = current.direction_to(target)
        for d in directional_preferences(primary):
            dest = current.add(d)
            key = (dest.x, dest.y)
            score = flow.get(key)
            if score is None:
                continue
            occupied = ct.get_tile_builder_bot_id(dest) is not None if ct.is_in_vision(dest) else False
            passable = ct.is_tile_passable(dest) if ct.is_in_vision(dest) else False
            rank = (score, 1 if occupied else 0, 0 if passable else 1, direction_rank(primary, d))
            options.append((rank, d))

        for _, d in sorted(options):
            if self.try_step(ct, d):
                return True

        if self.enemy_core_pos is None and current.distance_squared(target) <= 20:
            self.advance_assault_guess()
        else:
            self.assault_flow_signature = None
            self.assault_flow = None
        return False

    def _assault_step_toward(self, ct: Controller, target: Position) -> bool:
        assert self.width is not None and self.height is not None
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
            if (
                ct.get_tile_env(dest) == Environment.EMPTY
                and ct.get_tile_building_id(dest) is None
            ):
                cost = ct.get_road_cost()
                if can_afford(ct, cost[0], cost[1], reserve=BASE_RESERVE):
                    if ct.can_build_road(dest):
                        ct.build_road(dest)
                        if ct.can_move(d):
                            ct.move(d)
                            return True
        return False

    def try_build_path_launcher(self, ct: Controller) -> bool:
        assert self.enemy_guesses is not None and self.width is not None and self.height is not None
        target = self.enemy_core_pos or self.enemy_guesses[self.assault_guess_index % len(self.enemy_guesses)]
        current = ct.get_position()
        for d in directional_preferences(current.direction_to(target)):
            build_pos = current.add(d)
            if not in_bounds(build_pos, self.width, self.height):
                continue
            if ct.get_tile_building_id(build_pos) is not None:
                continue
            if ct.get_tile_env(build_pos) != Environment.EMPTY and not ct.is_tile_passable(build_pos):
                continue
            cost = ct.get_launcher_cost()
            if not can_afford(ct, cost[0], cost[1], reserve=reserve_assault(ct)):
                return False
            if ct.can_build_launcher(build_pos):
                ct.build_launcher(build_pos)
                return True
        return False

    # -----------------------------------------------------------------------
    # Gunner / Launcher units
    # -----------------------------------------------------------------------

    def run_gunner(self, ct: Controller) -> None:
        """Fire at the gunner's fixed target tile each round."""
        target = ct.get_gunner_target()
        if target is None:
            return
        # Prefer hitting builder bots (they move), then buildings
        builder_id = ct.get_tile_builder_bot_id(target)
        if builder_id is not None:
            if ct.get_team(builder_id) != ct.get_team() and ct.can_fire(target):
                ct.fire(target)
            return
        building_id = ct.get_tile_building_id(target)
        if building_id is not None and ct.get_team(building_id) != ct.get_team():
            if ct.can_fire(target):
                ct.fire(target)

    def run_launcher(self, ct: Controller) -> None:
        """Launch adjacent friendly builder bots toward the enemy core."""
        assert self.enemy_guesses is not None
        target = self.enemy_core_pos or self.enemy_guesses[0]
        for entity_id in ct.get_nearby_buildings():
            if ct.get_entity_type(entity_id) == EntityType.CORE and ct.get_team(entity_id) != ct.get_team():
                target = ct.get_position(entity_id)
                break

        adjacent_allies = [
            bot_id
            for bot_id in ct.get_nearby_builder_bots()
            if ct.get_team(bot_id) == ct.get_team()
            and ct.get_position(bot_id).distance_squared(ct.get_position()) <= 2
        ]
        if not adjacent_allies:
            return

        if self.enemy_core_pos is not None:
            assert self.width is not None and self.height is not None
            for bot_id in adjacent_allies:
                bot_pos = ct.get_position(bot_id)
                for core_tile in core_tiles(self.enemy_core_pos, self.width, self.height):
                    if ct.can_launch(bot_pos, core_tile):
                        ct.launch(bot_pos, core_tile)
                        return

        candidates = [
            pos
            for pos in ct.get_nearby_tiles(26)
            if ct.is_tile_passable(pos)
            and ct.get_tile_builder_bot_id(pos) is None
            and pos.distance_squared(target) < ct.get_position().distance_squared(target)
        ]
        candidates.sort(key=lambda pos: (
            pos.distance_squared(target),
            -ct.get_position().distance_squared(pos),
        ))
        for bot_id in adjacent_allies:
            bot_pos = ct.get_position(bot_id)
            for pos in candidates:
                if ct.can_launch(bot_pos, pos):
                    ct.launch(bot_pos, pos)
                    return

    # -----------------------------------------------------------------------
    # Movement helpers
    # -----------------------------------------------------------------------

    def move_to_sink(self, ct: Controller) -> None:
        assert self.sink_pos is not None and self.preferred_dir is not None and self.core_pos is not None
        current = ct.get_position()
        if current == self.sink_pos:
            self.trail = [current]
            return
        if self.move_towards(ct, self.sink_pos, self.preferred_dir, avoid_core=False):
            if ct.get_position() == self.sink_pos:
                self.trail = [self.sink_pos]

    def move_towards(
        self,
        ct: Controller,
        target: Position | None,
        fallback: Direction,
        avoid_core: bool = True,
    ) -> bool:
        assert self.core_pos is not None and self.width is not None and self.height is not None
        current = ct.get_position()
        if target is None:
            candidates = directional_preferences(fallback)
        else:
            direction = current.direction_to(target)
            secondary = fallback if direction == Direction.CENTRE else None
            candidates = directional_preferences(direction, secondary)

        recent = set(self.trail[-6:])
        for allow_recent in (False, True):
            for d in candidates:
                dest = current.add(d)
                if not in_bounds(dest, self.width, self.height):
                    continue
                if avoid_core and not on_core_tile(current, self.core_pos) and on_core_tile(dest, self.core_pos):
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
        reserve = BASE_RESERVE if self.role != "assault" else max(BASE_RESERVE // 2, 20)
        if not can_afford(ct, cost[0], cost[1], reserve=reserve):
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

    def try_support_orbit(self, ct: Controller) -> None:
        assert self.core_pos is not None
        current = ct.get_position()
        if current.distance_squared(self.core_pos) <= 8:
            return
        self.move_towards(ct, self.core_pos, current.direction_to(self.core_pos), avoid_core=False)

    # -----------------------------------------------------------------------
    # Ore planning
    # -----------------------------------------------------------------------

    def plan_visible_ore(self, ct: Controller) -> tuple[Position | None, Position | None]:
        assert self.preferred_dir is not None
        current = ct.get_position()
        best_ore: Position | None = None
        best_stand: Position | None = None
        best_score = 10 ** 9
        for pos in ct.get_nearby_tiles():
            if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
                continue
            if ct.get_tile_building_id(pos) is not None:
                continue
            stand = self.find_build_stand(ct, pos)
            if stand is None:
                continue
            score = (
                current.distance_squared(stand) * 10
                + current.distance_squared(pos)
                + direction_rank(self.preferred_dir, current.direction_to(stand))
            )
            if score < best_score:
                best_ore = pos
                best_stand = stand
                best_score = score
        return best_ore, best_stand

    def ore_target_still_valid(self, ct: Controller, pos: Position) -> bool:
        if not ct.is_in_vision(pos):
            return False
        if ct.get_tile_env(pos) != Environment.ORE_TITANIUM:
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